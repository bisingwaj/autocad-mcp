"""Routage et exécution des appels d'outils.

Ce module est la frontière publique du serveur. Il traduit des arguments JSON
en opérations déclaratives, les fait exécuter par un backend, et rend un
compte rendu que le modèle peut exploiter pour se corriger. Il ne calcule
aucune géométrie: c'est ``ops`` qui décide quoi dessiner, et le backend qui
écrit.

Quatre garanties y sont tenues, chacune contre un défaut avéré du serveur
historique.

* **Aucun succès mensonger.** Toute erreur ressort par ``CadError.to_dict()``,
  avec son code stable, et ``ToolResult.is_error`` vaut alors ``True``. Un lot
  partiellement exécuté rapporte les deux côtés: ce qui a été créé et ce qui a
  échoué, avec l'index de l'élément fautif **dans la liste fournie par le
  modèle**, pas dans la liste aplatie des opérations internes.
* **Connexion paresseuse.** Aucun backend n'est instancié tant qu'un outil
  n'est pas appelé. Lancer le serveur sans AutoCAD ouvert doit rester possible:
  sinon le client MCP échoue au démarrage et le modèle ne voit même pas le
  catalogue.
* **Aucun gel de la boucle d'événements.** Les appels backend sont bloquants,
  parfois longs, et le backend COM les sérialise sur un fil STA. Ils passent
  donc tous par ``anyio.to_thread.run_sync``.
* **Réponses bornées.** Les listes de handles et d'entités sont tronquées, avec
  le compte total conservé.

**Convention d'angle.** Les arguments arrivent en degrés, parce que c'est ce
que manipulent un humain et un modèle. ``ops.primitives`` fait déjà la
conversion vers les radians du modèle pour les arcs, les textes et les
hachures; ce module ne la refait pas. Elle n'est faite ici que pour
``architecture.label``, dont la rotation est déjà exprimée en radians côté
métier.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from anyio import Lock, to_thread

from ..backends.base import MEASURE_KEYS, CadBackend, EntityFilter, EntityInfo, SupportsRender
from ..config import Config
from ..errors import (
    CadError,
    ConfirmationRequired,
    InvalidParameter,
    OperationFailed,
    UnsupportedOperation,
)
from ..logging_setup import get_logger
from ..model.layers import color_index
from ..model.ops import Operation, OperationBatch
from ..ops import architecture, blocks, primitives
from ..ops import dimension as ops_dimension
from ..ops import query as ops_query
from ..ops import validate as ops_validate
from ..ops import volume as ops_volume
from ..units import Defaults
from .schemas import (
    MAX_BATCH_ITEMS,
    MAX_CHECK_ENTITIES,
    MAX_CHECK_ITEMS,
    MAX_MEASURE_ENTITIES,
    MAX_PROBLEMS,
    MAX_QUERY_LIMIT,
    MAX_RENDER_PIXELS,
    MEASURE_MODES,
    MIN_RENDER_PIXELS,
    RENDER_PROJECTIONS,
    ToolSpec,
    build_catalog,
)

__all__ = ["ToolHandlers", "ToolResult"]

_LOG = get_logger("tools")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Résultat d'un appel d'outil, indépendant du protocole.

    ``payload`` est sérialisé en JSON par la couche serveur, ``image_png`` est
    transmis en ``ImageContent``. Le découplage garde ``server`` sans logique et
    rend ces handlers testables sans client MCP.
    """

    payload: dict[str, Any]
    is_error: bool = False
    image_png: bytes | None = None


@dataclass(slots=True)
class _Batch:
    """Opérations à exécuter et traçabilité vers la demande d'origine."""

    operations: list[Operation] = field(default_factory=list)
    #: Pour chaque opération, l'index de l'élément du modèle qui l'a produite.
    owners: list[int] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def add(self, index: int, produced: Sequence[Operation]) -> None:
        self.operations.extend(produced)
        self.owners.extend([index] * len(produced))


# ---------------------------------------------------------------------------
# Lecture défensive des arguments
# ---------------------------------------------------------------------------
#
# Rien ne garantit qu'un client MCP valide les arguments contre le schéma. Une
# valeur manquante ou du mauvais type doit produire une erreur nommée et
# corrigeable, pas un TypeError anonyme remonté depuis la géométrie.


def _field(args: dict[str, Any], name: str, *, where: str) -> Any:
    if name not in args or args[name] is None:
        raise InvalidParameter(f"Paramètre requis absent: {name}", field=name, where=where)
    return args[name]


def _point(value: Any, name: str, *, where: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise InvalidParameter(
            f"{name} doit être un couple [x, y]", field=name, where=where, got=repr(value)
        )
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(
            f"{name} contient une coordonnée non numérique",
            field=name,
            where=where,
            got=repr(value),
        ) from exc


def _point_list(value: Any, name: str, *, where: str, minimum: int) -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < minimum:
        raise InvalidParameter(
            f"{name} demande au moins {minimum} points",
            field=name,
            where=where,
            count=len(value) if isinstance(value, (list, tuple)) else None,
        )
    return [_point(item, f"{name}[{i}]", where=where) for i, item in enumerate(value)]


def _number(value: Any, name: str, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParameter(
            f"{name} doit être un nombre", field=name, where=where, got=repr(value)
        )
    return float(value)


def _opt_number(args: dict[str, Any], name: str, *, where: str) -> float | None:
    value = args.get(name)
    return None if value is None else _number(value, name, where=where)


def _number_or(args: dict[str, Any], name: str, default: float, *, where: str) -> float:
    value = _opt_number(args, name, where=where)
    return default if value is None else value


def _opt_int_list(value: Any, name: str, *, where: str) -> list[int] | None:
    """Liste d'entiers optionnelle, telle que ``segments`` de l'élément dimensions."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidParameter(f"{name} doit être une liste", field=name, where=where, got=repr(value))
    result: list[int] = []
    for i, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise InvalidParameter(
                f"{name}[{i}] doit être un entier", field=name, where=where, got=repr(item)
            )
        result.append(item)
    return result


def _text(value: Any, name: str, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidParameter(
            f"{name} doit être une chaîne non vide", field=name, where=where, got=repr(value)
        )
    return value


def _opt_text(args: dict[str, Any], name: str, *, where: str) -> str | None:
    value = args.get(name)
    return None if value is None else _text(value, name, where=where)


def _flag(args: dict[str, Any], name: str, default: bool, *, where: str) -> bool:
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidParameter(
            f"{name} doit être un booléen", field=name, where=where, got=repr(value)
        )
    return value


def _choice(args: dict[str, Any], name: str, allowed: tuple[str, ...], default: str, *, where: str) -> str:
    value = args.get(name)
    if value is None:
        return default
    if value not in allowed:
        raise InvalidParameter(
            f"{name} hors domaine", field=name, where=where, got=value, supported=list(allowed)
        )
    return str(value)


def _opt_color(args: dict[str, Any], name: str = "color") -> int | None:
    """Convertit un nom ou un index en index ACI. ``None`` laisse ByLayer."""
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidParameter(f"{name} doit être un nom de couleur ou un index ACI", got=repr(value))
    return color_index(value)


def _items(args: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Liste d'éléments d'un lot, bornée et homogène."""
    value = _field(args, name, where=name)
    if not isinstance(value, list) or not value:
        raise InvalidParameter(f"{name} doit être une liste non vide", field=name)
    if len(value) > MAX_BATCH_ITEMS:
        raise InvalidParameter(
            f"Lot trop grand: {len(value)} éléments",
            field=name,
            maximum=MAX_BATCH_ITEMS,
            remedy="découper en plusieurs appels, ce qui garde l'annulation exploitable",
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidParameter(
                f"{name}[{index}] doit être un objet", field=name, index=index, got=repr(item)
            )
    return value


def _kind(item: dict[str, Any], discriminator: str, index: int, allowed: tuple[str, ...]) -> str:
    value = item.get(discriminator)
    if value not in allowed:
        raise InvalidParameter(
            f"Champ {discriminator!r} absent ou inconnu",
            index=index,
            got=value,
            supported=list(allowed),
        )
    return str(value)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_DRAW_OPS = ("line", "polyline", "rectangle", "circle", "arc", "text", "mtext", "hatch")
_STRUCTURE_ELEMENTS = (
    "wall_network",
    "wall",
    "wall_run",
    "door",
    "door_in_wall",
    "window",
    "room",
    "label",
    "wall_volume",
    "slab",
    "box",
    "dimensions",
)
_OPENING_KINDS = ("door", "window", "passage")
_HANDS = ("left", "right")

#: Ce qu'un moteur doit publier pour qu'une longueur ou une aire soit mesurable.
#: Cité tel quel dans les réponses: une mesure absente doit se comprendre sans
#: aller lire le code du backend.
_MEASURE_NOTE = (
    "Une mesure n'est comptée que si le moteur la publie, dans les clés "
    + ", ".join(MEASURE_KEYS)
    + ". Un total nul serait un mensonge: il est rendu à null quand rien n'a répondu."
)


class ToolHandlers:
    """Exécute les appels d'outils sur un backend, ouvert à la demande.

    Args:
        config: configuration d'exécution, qui décide du backend, de l'unité et
            des bornes de réponse.
        backend: backend déjà construit. Sert aux tests et à l'usage
            programmatique; laissé à ``None``, il est instancié et connecté au
            premier appel d'outil.
    """

    def __init__(self, config: Config, backend: CadBackend | None = None) -> None:
        self._config = config
        self._backend = backend
        self._connected = False
        self._lock = Lock()
        self._catalog: tuple[ToolSpec, ...] = build_catalog(config)
        self._routes: dict[str, Callable[[dict[str, Any]], Awaitable[ToolResult]]] = {
            "get_drawing_info": self._get_drawing_info,
            "query_entities": self._query_entities,
            "measure": self._measure,
            "check_plan": self._check_plan,
            "render_view": self._render_view,
            "draw": self._draw,
            "build_structure": self._build_structure,
            "place_blocks": self._place_blocks,
            "delete_entities": self._delete_entities,
            "set_entity_color": self._set_entity_color,
            "undo_last_batch": self._undo_last_batch,
            "run_cad_command": self._run_cad_command,
        }

    # ---- catalogue ----------------------------------------------------

    @property
    def catalog(self) -> tuple[ToolSpec, ...]:
        """Outils exposés, dans l'ordre de déclaration."""
        return self._catalog

    @property
    def handled(self) -> tuple[str, ...]:
        """Noms effectivement routés. Doit coïncider avec le catalogue annoncé."""
        return tuple(self._routes)

    @property
    def connected(self) -> bool:
        """Vrai une fois le backend ouvert, donc pas avant le premier appel d'outil."""
        return self._connected and self._backend is not None

    @property
    def config(self) -> Config:
        return self._config

    # ---- cycle de vie -------------------------------------------------

    async def aclose(self) -> None:
        """Libère le backend. Ne ferme jamais AutoCAD."""
        backend = self._backend
        if backend is None or not self._connected:
            return
        self._connected = False
        await to_thread.run_sync(backend.close)

    def _create_backend(self) -> CadBackend:
        """Instancie le backend choisi par la configuration.

        L'import est fait ici, tardivement: importer ``backends.acad_com`` au
        chargement du paquet le rendrait inchargeable hors Windows.
        """
        from .. import get_backend

        name = self._config.backend
        if name == "ezdxf":
            backend = get_backend(name, path=self._config.dxf_path, unit=self._config.unit)
        elif name == "recording":
            backend = get_backend(name, unit=self._config.unit)
        else:
            backend = get_backend(name)
        return backend  # type: ignore[no-any-return]

    async def _ready(self) -> CadBackend:
        """Backend connecté, ouvert au premier besoin et pas avant."""
        async with self._lock:
            if self._backend is None:
                self._backend = await to_thread.run_sync(self._create_backend)
            if not self._connected:
                await to_thread.run_sync(self._backend.connect)
                self._connected = True
                _LOG.info("backend %s connecté", self._backend.name)
            return self._backend

    async def _defaults(self, backend: CadBackend) -> Defaults:
        """Valeurs par défaut à l'échelle du **document**, pas de la configuration.

        Un fichier ouvert peut être en millimètres alors que la configuration
        dit mètres. C'est l'unité du document qui fait foi, sans quoi un mur de
        vingt centimètres deviendrait un mur de vingt mètres.
        """
        unit = await to_thread.run_sync(lambda: backend.unit)
        return Defaults(unit)

    # ---- routage ------------------------------------------------------

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Exécute un outil. Ne lève jamais: toute erreur devient un résultat.

        Le modèle doit pouvoir lire la cause de l'échec et son code pour se
        corriger. Une exception qui remonterait au transport lui arriverait sans
        code exploitable.
        """
        handler = self._routes.get(name)
        if handler is None:
            return _failed(
                InvalidParameter(
                    f"Outil inconnu: {name!r}",
                    supported=[spec.name for spec in self._catalog],
                )
            )
        args = dict(arguments or {})
        try:
            return await handler(args)
        except CadError as exc:
            _LOG.warning("%s a échoué: %s (%s)", name, exc.message, exc.code)
            return _failed(exc)
        except Exception as exc:  # consigné, jamais avalé
            _LOG.exception("%s a levé une exception non typée", name)
            return _failed(
                OperationFailed(
                    f"{type(exc).__name__}: {exc}",
                    tool=name,
                    remedy="signaler l'anomalie; l'opération n'a pas abouti",
                )
            )

    # ---- lecture ------------------------------------------------------

    async def _get_drawing_info(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        info = await to_thread.run_sync(backend.document_info)
        defaults = await self._defaults(backend)

        payload = dict(info)
        payload.setdefault("backend", backend.name)
        payload["defaults"] = {
            "unit": defaults.unit.value,
            "wall_thickness": defaults.wall_thickness,
            "partition_thickness": defaults.partition_thickness,
            "door_width": defaults.door_width,
            "text_height": defaults.text_height,
            "note": "Longueurs converties dans l'unité du document.",
        }
        payload["limits"] = {
            "query_limit_default": self._config.query_limit,
            "query_limit_max": MAX_QUERY_LIMIT,
            "batch_items_max": MAX_BATCH_ITEMS,
            "measure_entities_max": MAX_MEASURE_ENTITIES,
            "check_entities_max": MAX_CHECK_ENTITIES,
        }
        payload["can_render"] = isinstance(backend, SupportsRender)
        # Une passerelle de commandes annoncée mais inopérante ferait perdre un
        # appel au modèle: on dit ici, avant qu'il essaie, si ce moteur en a une.
        payload["can_run_commands"] = _runs_commands(backend)
        payload["allowed_commands"] = sorted(self._config.allowed_commands)
        payload["blocks_library"] = list(blocks.LIBRARY)
        return ToolResult(payload)

    async def _measure(self, args: dict[str, Any]) -> ToolResult:
        """Mesures et requêtes spatiales, en un seul outil paramétré par mode."""
        backend = await self._ready()
        defaults = await self._defaults(backend)
        mode = _choice(args, "mode", MEASURE_MODES, "summary", where="mode")
        scope = _scope(args)

        total = await to_thread.run_sync(backend.count, scope)
        entities = await to_thread.run_sync(
            partial(backend.query, scope, limit=MAX_MEASURE_ENTITIES)
        )

        payload: dict[str, Any] = {
            "mode": mode,
            "scope": _selector_echo(scope),
            "unit": defaults.unit.value,
            "matching": total,
            "examined": len(entities),
        }
        if total > len(entities):
            payload["truncated"] = True
            payload["hint"] = (
                f"{total - len(entities)} entités correspondantes n'ont pas été "
                "examinées: la mesure porte sur un échantillon et non sur le dessin "
                "entier. Restreindre par layer ou par type pour obtenir un total juste."
            )

        engine = backend.name
        if mode == "summary":
            summary = ops_query.summarize(entities)
            described = summary.to_dict()
            described["length"] = _measure_payload(summary.length, engine)
            described["area"] = _measure_payload(summary.area, engine)
            payload["summary"] = described
        elif mode == "totals":
            payload["length"] = _measure_payload(ops_query.total_length(entities), engine)
            payload["area"] = _measure_payload(ops_query.total_area(entities), engine)
        elif mode in ("by_layer", "by_type"):
            groups = (
                ops_query.group_by_layer(entities)
                if mode == "by_layer"
                else ops_query.group_by_type(entities)
            )
            key = "layer" if mode == "by_layer" else "type"
            rows = [
                {
                    key: name,
                    "count": len(group),
                    "length": _measure_payload(ops_query.total_length(group), engine),
                    "area": _measure_payload(ops_query.total_area(group), engine),
                }
                for name, group in groups.items()
            ]
            payload["group_count"] = len(rows)
            payload.update(_bounded(rows, "groups", self._config.query_limit))
        elif mode == "quantities":
            rows = [
                {
                    "layer": row.layer,
                    "type": row.kind,
                    "count": row.count,
                    "length": _measure_payload(row.length, engine),
                    "area": _measure_payload(row.area, engine),
                }
                for row in ops_query.bill_of_quantities(entities)
            ]
            payload["row_count"] = len(rows)
            payload.update(_bounded(rows, "rows", self._config.query_limit))
        elif mode == "nearest":
            point = _point(_field(args, "point", where="point"), "point", where="point")
            limit = self._limit(args)
            found = ops_query.nearest(entities, point, limit=limit)
            payload["point"] = list(point)
            payload["returned"] = len(found)
            payload["entities"] = [
                {"distance": distance, **entity.to_dict()} for entity, distance in found
            ]
            payload["distance_note"] = (
                "La distance est celle du point à la boîte englobante de l'entité, "
                "donc nulle si le point tombe dedans, et minorante partout ailleurs."
            )
            payload.update(_without_bbox_note(entities))
        else:  # in_window
            window = _window(_field(args, "window", where="window"), "window")
            window_mode = _choice(
                args, "window_mode", ("inside", "crossing"), "inside", where="window_mode"
            )
            selected = ops_query.in_window(
                entities,
                window,
                mode=window_mode,  # type: ignore[arg-type]
                tol=defaults.tolerance,
            )
            limit = self._limit(args)
            payload["window"] = list(window)
            payload["window_mode"] = window_mode
            payload["matched"] = len(selected)
            payload.update(
                _bounded([entity.to_dict() for entity in selected], "entities", limit)
            )
            payload.update(_without_bbox_note(entities))
        return ToolResult(payload)

    async def _check_plan(self, args: dict[str, Any]) -> ToolResult:
        """Rapport de validation d'un plan, actionnable défaut par défaut.

        Trouver des défauts n'est pas un échec d'appel: le résultat n'est donc
        pas marqué en erreur, c'est ``ok`` qui porte le verdict. Confondre les
        deux empêcherait de distinguer « le plan est faux » de « la vérification
        n'a pas pu se faire ».
        """
        backend = await self._ready()
        defaults = await self._defaults(backend)
        scope = _scope(args)

        total = await to_thread.run_sync(backend.count, scope)
        entities = await to_thread.run_sync(
            partial(backend.query, scope, limit=MAX_CHECK_ENTITIES)
        )
        contours = _contours(args)
        segments = _segments(args)

        report = await to_thread.run_sync(
            partial(
                ops_validate.validate_plan,
                entities=entities,
                contours=contours,
                segments=segments,
                unit=defaults.unit,
                tol=_opt_number(args, "tolerance", where="tolerance"),
                minimum_size=_opt_number(args, "minimum_size", where="minimum_size"),
            )
        )

        described = report.to_dict()
        problems = described.pop("problems")
        payload: dict[str, Any] = {
            "unit": defaults.unit.value,
            "scope": _selector_echo(scope),
            **described,
            "matching": total,
        }
        payload.update(_bounded(problems, "problems", MAX_PROBLEMS))
        if total > len(entities):
            payload["truncated"] = True
            payload["hint"] = (
                f"{total - len(entities)} entités n'ont pas été examinées: le rapport "
                "est partiel. Restreindre par layer et recommencer calque par calque."
            )
        elif not contours and not segments:
            payload["hint"] = (
                "Seuls les doublons et les résidus ont été cherchés. Les contours non "
                "fermés, les murs qui se croisent et les trous entre extrémités "
                "demandent que vous fournissiez contours et segments: le moteur publie "
                "des boîtes englobantes, pas les sommets, et rien n'est deviné."
            )
        elif report.ok:
            payload["hint"] = (
                "Aucun défaut grave. Les avertissements restent à regarder: un doublon "
                "est une suspicion fondée sur la boîte englobante, pas une certitude."
            )
        else:
            payload["hint"] = (
                "Corriger les défauts de gravité \"error\" d'abord: chacun porte sa "
                "localisation et son remède. Puis rappeler check_plan."
            )
        return ToolResult(payload)

    async def _query_entities(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        selector = _selector(args)
        limit = self._limit(args)

        total = await to_thread.run_sync(backend.count, selector)
        entities = await to_thread.run_sync(partial(backend.query, selector, limit=limit))

        payload: dict[str, Any] = {
            "total": total,
            "returned": len(entities),
            "limit": limit,
            "truncated": total > len(entities),
            "filter": _selector_echo(selector),
            "entities": [entity.to_dict() for entity in entities],
        }
        if payload["truncated"]:
            payload["hint"] = (
                f"{total - len(entities)} entités correspondantes ne sont pas décrites ici. "
                "Affiner le filtre par calque, type ou fenêtre plutôt qu'augmenter limit, "
                "ou regarder le dessin avec render_view."
            )
        return ToolResult(payload)

    async def _render_view(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        if not isinstance(backend, SupportsRender):
            raise UnsupportedOperation(
                f"Le backend {backend.name} ne produit pas d'image",
                remedy=(
                    "sur AutoCAD, l'écran tient lieu de retour visuel; pour obtenir une "
                    "image, lancer le serveur avec AUTOCAD_MCP_BACKEND=ezdxf"
                ),
            )

        width = self._pixels(args, "width", self._config.render_size[0])
        height = self._pixels(args, "height", self._config.render_size[1])
        projection = _choice(args, "projection", RENDER_PROJECTIONS, "plan", where="projection")

        if projection == "iso":
            return await self._render_iso(backend, width, height, args)

        png = await to_thread.run_sync(backend.render_png, width, height)
        count = await to_thread.run_sync(backend.count, None)
        extents = await self._extents(backend)

        payload: dict[str, Any] = {
            "backend": backend.name,
            "projection": "plan",
            "width": width,
            "height": height,
            "png_bytes": len(png),
            "entity_count": count,
            "extents": list(extents) if extents else None,
            "unit": (await self._defaults(backend)).unit.value,
        }
        if count == 0:
            payload["note"] = (
                "Le dessin est vide: l'image l'est aussi, ce qui n'est pas une erreur."
            )
        return ToolResult(payload, image_png=png)

    async def _render_iso(
        self, backend: CadBackend, width: int, height: int, args: dict[str, Any]
    ) -> ToolResult:
        """Vue en volume, relue depuis le document plutôt que depuis un lot en mémoire.

        Relire le document plutôt que rejouer une liste d'opérations garde ce
        rendu correct qu'il vienne d'un seul appel à build_structure ou de
        plusieurs, et même d'un document rouvert.
        """
        from .. import render as render_module
        from .. import viewer

        document = getattr(backend, "document", None)
        if document is None:
            raise UnsupportedOperation(
                f"Le backend {backend.name} n'expose pas de document pour la vue en volume",
                remedy=(
                    "la projection iso suppose un moteur qui garde un document "
                    "accessible, ce que le backend ezdxf fait; utiliser "
                    'projection="plan" sur ce moteur'
                ),
            )

        scene = await to_thread.run_sync(partial(viewer.scene_from_document, document))
        if not scene["meshes"]:
            raise OperationFailed(
                "Le document ne contient aucun volume: la vue en trois dimensions "
                "serait vide",
                remedy=(
                    "construire d'abord des volumes avec build_structure (wall_volume, "
                    "slab ou box), puis rappeler render_view en projection iso"
                ),
            )

        elevation = _number_or(
            args, "elevation_deg", render_module.ISO_ELEVATION, where="elevation_deg"
        )
        azimuth = _number_or(
            args, "azimuth_deg", render_module.ISO_AZIMUTH, where="azimuth_deg"
        )
        png = await to_thread.run_sync(
            partial(
                render_module.render_scene_png,
                scene,
                width,
                height,
                elevation=elevation,
                azimuth=azimuth,
            )
        )

        payload: dict[str, Any] = {
            "backend": backend.name,
            "projection": "iso",
            "width": width,
            "height": height,
            "png_bytes": len(png),
            "mesh_count": scene["counts"]["meshes"],
            "face_count": scene["counts"]["faces"],
            "bounds": scene["bounds"],
            "elevation_deg": elevation,
            "azimuth_deg": azimuth,
            "unit": scene["unit"],
        }
        return ToolResult(payload, image_png=png)

    async def _extents(self, backend: CadBackend) -> tuple[float, float, float, float] | None:
        """Limites du dessin, ``None`` si le moteur ne sait pas les calculer."""
        try:
            return await to_thread.run_sync(backend.extents)
        except UnsupportedOperation:
            return None

    # ---- écriture -----------------------------------------------------

    async def _draw(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        defaults = await self._defaults(backend)
        items = _items(args, "operations")
        label = _opt_text(args, "label", where="label") or "draw"

        batch = _Batch()
        for index, item in enumerate(items):
            try:
                batch.add(index, _draw_operation(item, index, defaults))
            except CadError as exc:
                batch.failures.append(_rejected(index, item.get("op"), exc))

        return await self._execute(backend, batch, label, len(items), "op")

    async def _build_structure(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        defaults = await self._defaults(backend)
        items = _items(args, "elements")
        label = _opt_text(args, "label", where="label") or "build_structure"

        batch = _Batch()
        for index, item in enumerate(items):
            try:
                batch.add(index, _structure_operation(item, index, defaults))
            except CadError as exc:
                batch.failures.append(_rejected(index, item.get("element"), exc))

        return await self._execute(backend, batch, label, len(items), "element")

    async def _place_blocks(self, args: dict[str, Any]) -> ToolResult:
        """Insère des occurrences de la bibliothèque, définitions comprises.

        ``blocks.insert`` émet la définition avant l'occurrence, et la
        définition a une sémantique de garantie de présence: le modèle n'a donc
        rien à préparer, et un même symbole posé vingt fois ne produit qu'une
        définition.
        """
        backend = await self._ready()
        defaults = await self._defaults(backend)
        items = _items(args, "blocks")
        label = _opt_text(args, "label", where="label") or "place_blocks"

        batch = _Batch()
        for index, item in enumerate(items):
            try:
                batch.add(index, _block_operation(item, index, defaults))
            except CadError as exc:
                batch.failures.append(_rejected(index, item.get("block"), exc))

        return await self._execute(backend, batch, label, len(items), "block")

    async def _execute(
        self,
        backend: CadBackend,
        batch: _Batch,
        label: str,
        requested: int,
        discriminator: str,
    ) -> ToolResult:
        """Exécute le lot et rapporte les deux côtés du résultat.

        Les éléments refusés à la construction et ceux refusés à l'exécution
        sont rassemblés dans une seule liste, ramenés à l'index de l'élément
        tel que le modèle l'a fourni. Sans cette remise en correspondance,
        l'index rapporté désignerait une opération interne dont le modèle n'a
        jamais entendu parler, comme la création implicite d'un calque.
        """
        failures = list(batch.failures)
        payload: dict[str, Any] = {"label": label, "requested": requested}

        if not batch.operations:
            payload["ok"] = False
            payload["created_count"] = 0
            payload["handles"] = []
            payload["failed_count"] = len(failures)
            payload["failures"] = failures
            payload["error"] = "Aucun élément exploitable dans le lot"
            payload["code"] = InvalidParameter.code
            return ToolResult(payload, is_error=True)

        operations = OperationBatch(tuple(batch.operations), label=label)
        result = await to_thread.run_sync(backend.execute, operations)

        for failure in result.failures:
            remapped = dict(failure)
            flat = failure.get("index")
            if isinstance(flat, int) and 0 <= flat < len(batch.owners):
                remapped["index"] = batch.owners[flat]
                remapped["internal_index"] = flat
            remapped["stage"] = "execute"
            remapped.setdefault(discriminator, remapped.pop("operation", None))
            failures.append(remapped)

        handles = [ref.handle for ref in result.created]
        payload["ok"] = not failures
        payload["created_count"] = len(handles)
        payload.update(_bounded(handles, "handles", self._config.query_limit))
        if result.layers:
            payload["layers"] = result.layers
        if result.blocks:
            # Une définition de bloc n'est pas une entité: elle n'a pas de
            # handle, ne se compte pas parmi les créations et ne s'annule pas
            # avec le lot. La rapporter à part est la seule façon de dire au
            # modèle que le symbole est désormais disponible dans le document.
            payload["blocks"] = result.blocks
        if failures:
            payload["failed_count"] = len(failures)
            payload["failures"] = failures
            payload["hint"] = (
                "Le lot est partiellement exécuté. Les entités listées dans handles "
                "existent réellement; les éléments en échec n'ont rien créé. Corriger "
                "puis rappeler l'outil avec les seuls éléments fautifs."
            )
        if handles:
            payload.update(await self._autosave(backend))
        return ToolResult(payload, is_error=bool(failures))

    async def _autosave(self, backend: CadBackend) -> dict[str, Any]:
        """Écrit le document sur disque quand un chemin est configuré.

        Sans cela, un dessin produit par le backend DXF ne quitterait jamais la
        mémoire du serveur: l'utilisateur ne verrait aucun fichier apparaître et
        perdrait tout à l'arrêt. Un échec d'écriture est rapporté tel quel, sans
        remettre en cause les entités qui, elles, ont bien été créées.
        """
        if not self._config.dxf_path:
            return {}
        try:
            path = await to_thread.run_sync(backend.save)
        except CadError as exc:
            _LOG.error("enregistrement impossible: %s", exc.message)
            return {"saved": False, "save_error": exc.to_dict()}
        return {"saved": True, "saved_to": path}

    # ---- édition ------------------------------------------------------

    async def _delete_entities(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        selector = _selector(args)
        confirmed = _flag(args, "confirm_delete_all", False, where="confirm_delete_all")

        if selector.is_empty:
            if not confirmed:
                raise ConfirmationRequired(
                    "Suppression sans filtre refusée: elle effacerait tout le dessin",
                    remedy=(
                        "préciser au moins un critère (layer, type, color, handles, window), "
                        "ou poser confirm_delete_all à true si l'effacement total est "
                        "réellement demandé"
                    ),
                )
            # Un filtre vide est refusé par les backends, et c'est heureux. Pour
            # honorer une demande d'effacement total, on désigne explicitement
            # chaque entité par son handle.
            existing = await to_thread.run_sync(partial(backend.query, None, limit=None))
            if not existing:
                return ToolResult(
                    {"deleted_count": 0, "handles": [], "filter": {}, "note": "Dessin déjà vide"}
                )
            selector = EntityFilter(handles=tuple(entity.handle for entity in existing))

        deleted = await to_thread.run_sync(backend.delete, selector)
        payload: dict[str, Any] = {
            "deleted_count": len(deleted),
            "filter": _selector_echo(selector) if not confirmed else {"all": True},
            "irreversible": True,
        }
        payload.update(_bounded(list(deleted), "handles", self._config.query_limit))
        if not deleted:
            payload["note"] = "Aucune entité ne correspond au filtre: rien n'a été supprimé."
        else:
            payload.update(await self._autosave(backend))
        return ToolResult(payload)

    async def _set_entity_color(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        raw = _field(args, "handles", where="handles")
        if not isinstance(raw, list) or not raw:
            raise InvalidParameter("handles doit être une liste non vide", field="handles")
        if len(raw) > MAX_BATCH_ITEMS:
            raise InvalidParameter(
                f"Trop de handles: {len(raw)}", field="handles", maximum=MAX_BATCH_ITEMS
            )
        handles = [_text(item, f"handles[{i}]", where="handles") for i, item in enumerate(raw)]
        color = _opt_color(args)
        if color is None:
            raise InvalidParameter("color est requis", field="color")

        updated: list[str] = []
        failures: list[dict[str, Any]] = []
        for index, handle in enumerate(handles):
            try:
                await to_thread.run_sync(backend.set_color, handle, color)
            except CadError as exc:
                failures.append({"index": index, "handle": handle, **exc.to_dict()})
            else:
                updated.append(handle)

        payload: dict[str, Any] = {
            "ok": not failures,
            "color": color,
            "updated_count": len(updated),
        }
        payload.update(_bounded(updated, "handles", self._config.query_limit))
        if failures:
            payload["failed_count"] = len(failures)
            payload["failures"] = failures
            payload["hint"] = (
                "Un handle inconnu vient souvent d'une entité déjà supprimée. "
                "Rafraîchir la liste avec query_entities."
            )
        if updated:
            payload.update(await self._autosave(backend))
        return ToolResult(payload, is_error=bool(failures))

    async def _undo_last_batch(self, args: dict[str, Any]) -> ToolResult:
        backend = await self._ready()
        await to_thread.run_sync(backend.undo)
        remaining = await to_thread.run_sync(backend.count, None)
        payload: dict[str, Any] = {
            "ok": True,
            "entity_count": remaining,
            "scope": (
                "Seules les créations du dernier lot sont annulées. Les couleurs "
                "changées et les entités supprimées ne sont pas restaurées."
            ),
        }
        payload.update(await self._autosave(backend))
        return ToolResult(payload)

    # ---- passerelle vers le logiciel de CAO ----------------------------

    async def _run_cad_command(self, args: dict[str, Any]) -> ToolResult:
        """Exécute une commande native, strictement filtrée par la liste blanche.

        Le filtrage est fait ICI et nulle part ailleurs. ``backend.run_command``
        transmet la chaîne au logiciel: laisser passer une commande non
        autorisée reviendrait à exécuter du code arbitraire sur la machine de
        l'utilisateur. L'ordre des contrôles compte: la liste blanche d'abord,
        la confirmation ensuite, l'appel en dernier.
        """
        backend = await self._ready()
        raw = _text(_field(args, "command", where="command"), "command", where="command")
        command = raw.strip().upper()
        allowed = sorted(self._config.allowed_commands)

        if command not in self._config.allowed_commands:
            raise InvalidParameter(
                f"Commande refusée par la liste blanche: {raw!r}",
                field="command",
                got=command,
                allowed=allowed,
                remedy=(
                    "n'employer qu'une commande de allowed; la liste est volontairement "
                    "courte, parce qu'une chaîne libre transmise à un logiciel de CAO "
                    "vaut exécution de code arbitraire sur la machine de l'utilisateur"
                ),
            )
        if not _flag(args, "confirm_command", False, where="confirm_command"):
            raise ConfirmationRequired(
                f"Commande {command} refusée sans confirmation explicite",
                command=command,
                remedy=(
                    "poser confirm_command à true; la commande agit directement sur le "
                    "document et undo_last_batch ne la défera pas"
                ),
            )
        arguments = _string_list(args.get("arguments"), "arguments")

        outcome = await to_thread.run_sync(backend.run_command, command, arguments)
        count = await to_thread.run_sync(backend.count, None)

        payload: dict[str, Any] = {
            "ok": True,
            "backend": backend.name,
            "command": command,
            "arguments": arguments,
            "irreversible": True,
            "result": _bounded_values(outcome),
            "entity_count": count,
            "hint": (
                "Le dessin n'a pas été relu pour vérifier l'effet de la commande: "
                "regarder le résultat avec render_view, ou le chiffrer avec measure."
            ),
        }
        payload.update(await self._autosave(backend))
        return ToolResult(payload)

    # ---- bornes -------------------------------------------------------

    def _limit(self, args: dict[str, Any]) -> int:
        value = args.get("limit")
        if value is None:
            requested = int(self._config.query_limit)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParameter("limit doit être un entier", field="limit", got=repr(value))
        else:
            requested = value
        if requested < 1:
            raise InvalidParameter(
                "limit doit être strictement positif", field="limit", got=requested
            )
        return min(requested, MAX_QUERY_LIMIT)

    def _pixels(self, args: dict[str, Any], name: str, default: int) -> int:
        value = args.get(name)
        if value is None:
            size = int(default)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParameter(f"{name} doit être un entier", field=name, got=repr(value))
        else:
            size = value
        if not MIN_RENDER_PIXELS <= size <= MAX_RENDER_PIXELS:
            raise InvalidParameter(
                f"{name} hors bornes: {size} pixels",
                field=name,
                minimum=MIN_RENDER_PIXELS,
                maximum=MAX_RENDER_PIXELS,
            )
        return size


# ---------------------------------------------------------------------------
# Construction des opérations
# ---------------------------------------------------------------------------


def _draw_operation(item: dict[str, Any], index: int, defaults: Defaults) -> list[Operation]:
    """Traduit une primitive demandée en opérations déclaratives.

    Les degrés reçus ici sont convertis en radians par ``ops.primitives``, qui
    est la frontière prévue pour cela. Les redoubler donnerait des arcs faux.
    """
    kind = _kind(item, "op", index, _DRAW_OPS)
    where = f"operations[{index}].{kind}"
    layer = _opt_text(item, "layer", where=where)
    color = _opt_color(item)

    if kind == "line":
        return primitives.line(
            _point(_field(item, "start", where=where), "start", where=where),
            _point(_field(item, "end", where=where), "end", where=where),
            layer=layer or "0",
            color=color,
        )
    if kind == "polyline":
        return primitives.polyline(
            _point_list(_field(item, "points", where=where), "points", where=where, minimum=2),
            closed=_flag(item, "closed", False, where=where),
            width=_number_or(item, "width", 0.0, where=where),
            layer=layer or "0",
            color=color,
        )
    if kind == "rectangle":
        return primitives.rectangle(
            _point(_field(item, "corner1", where=where), "corner1", where=where),
            _point(_field(item, "corner2", where=where), "corner2", where=where),
            defaults,
            layer=layer or "0",
            color=color,
        )
    if kind == "circle":
        return primitives.circle(
            _point(_field(item, "center", where=where), "center", where=where),
            _number(_field(item, "radius", where=where), "radius", where=where),
            layer=layer or "0",
            color=color,
        )
    if kind == "arc":
        return primitives.arc(
            _point(_field(item, "center", where=where), "center", where=where),
            _number(_field(item, "radius", where=where), "radius", where=where),
            _number(_field(item, "start_angle_deg", where=where), "start_angle_deg", where=where),
            _number(_field(item, "end_angle_deg", where=where), "end_angle_deg", where=where),
            layer=layer or "0",
            color=color,
        )
    if kind == "text":
        return primitives.text(
            _point(_field(item, "position", where=where), "position", where=where),
            _text(_field(item, "content", where=where), "content", where=where),
            defaults,
            height=_opt_number(item, "height", where=where),
            rotation_deg=_number_or(item, "rotation_deg", 0.0, where=where),
            layer=layer or "ANNOTATION",
            color=color,
            halign=_choice(item, "halign", ("left", "center", "right"), "left", where=where),
        )
    if kind == "mtext":
        return primitives.mtext(
            _point(_field(item, "position", where=where), "position", where=where),
            _text(_field(item, "content", where=where), "content", where=where),
            defaults,
            height=_opt_number(item, "height", where=where),
            width=_number_or(item, "width", 0.0, where=where),
            rotation_deg=_number_or(item, "rotation_deg", 0.0, where=where),
            layer=layer or "ANNOTATION",
            color=color,
        )
    # hatch
    raw = _field(item, "boundaries", where=where)
    if not isinstance(raw, list) or not raw:
        raise InvalidParameter("boundaries doit être une liste de contours", where=where)
    rings = [
        _point_list(ring, f"boundaries[{i}]", where=where, minimum=3) for i, ring in enumerate(raw)
    ]
    return primitives.hatch(
        rings,
        pattern=_opt_text(item, "pattern", where=where) or "SOLID",
        scale=_number_or(item, "scale", 1.0, where=where),
        angle_deg=_number_or(item, "angle_deg", 0.0, where=where),
        layer=layer or "HATCH",
        color=color,
    )


def _structure_operation(item: dict[str, Any], index: int, defaults: Defaults) -> list[Operation]:
    """Traduit un élément de bâtiment en opérations déclaratives."""
    kind = _kind(item, "element", index, _STRUCTURE_ELEMENTS)
    where = f"elements[{index}].{kind}"
    color = _opt_color(item)

    if kind == "wall_network":
        points = _point_list(
            _field(item, "points", where=where), "points", where=where, minimum=2
        )
        closed = _flag(item, "closed", False, where=where)
        return architecture.wall_network(
            points,
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            closed=closed,
            openings=_openings(item.get("openings"), points, closed, where=where),
            color=color,
            layer=_opt_text(item, "layer", where=where),
            show_symbols=_flag(item, "show_symbols", True, where=where),
        )
    if kind == "wall":
        return architecture.wall(
            _point(_field(item, "start", where=where), "start", where=where),
            _point(_field(item, "end", where=where), "end", where=where),
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            color=color,
            layer=_opt_text(item, "layer", where=where),
        )
    if kind == "wall_run":
        return architecture.wall_run(
            _point_list(_field(item, "points", where=where), "points", where=where, minimum=2),
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            closed=_flag(item, "closed", False, where=where),
            color=color,
        )
    if kind == "door":
        return architecture.door(
            _point(_field(item, "hinge", where=where), "hinge", where=where),
            _point(_field(item, "leaf_end", where=where), "leaf_end", where=where),
            defaults,
            opening_deg=_number_or(item, "opening_deg", 90.0, where=where),
            hand=_choice(item, "hand", ("left", "right"), "left", where=where),  # type: ignore[arg-type]
            color=color,
            show_swing=_flag(item, "show_swing", True, where=where),
        )
    if kind == "door_in_wall":
        return architecture.door_in_wall(
            _point(_field(item, "wall_start", where=where), "wall_start", where=where),
            _point(_field(item, "wall_end", where=where), "wall_end", where=where),
            defaults,
            position=_number_or(item, "position", 0.5, where=where),
            width=_opt_number(item, "width", where=where),
            hand=_choice(item, "hand", ("left", "right"), "left", where=where),  # type: ignore[arg-type]
            opening_deg=_number_or(item, "opening_deg", 90.0, where=where),
        )
    if kind == "window":
        return architecture.window(
            _point(_field(item, "start", where=where), "start", where=where),
            _point(_field(item, "end", where=where), "end", where=where),
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            color=color,
        )
    if kind == "room":
        return architecture.room(
            _point(_field(item, "corner1", where=where), "corner1", where=where),
            _point(_field(item, "corner2", where=where), "corner2", where=where),
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            name=_opt_text(item, "name", where=where),
            color=color,
            show_area=_flag(item, "show_area", True, where=where),
        )
    if kind == "label":
        # Seul cas où la conversion des degrés est à faire ici, la fonction
        # métier attendant des radians comme tout le modèle.
        return architecture.label(
            _point(_field(item, "position", where=where), "position", where=where),
            _text(_field(item, "text", where=where), "text", where=where),
            defaults,
            height=_opt_number(item, "height", where=where),
            rotation=math.radians(_number_or(item, "rotation_deg", 0.0, where=where)),
            color=color,
        )
    if kind == "wall_volume":
        # Pendant en volume de wall_network: mêmes points, mêmes baies, même
        # validation de rang de mur, une hauteur en plus.
        points = _point_list(
            _field(item, "points", where=where), "points", where=where, minimum=2
        )
        closed = _flag(item, "closed", False, where=where)
        return ops_volume.wall_volume(
            points,
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            closed=closed,
            openings=_openings(item.get("openings"), points, closed, where=where),
            height=_opt_number(item, "height", where=where),
            layer=_opt_text(item, "layer", where=where),
            color=color,
        )
    if kind == "slab":
        return ops_volume.slab(
            _point_list(
                _field(item, "contour", where=where), "contour", where=where, minimum=3
            ),
            defaults,
            thickness=_opt_number(item, "thickness", where=where),
            z=_number_or(item, "z", 0.0, where=where),
            layer=_opt_text(item, "layer", where=where),
        )
    if kind == "box":
        return ops_volume.box(
            _point(_field(item, "corner1", where=where), "corner1", where=where),
            _point(_field(item, "corner2", where=where), "corner2", where=where),
            z=_number_or(item, "z", 0.0, where=where),
            height=_number(_field(item, "height", where=where), "height", where=where),
            layer=_opt_text(item, "layer", where=where) or "FURNITURE",
            color=color,
        )
    # dimensions: pendant coté de wall_network, mêmes points et mêmes baies,
    # aucune maçonnerie ni symbole produits.
    points = _point_list(_field(item, "points", where=where), "points", where=where, minimum=2)
    closed = _flag(item, "closed", False, where=where)
    return ops_dimension.dimension_walls(
        points,
        defaults,
        openings=_openings(item.get("openings"), points, closed, where=where),
        closed=closed,
        thickness=_opt_number(item, "thickness", where=where),
        segments=_opt_int_list(item.get("segments"), "segments", where=where),
        outside=_flag(item, "outside", True, where=where),
        layer=_opt_text(item, "layer", where=where),
    )


def _openings(
    value: Any,
    points: list[tuple[float, float]],
    closed: bool,
    *,
    where: str,
) -> list[architecture.Opening] | None:
    """Traduit les baies d'une enfilade, en vérifiant qu'elles la désignent.

    Le rang du mur percé est validé ici, contre le nombre de murs réellement
    formés par les points donnés. ``ops.architecture`` le vérifie aussi, mais
    seulement quand il pose les symboles: une baie hors de l'enfilade avec
    ``show_symbols`` à faux serait sinon ignorée en silence, et le modèle
    croirait avoir percé un mur qui n'a pas bougé.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidParameter(
            "openings doit être une liste", field="openings", where=where, got=repr(value)
        )
    if not value:
        return None
    if len(value) > MAX_BATCH_ITEMS:
        raise InvalidParameter(
            f"Trop de baies dans une enfilade: {len(value)}",
            field="openings",
            where=where,
            maximum=MAX_BATCH_ITEMS,
        )

    segments = len(points) if closed else len(points) - 1
    baies: list[architecture.Opening] = []
    for index, raw in enumerate(value):
        spot = f"{where}.openings[{index}]"
        if not isinstance(raw, dict):
            raise InvalidParameter(
                "Une baie doit être un objet", where=spot, index=index, got=repr(raw)
            )
        segment = raw.get("segment")
        if isinstance(segment, bool) or not isinstance(segment, int):
            raise InvalidParameter(
                "segment doit être un entier", field="segment", where=spot, got=repr(segment)
            )
        if not 0 <= segment < segments:
            raise InvalidParameter(
                f"Baie hors de l'enfilade: rang {segment}",
                field="segment",
                where=spot,
                got=segment,
                segments=segments,
                remedy=(
                    f"le rang va de 0 à {segments - 1}; le mur de points[0] à points[1] "
                    "porte le rang 0, et une enfilade fermée compte un mur de plus"
                ),
            )
        position = _number(_field(raw, "position", where=spot), "position", where=spot)
        if not 0.0 <= position <= 1.0:
            raise InvalidParameter(
                "position doit être une fraction de 0 à 1 de la longueur du mur",
                field="position",
                where=spot,
                got=position,
            )
        width = _number(_field(raw, "width", where=spot), "width", where=spot)
        if width <= 0.0:
            raise InvalidParameter(
                "width doit être strictement positive",
                field="width",
                where=spot,
                got=width,
            )
        baies.append(
            architecture.Opening(
                segment=segment,
                position=position,
                width=width,
                kind=_choice(raw, "kind", _OPENING_KINDS, "door", where=spot),  # type: ignore[arg-type]
                hand=_choice(raw, "hand", _HANDS, "left", where=spot),  # type: ignore[arg-type]
                opening_deg=_number_or(raw, "opening_deg", 90.0, where=spot),
            )
        )
    return baies


def _block_operation(item: dict[str, Any], index: int, defaults: Defaults) -> list[Operation]:
    """Traduit une occurrence de symbole en opérations déclaratives.

    La clé inconnue est refusée par ``blocks.spec``, qui rend la liste des clés
    valides: c'est la bibliothèque qui fait autorité, pas une copie tenue ici.
    """
    where = f"blocks[{index}]"
    return blocks.insert(
        _text(_field(item, "block", where=where), "block", where=where),
        _point(_field(item, "at", where=where), "at", where=where),
        defaults,
        rotation_deg=_number_or(item, "rotation_deg", 0.0, where=where),
        scale=_number_or(item, "scale", 1.0, where=where),
        mark=_opt_text(item, "mark", where=where),
    )


def _contours(args: dict[str, Any]) -> list[ops_validate.Contour]:
    """Contours soumis à la validation, tels que le modèle les a tracés."""
    raw = args.get("contours")
    if raw is None:
        return []
    items = _checked_list(raw, "contours")
    contours: list[ops_validate.Contour] = []
    for index, item in enumerate(items):
        where = f"contours[{index}]"
        contours.append(
            ops_validate.Contour(
                points=tuple(
                    _point_list(
                        _field(item, "points", where=where), "points", where=where, minimum=2
                    )
                ),
                closed=_flag(item, "closed", False, where=where),
                ref=_opt_text(item, "ref", where=where) or "",
                layer=_opt_text(item, "layer", where=where) or "",
            )
        )
    return contours


def _segments(args: dict[str, Any]) -> list[ops_validate.Segment]:
    """Axes de murs soumis à la validation."""
    raw = args.get("segments")
    if raw is None:
        return []
    items = _checked_list(raw, "segments")
    segments: list[ops_validate.Segment] = []
    for index, item in enumerate(items):
        where = f"segments[{index}]"
        segments.append(
            ops_validate.Segment(
                start=_point(_field(item, "start", where=where), "start", where=where),
                end=_point(_field(item, "end", where=where), "end", where=where),
                ref=_opt_text(item, "ref", where=where) or "",
                layer=_opt_text(item, "layer", where=where) or "",
            )
        )
    return segments


def _checked_list(value: Any, name: str) -> list[dict[str, Any]]:
    """Liste d'objets soumise à ``check_plan``, bornée et homogène.

    La borne n'est pas décorative: la recherche de croisements et de trous
    compare les éléments deux à deux, donc son coût croît avec le carré de leur
    nombre.
    """
    if not isinstance(value, list):
        raise InvalidParameter(f"{name} doit être une liste", field=name, got=repr(value))
    if len(value) > MAX_CHECK_ITEMS:
        raise InvalidParameter(
            f"{name}: {len(value)} éléments soumis à la fois",
            field=name,
            maximum=MAX_CHECK_ITEMS,
            remedy="vérifier le plan zone par zone, ce qui rend aussi le rapport lisible",
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidParameter(
                f"{name}[{index}] doit être un objet", field=name, index=index, got=repr(item)
            )
    return value


def _string_list(value: Any, name: str) -> list[str]:
    """Suite de chaînes, vide si le champ est absent."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidParameter(f"{name} doit être une liste", field=name, got=repr(value))
    return [
        item if isinstance(item, str) else _reject_non_string(item, name, index)
        for index, item in enumerate(value)
    ]


def _reject_non_string(value: Any, name: str, index: int) -> str:
    raise InvalidParameter(
        f"{name}[{index}] doit être une chaîne", field=name, index=index, got=repr(value)
    )


def _window(value: Any, name: str) -> tuple[float, float, float, float]:
    """Fenêtre rectangulaire, lue défensivement."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise InvalidParameter(
            f"{name} doit valoir [xmin, ymin, xmax, ymax]", field=name, got=repr(value)
        )
    xmin, ymin, xmax, ymax = (
        _number(item, f"{name}[{i}]", where=name) for i, item in enumerate(value)
    )
    return (xmin, ymin, xmax, ymax)


def _runs_commands(backend: CadBackend) -> bool:
    """Vrai si ce moteur a réellement un interpréteur de commandes.

    ``CadBackend.run_command`` existe toujours mais lève par défaut. Annoncer la
    passerelle comme disponible ferait perdre un appel au modèle et lui ferait
    croire à un échec ponctuel là où il y a une incapacité permanente.
    """
    return type(backend).run_command is not CadBackend.run_command


def _measure_payload(measure: ops_query.Measure, engine: str) -> dict[str, Any]:
    """Met une mesure en réponse, en disant ce qu'elle ignore.

    Le total est ramené à ``None`` quand aucune entité n'a répondu. Zéro
    voudrait dire « mesuré, et ça fait zéro »; c'est faux, et c'est exactement
    le genre de chiffre qu'on recopie dans un devis sans le vérifier.
    """
    payload = measure.to_dict()
    if measure.counted == 0:
        payload["total"] = None
        payload["note"] = (
            f"Aucune des entités examinées ne publie cette mesure sur le moteur "
            f"{engine}: le total est inconnu, et non nul. {_MEASURE_NOTE}"
        )
    elif not measure.complete:
        payload["note"] = (
            f"{measure.missing} entités examinées ne publient pas cette mesure: le "
            f"total ne porte que sur les {measure.counted} autres. {_MEASURE_NOTE}"
        )
    return payload


def _without_bbox_note(entities: Sequence[EntityInfo]) -> dict[str, Any]:
    """Signale les entités écartées d'une requête spatiale, faute de limites.

    Les placer au hasard ou les compter comme présentes serait inventer: elles
    sont donc absentes du résultat, et leur nombre est dit.
    """
    missing = sum(1 for entity in entities if entity.bbox is None)
    if not missing:
        return {}
    return {
        "without_bbox": missing,
        "without_bbox_note": (
            f"{missing} entités examinées n'ont pas de boîte englobante et ont été "
            "écartées de la sélection: le moteur ne sait pas où elles sont."
        ),
    }


def _bounded_values(payload: Any, ceiling: int = 2000) -> Any:
    """Tronque les chaînes d'une réponse venue du logiciel de CAO.

    Une commande native peut rendre un journal entier. Le compte-rendu doit
    rester lisible: la valeur est coupée, et la coupe est annoncée dans le texte
    lui-même plutôt que dans un champ que le modèle pourrait manquer.
    """
    if isinstance(payload, str) and len(payload) > ceiling:
        return payload[:ceiling] + f"… [tronqué, {len(payload)} caractères au total]"
    if isinstance(payload, dict):
        return {key: _bounded_values(value, ceiling) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_bounded_values(value, ceiling) for value in payload[:MAX_BATCH_ITEMS]]
    return payload


# ---------------------------------------------------------------------------
# Sélection et mise en forme des réponses
# ---------------------------------------------------------------------------


def _selector(args: dict[str, Any]) -> EntityFilter:
    """Filtre unifié, partagé par la lecture et la suppression."""
    handles = args.get("handles")
    if handles is not None:
        if not isinstance(handles, list) or not handles:
            raise InvalidParameter("handles doit être une liste non vide", field="handles")
        handles = tuple(
            _text(item, f"handles[{i}]", where="handles") for i, item in enumerate(handles)
        )

    window = args.get("window")
    if window is not None:
        if not isinstance(window, (list, tuple)) or len(window) != 4:
            raise InvalidParameter(
                "window doit valoir [xmin, ymin, xmax, ymax]", field="window", got=repr(window)
            )
        window = tuple(_number(v, f"window[{i}]", where="window") for i, v in enumerate(window))

    kind = args.get("type")
    if kind is not None and not isinstance(kind, str):
        raise InvalidParameter("type doit être une chaîne", field="type", got=repr(kind))

    layer = args.get("layer")
    if layer is not None and not isinstance(layer, str):
        raise InvalidParameter("layer doit être une chaîne", field="layer", got=repr(layer))

    return EntityFilter(
        layer=layer,
        kind=kind.upper() if isinstance(kind, str) else None,
        color=_opt_color(args),
        handles=handles,
        window=window,
    )


def _scope(args: dict[str, Any]) -> EntityFilter:
    """Portée d'une mesure ou d'une vérification: calque et type, rien d'autre.

    Volontairement plus étroit que ``_selector``. ``measure`` porte un champ
    ``window`` dont le sens est tout autre — c'est une question, pas un filtre
    de lecture — et les confondre ferait appliquer deux fois la même fenêtre,
    une fois au moteur et une fois à la mesure.
    """
    layer = args.get("layer")
    if layer is not None and not isinstance(layer, str):
        raise InvalidParameter("layer doit être une chaîne", field="layer", got=repr(layer))

    kind = args.get("type")
    if kind is not None and not isinstance(kind, str):
        raise InvalidParameter("type doit être une chaîne", field="type", got=repr(kind))

    return EntityFilter(
        layer=layer, kind=kind.upper() if isinstance(kind, str) else None
    )


def _selector_echo(selector: EntityFilter) -> dict[str, Any]:
    """Rappelle le filtre effectivement appliqué, sans les critères absents."""
    echo: dict[str, Any] = {}
    if selector.layer is not None:
        echo["layer"] = selector.layer
    if selector.kind is not None:
        echo["type"] = selector.kind
    if selector.color is not None:
        echo["color"] = selector.color
    if selector.handles is not None:
        echo["handles_count"] = len(selector.handles)
    if selector.window is not None:
        echo["window"] = list(selector.window)
    return echo


def _bounded(values: list[Any], name: str, limit: int) -> dict[str, Any]:
    """Tronque une liste en gardant le compte exact.

    Vaut pour des handles, des entités décrites, des lignes de nomenclature ou
    des défauts de plan: dans tous les cas, un dessin de dix mille objets
    saturerait le contexte du modèle sans rien lui apprendre de plus.
    """
    ceiling = max(1, min(int(limit), MAX_QUERY_LIMIT))
    if len(values) <= ceiling:
        return {name: values}
    return {
        name: values[:ceiling],
        f"{name}_truncated": True,
        f"{name}_total": len(values),
    }


def _rejected(index: int, kind: Any, exc: CadError) -> dict[str, Any]:
    """Échec survenu avant toute écriture, donc sans entité créée."""
    payload: dict[str, Any] = {"index": index, "stage": "build", "kind": kind}
    payload.update(exc.to_dict())
    return payload


def _failed(exc: CadError) -> ToolResult:
    """Transforme une erreur typée en résultat d'outil, jamais en succès."""
    return ToolResult(exc.to_dict(), is_error=True)
