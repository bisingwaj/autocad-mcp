"""Mesures et requêtes spatiales sur un jeu d'entités déjà lu.

Ces fonctions sont **pures**: elles reçoivent les entités, elles ne les lisent
pas. La lecture appartient au backend, qui rend des ``EntityInfo``. Ce module
ne connaît ni AutoCAD, ni `ezdxf`, ni COM, et ignore jusqu'à l'existence des
backends: il travaille sur une forme structurelle, :class:`EntityLike`, que
``EntityInfo`` satisfait sans avoir à le déclarer.

Le problème traité est celui du contexte. Un dessin de cinquante mille entités
ne tient pas dans la fenêtre du modèle, et le lui déverser ne lui apprend rien.
:func:`summarize` rend à la place un état des lieux — combien, sur quels
calques, de quels types, entre quelles limites — et les autres fonctions
permettent de demander précisément ce qui occupe une zone, ou ce qui se trouve
près d'un point.

Trois conventions tiennent le module:

* **Aucune tolérance n'est codée en dur.** Toute comparaison passe par ``tol``,
  et l'appelant y met ``Defaults(unit).tolerance`` quand l'échelle du document
  compte.
* **Une mesure incomplète le dit.** :class:`Measure` porte le nombre d'entités
  qui ont répondu et le nombre de celles qui n'avaient pas l'information. Un
  total muet sur ce qu'il ignore serait un succès sans preuve.
* **Une entité sans boîte englobante n'est jamais devinée.** Elle est écartée
  des requêtes spatiales et comptée à part, jamais placée au hasard.

Tout calcul géométrique vient de :mod:`autocad_mcp.geometry`; rien n'est
recalculé ici.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, TypeVar

from ..errors import InvalidParameter
from ..geometry import EPS, BBox, bbox, bbox_contains, bbox_intersects, bbox_union, distance
from ..model.ops import Point2

#: Mode de sélection par fenêtre, au sens d'AutoCAD.
#:
#: ``"inside"`` ne retient que ce qui tient entièrement dans la fenêtre,
#: ``"crossing"`` retient en plus ce qui la traverse ou la touche.
WindowMode: TypeAlias = Literal["inside", "crossing"]

#: Clé de ``EntityInfo.extra`` portant une longueur, dans l'unité du document.
LENGTH_KEY = "length"
#: Clé de ``EntityInfo.extra`` portant une aire, dans l'unité du document au carré.
AREA_KEY = "area"

__all__ = [
    "AREA_KEY",
    "LENGTH_KEY",
    "EntityLike",
    "Measure",
    "QuantityRow",
    "Summary",
    "WindowMode",
    "bill_of_quantities",
    "group_by_layer",
    "group_by_type",
    "in_window",
    "nearest",
    "summarize",
    "total_area",
    "total_length",
]


class EntityLike(Protocol):
    """Ce que ce module attend d'une entité lue dans un document.

    C'est exactement la forme de ``backends.base.EntityInfo``, décrite ici de
    façon structurelle afin que la logique métier n'importe aucun backend. Les
    membres sont déclarés en lecture seule: une entité lue est un constat, pas
    un objet à modifier.
    """

    @property
    def handle(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def layer(self) -> str: ...

    @property
    def bbox(self) -> tuple[float, float, float, float] | None: ...

    @property
    def extra(self) -> Mapping[str, Any]: ...


#: Toute fonction qui rend des entités rend celles qu'on lui a données, sans
#: les convertir: le type concret de l'appelant traverse le module.
E = TypeVar("E", bound=EntityLike)


# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------


def _box(entity: EntityLike) -> BBox | None:
    """Boîte englobante normalisée d'une entité, ou ``None`` si elle en manque.

    Le passage par ``geometry.bbox`` remet les coins dans l'ordre: une boîte
    inversée venue d'un moteur tiers ferait autrement échouer toutes les
    comparaisons au lieu de donner une réponse.
    """
    box = entity.bbox
    if box is None:
        return None
    return bbox(((box[0], box[1]), (box[2], box[3])))


def _number(value: Any) -> float | None:
    """Convertit une valeur d'``extra`` en mesure utilisable, ou ``None``.

    Un booléen n'est pas une mesure, et un infini ou un ``NaN`` empoisonnerait
    un total entier: les deux sont refusés plutôt qu'additionnés.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _window_box(window: tuple[float, float, float, float]) -> BBox:
    """Normalise une fenêtre donnée par deux coins quelconques."""
    xmin, ymin, xmax, ymax = (float(v) for v in window)
    return bbox(((xmin, ymin), (xmax, ymax)))


def _distance_to_box(point: Point2, box: BBox) -> float:
    """Distance d'un point à une boîte, nulle si le point est dedans."""
    xmin, ymin, xmax, ymax = box
    px, py = float(point[0]), float(point[1])
    closest = (min(max(px, xmin), xmax), min(max(py, ymin), ymax))
    return distance((px, py), closest)


def _counted(pairs: Iterable[tuple[str, int]]) -> dict[str, int]:
    """Trie un comptage du plus fourni au moins fourni, puis par nom.

    L'ordre est déterministe, donc testable, et met en tête ce qui pèse dans
    le dessin.
    """
    return dict(sorted(pairs, key=lambda item: (-item[1], item[0])))


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measure:
    """Total d'une grandeur, accompagné de ce qu'il ignore.

    ``counted`` est le nombre d'entités qui portaient l'information, ``missing``
    le nombre de celles qui ne la portaient pas. Une nomenclature bâtie sur un
    total silencieusement partiel est pire qu'absente: elle a l'air juste.
    """

    total: float
    counted: int
    missing: int

    @property
    def complete(self) -> bool:
        """Vrai si toutes les entités examinées ont répondu."""
        return self.missing == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "counted": self.counted,
            "missing": self.missing,
            "complete": self.complete,
        }


def _measure(entities: Iterable[EntityLike], key: str) -> Measure:
    total = 0.0
    counted = 0
    missing = 0
    for entity in entities:
        value = _number(entity.extra.get(key))
        if value is None:
            missing += 1
        else:
            total += value
            counted += 1
    return Measure(total=total, counted=counted, missing=missing)


def total_length(entities: Iterable[EntityLike]) -> Measure:
    """Longueur cumulée des entités qui la déclarent, dans l'unité du document.

    L'information est lue dans ``extra[\"length\"]``, seul endroit où un backend
    peut la publier sans que la logique métier ait à reconstruire la géométrie.
    Les entités muettes ne sont pas estimées: elles sont comptées dans
    ``missing``, ce qui rend la lacune visible au lieu de la dissoudre dans un
    total.
    """
    return _measure(entities, LENGTH_KEY)


def total_area(entities: Iterable[EntityLike]) -> Measure:
    """Aire cumulée des entités qui la déclarent, lue dans ``extra[\"area\"]``.

    Même contrat que :func:`total_length`: rien n'est estimé, ce qui manque est
    compté.
    """
    return _measure(entities, AREA_KEY)


# ---------------------------------------------------------------------------
# Résumé
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Summary:
    """État des lieux d'un jeu d'entités, destiné au modèle.

    C'est ce qu'on renvoie au lieu du dessin entier: quelques dizaines de
    lignes qui disent ce qu'il y a, où, et dans quelles limites.
    """

    count: int
    by_layer: dict[str, int]
    by_type: dict[str, int]
    #: Limites globales, ``None`` si aucune entité n'a de boîte englobante.
    extents: BBox | None
    #: Entités dont le moteur n'a pas su donner les limites.
    without_bbox: int
    length: Measure
    area: Measure

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "count": self.count,
            "by_layer": self.by_layer,
            "by_type": self.by_type,
            "length": self.length.to_dict(),
            "area": self.area.to_dict(),
        }
        if self.extents is not None:
            payload["extents"] = list(self.extents)
        if self.without_bbox:
            payload["without_bbox"] = self.without_bbox
        return payload


def summarize(entities: Iterable[EntityLike]) -> Summary:
    """Résume un jeu d'entités: compte, calques, types, limites, quantités.

    Un seul parcours suffit, et la fonction accepte donc un itérateur. Les
    limites globales sont l'union des boîtes disponibles; les entités qui n'en
    ont pas sont comptées séparément plutôt qu'ignorées en silence.
    """
    count = 0
    layers: dict[str, int] = {}
    kinds: dict[str, int] = {}
    boxes: list[BBox] = []
    without_bbox = 0
    length_total = 0.0
    length_counted = 0
    area_total = 0.0
    area_counted = 0

    for entity in entities:
        count += 1
        layers[entity.layer] = layers.get(entity.layer, 0) + 1
        kinds[entity.kind] = kinds.get(entity.kind, 0) + 1
        box = _box(entity)
        if box is None:
            without_bbox += 1
        else:
            boxes.append(box)
        length = _number(entity.extra.get(LENGTH_KEY))
        if length is not None:
            length_total += length
            length_counted += 1
        area = _number(entity.extra.get(AREA_KEY))
        if area is not None:
            area_total += area
            area_counted += 1

    return Summary(
        count=count,
        by_layer=_counted(layers.items()),
        by_type=_counted(kinds.items()),
        extents=bbox_union(boxes) if boxes else None,
        without_bbox=without_bbox,
        length=Measure(length_total, length_counted, count - length_counted),
        area=Measure(area_total, area_counted, count - area_counted),
    )


# ---------------------------------------------------------------------------
# Requêtes spatiales
# ---------------------------------------------------------------------------


def in_window(
    entities: Iterable[E],
    window: tuple[float, float, float, float],
    *,
    mode: WindowMode = "inside",
    tol: float = EPS,
) -> list[E]:
    """Entités retenues par une fenêtre rectangulaire, à la mode d'AutoCAD.

    ``mode=\"inside\"`` ne garde que ce qui tient entièrement dans la fenêtre,
    ``mode=\"crossing\"`` garde en plus ce qui la traverse ou la touche. C'est
    la distinction de la sélection par fenêtre et de la sélection capturante,
    et elle change tout: sur un plan, la première rend les pièces d'une zone,
    la seconde rend aussi les murs qui la bordent.

    Les deux coins peuvent arriver dans n'importe quel ordre. La sélection
    porte sur les boîtes englobantes, donc une entité concave peut être retenue
    en capturante sans qu'aucun de ses traits ne coupe la fenêtre; les entités
    dépourvues de boîte sont écartées, faute de pouvoir en juger.

    Lève ``InvalidParameter`` si le mode est inconnu.
    """
    if mode not in ("inside", "crossing"):
        raise InvalidParameter(
            f"Mode de fenêtre inconnu: {mode!r}",
            supported=["inside", "crossing"],
        )
    box = _window_box(window)
    selected: list[E] = []
    for entity in entities:
        entity_box = _box(entity)
        if entity_box is None:
            continue
        retained = (
            bbox_contains(box, entity_box, tol=tol)
            if mode == "inside"
            else bbox_intersects(box, entity_box, tol=tol)
        )
        if retained:
            selected.append(entity)
    return selected


def nearest(
    entities: Iterable[E],
    point: Point2,
    *,
    limit: int = 10,
) -> list[tuple[E, float]]:
    """Les ``limit`` entités les plus proches d'un point, avec leur distance.

    La distance est celle du point à la boîte englobante de l'entité, donc nulle
    si le point tombe dedans. C'est une minoration de la distance réelle au
    trait, ce qui convient pour « qu'y a-t-il ici » et doit être su pour tout
    le reste.

    Le tri départage les distances égales par le handle, afin que deux appels
    identiques rendent le même ordre. Les entités sans boîte englobante sont
    écartées: les placer à distance nulle ou infinie serait inventer.

    Lève ``InvalidParameter`` si ``limit`` est nul ou négatif.
    """
    if limit <= 0:
        raise InvalidParameter("Le nombre de voisins demandés doit être positif", limit=limit)
    measured: list[tuple[E, float]] = []
    for entity in entities:
        box = _box(entity)
        if box is None:
            continue
        measured.append((entity, _distance_to_box(point, box)))
    measured.sort(key=lambda item: (item[1], item[0].handle))
    return measured[:limit]


# ---------------------------------------------------------------------------
# Regroupements et nomenclature
# ---------------------------------------------------------------------------


def group_by_layer(entities: Iterable[E]) -> dict[str, list[E]]:
    """Range les entités par calque, calques triés par nom.

    La comparaison est faite sur le nom exact. Aucune correspondance par
    sous-chaîne: c'est elle qui faisait autrefois attraper ``light`` par
    ``skylight`` et ``table`` par ``portable``.
    """
    groups: dict[str, list[E]] = {}
    for entity in entities:
        groups.setdefault(entity.layer, []).append(entity)
    return dict(sorted(groups.items()))


def group_by_type(entities: Iterable[E]) -> dict[str, list[E]]:
    """Range les entités par type, types triés par nom.

    Le type est celui rendu par le moteur, sans traduction: un backend qui dit
    ``LWPOLYLINE`` et un autre qui dit ``polyline`` forment deux groupes, et
    c'est honnête tant que la normalisation n'est pas décidée ailleurs.
    """
    groups: dict[str, list[E]] = {}
    for entity in entities:
        groups.setdefault(entity.kind, []).append(entity)
    return dict(sorted(groups.items()))


@dataclass(frozen=True, slots=True)
class QuantityRow:
    """Une ligne de nomenclature: un couple calque/type et ses quantités."""

    layer: str
    kind: str
    count: int
    length: Measure
    area: Measure

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "type": self.kind,
            "count": self.count,
            "length": self.length.to_dict(),
            "area": self.area.to_dict(),
        }


def bill_of_quantities(entities: Iterable[EntityLike]) -> list[QuantityRow]:
    """Quantités par calque et par type, base d'une nomenclature.

    Chaque ligne porte un compte, toujours exact, et deux mesures qui disent
    elles-mêmes ce qu'elles ignorent. Les lignes sont triées par calque puis
    par type, ce qui donne un tableau lisible et stable d'un appel à l'autre.
    """
    buckets: dict[tuple[str, str], list[EntityLike]] = {}
    for entity in entities:
        buckets.setdefault((entity.layer, entity.kind), []).append(entity)
    return [
        QuantityRow(
            layer=layer,
            kind=kind,
            count=len(group),
            length=total_length(group),
            area=total_area(group),
        )
        for (layer, kind), group in sorted(buckets.items())
    ]
