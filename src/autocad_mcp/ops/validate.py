"""Détection des défauts d'un plan.

Un modèle qui dessine sans voir accumule des fautes invisibles: un contour de
pièce qui ne se referme pas et refuse la hachure, deux murs qui se croisent en
croix au lieu de se rejoindre, une ligne tracée deux fois parce que le plan a
été repris, un résidu d'un millième de millimètre, deux extrémités qui se
ratent d'un cheveu. Aucune de ces fautes ne lève d'erreur au dessin: elles se
découvrent au tracé, ou jamais.

Ce module les nomme. Il rend des :class:`Problem`, chacun portant un type, une
gravité, une localisation et un remède en une phrase, afin que le modèle puisse
se corriger seul au lieu de redessiner au hasard.

Comme :mod:`autocad_mcp.ops.query`, tout est **pur**: les contours, segments et
entités arrivent déjà lus, aucun backend n'est connu, et tout calcul
géométrique vient de :mod:`autocad_mcp.geometry`.

**La tolérance décide de tout.** À l'échelle du mètre, un écart d'un dixième de
millimètre est une jonction; à l'échelle du millimètre, c'est un trou. Aucune
valeur n'est donc codée en dur: soit l'appelant passe ``tol``, soit il passe
l'unité du document et ``Defaults(unit).tolerance`` s'applique.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Literal, TypeAlias

from ..errors import InvalidParameter
from ..geometry import EPS, BBox, bbox, bbox_intersects, distance, midpoint, segments_intersect
from ..model.ops import Point2
from ..units import Defaults, Unit
from .query import EntityLike

#: Gravité d'un défaut.
#:
#: ``"error"`` signale ce qui empêche un traitement ou ce qui est certainement
#: faux: un contour presque fermé, deux murs qui se croisent, un trou entre
#: deux extrémités. ``"warning"`` signale une suspicion à confirmer, que la
#: géométrie disponible ne permet pas de trancher.
Severity: TypeAlias = Literal["error", "warning"]

#: Type de défaut. La liste est fermée: un modèle peut aiguiller dessus.
ProblemKind: TypeAlias = Literal[
    "open_contour",
    "crossing_walls",
    "duplicate",
    "tiny_entity",
    "gap",
]

#: Part du plus grand écart d'un contour en deçà de laquelle un contour ouvert
#: est tenu pour un contour qu'on a voulu fermer. Sans dimension: c'est un
#: rapport, pas une longueur, donc il ne dépend pas de l'unité du document. Un
#: carré de cinq mètres dont les extrémités se ratent de vingt centimètres est
#: un contour raté; le même carré ouvert sur deux mètres est peut-être une
#: polyligne volontairement ouverte.
NEAR_CLOSED_RATIO = 0.05

__all__ = [
    "NEAR_CLOSED_RATIO",
    "Contour",
    "Problem",
    "ProblemKind",
    "Report",
    "Segment",
    "Severity",
    "find_crossing_walls",
    "find_duplicates",
    "find_gaps",
    "find_open_contours",
    "find_tiny_entities",
    "validate_plan",
]


# ---------------------------------------------------------------------------
# Ce qu'on examine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contour:
    """Suite de sommets susceptible de délimiter une surface.

    ``closed`` est la fermeture portée par l'entité elle-même, celle de
    ``AddPolyline(closed=True)``, et non le fait que le dernier sommet retombe
    sur le premier. ``ref`` désigne l'objet dans le document — un handle, un
    nom — afin que le rapport dise de quoi il parle.
    """

    points: tuple[Point2, ...]
    closed: bool = False
    ref: str = ""
    layer: str = ""


@dataclass(frozen=True, slots=True)
class Segment:
    """Axe d'un mur, ou tout segment droit qu'on veut confronter aux autres."""

    start: Point2
    end: Point2
    ref: str = ""
    layer: str = ""


# ---------------------------------------------------------------------------
# Ce qu'on rapporte
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    """Un défaut, localisé et assorti de son remède.

    ``remedy`` tient en une phrase et dit quoi faire, pas ce qui ne va pas:
    c'est la seule forme qu'un modèle peut exécuter sans interpréter.
    """

    kind: ProblemKind
    severity: Severity
    message: str
    remedy: str
    #: Où regarder dans le dessin. ``None`` quand le défaut n'a pas de point.
    location: Point2 | None = None
    #: Handles ou noms des objets concernés.
    refs: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "remedy": self.remedy,
        }
        if self.location is not None:
            payload["location"] = list(self.location)
        if self.refs:
            payload["refs"] = list(self.refs)
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True, slots=True)
class Report:
    """Rapport de validation d'un plan.

    Porte aussi les seuils employés: un rapport vide ne veut rien dire tant
    qu'on ne sait pas avec quelle tolérance il a été établi.
    """

    problems: tuple[Problem, ...]
    tolerance: float
    minimum_size: float
    #: Ce qui a été examiné, par famille. Un rapport vert sur zéro objet
    #: examiné n'est pas un plan correct, c'est un plan non examiné.
    checked: dict[str, int]

    @property
    def errors(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.severity == "error")

    @property
    def warnings(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.severity == "warning")

    @property
    def ok(self) -> bool:
        """Vrai si aucun défaut grave n'a été trouvé.

        Les suspicions n'invalident pas un plan: elles demandent un regard.
        """
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "tolerance": self.tolerance,
            "minimum_size": self.minimum_size,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "problems": [p.to_dict() for p in self.problems],
        }


# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------


def _positive(value: float, name: str) -> float:
    number = float(value)
    if number <= 0.0:
        raise InvalidParameter(f"{name} doit être strictement positif", value=number)
    return number


def _box_of(points: Sequence[Point2]) -> BBox:
    return bbox(points)


def _diagonal(box: BBox) -> float:
    """Taille d'une boîte, mesurée sur sa diagonale."""
    return distance((box[0], box[1]), (box[2], box[3]))


def _entity_box(entity: EntityLike) -> BBox | None:
    box = entity.bbox
    if box is None:
        return None
    return bbox(((box[0], box[1]), (box[2], box[3])))


def _same_box(a: BBox, b: BBox, tol: float) -> bool:
    """Vrai si deux boîtes coïncident coin par coin, à ``tol`` près."""
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _label(ref: str, fallback: str) -> str:
    return ref if ref else fallback


# ---------------------------------------------------------------------------
# Contours non fermés
# ---------------------------------------------------------------------------


def find_open_contours(
    polylines: Iterable[Contour],
    *,
    tol: float,
    near_ratio: float = NEAR_CLOSED_RATIO,
) -> list[Problem]:
    """Contours censés délimiter une surface et qui ne se referment pas.

    Un contour déclaré fermé est correct par construction. Un contour dont le
    dernier sommet retombe sur le premier à ``tol`` près se referme aussi: le
    sommet surnuméraire relève de ``geometry.close_ring``, au tracé, pas d'un
    défaut de plan.

    Reste le cas qui coûte cher: le contour ouvert. Sa gravité dépend de
    l'écart. Un écart inférieur à ``near_ratio`` fois la diagonale du contour
    trahit une fermeture manquée et vaut une erreur; au-delà, la polyligne est
    peut-être ouverte à dessein et n'est signalée qu'en avertissement.

    Les suites de moins de trois sommets sont ignorées: un segment n'est pas un
    contour raté, c'est un segment.
    """
    tolerance = _positive(tol, "La tolérance")
    ratio = _positive(near_ratio, "Le rapport de fermeture")
    problems: list[Problem] = []
    for index, contour in enumerate(polylines):
        points = tuple(contour.points)
        if contour.closed or len(points) < 3:
            continue
        gap = distance(points[0], points[-1])
        if gap <= tolerance:
            continue
        size = _diagonal(_box_of(points))
        severity: Severity = "error" if gap <= ratio * size else "warning"
        problems.append(
            Problem(
                kind="open_contour",
                severity=severity,
                message=(
                    f"Contour non fermé: ses deux extrémités sont distantes de {gap:g}"
                ),
                remedy=(
                    "Fermer la polyligne, ou ramener son dernier sommet sur le premier, "
                    "sans quoi la surface ne sera ni hachurable ni mesurable."
                ),
                location=midpoint(points[0], points[-1]),
                refs=(_label(contour.ref, f"contour[{index}]"),),
                details={
                    "gap": gap,
                    "size": size,
                    "layer": contour.layer,
                    "vertices": len(points),
                },
            )
        )
    return problems


# ---------------------------------------------------------------------------
# Murs qui se croisent
# ---------------------------------------------------------------------------


def find_crossing_walls(segments: Iterable[Segment], *, tol: float) -> list[Problem]:
    """Murs qui se traversent au lieu de se rejoindre, avec le point de croisée.

    Une jonction en L ou en T est légitime: l'intersection y tombe sur une
    extrémité d'au moins un des deux murs. Le défaut visé est la croix, où
    l'intersection est intérieure aux deux segments — le refend qui dépasse
    l'enveloppe, les deux murs qui se chevauchent au coin.

    Deux murs strictement parallèles, même superposés, ne sont pas rapportés
    ici: leur intersection n'est pas un point, et ``find_duplicates`` est le
    bon endroit pour la superposition. Les segments de longueur nulle sont
    écartés, ``find_tiny_entities`` les prend en charge.
    """
    tolerance = _positive(tol, "La tolérance")
    kept: list[tuple[int, Segment, BBox]] = []
    for index, segment in enumerate(segments):
        if distance(segment.start, segment.end) <= tolerance:
            continue
        kept.append((index, segment, _box_of((segment.start, segment.end))))

    problems: list[Problem] = []
    for (i, a, box_a), (j, b, box_b) in combinations(kept, 2):
        if not bbox_intersects(box_a, box_b, tol=tolerance):
            continue
        point = segments_intersect(a.start, a.end, b.start, b.end, tol=tolerance)
        if point is None:
            continue
        ends = (a.start, a.end, b.start, b.end)
        if any(distance(point, end) <= tolerance for end in ends):
            # Jonction franche en L ou en T: c'est un assemblage, pas un défaut.
            continue
        problems.append(
            Problem(
                kind="crossing_walls",
                severity="error",
                message=(
                    f"Deux murs se croisent en ({point[0]:g}, {point[1]:g}) "
                    "au lieu de se rejoindre"
                ),
                remedy=(
                    "Ajuster les deux murs sur leur intersection, ou arrêter l'un "
                    "contre l'autre pour former une jonction en T."
                ),
                location=point,
                refs=(_label(a.ref, f"segment[{i}]"), _label(b.ref, f"segment[{j}]")),
                details={"layers": [a.layer, b.layer]},
            )
        )
    return problems


# ---------------------------------------------------------------------------
# Doublons
# ---------------------------------------------------------------------------


def find_duplicates(entities: Iterable[EntityLike], *, tol: float) -> list[Problem]:
    """Entités superposées, défaut classique d'un plan repris deux fois.

    Deux entités sont tenues pour superposées quand elles ont le même type, le
    même calque, la même description ``extra`` et la même boîte englobante à
    ``tol`` près. Le doublon est invisible à l'écran, double les quantités
    d'une nomenclature et fait échouer les hachures.

    La boîte englobante ne décrit pas entièrement une géométrie: les deux
    diagonales d'un rectangle partagent la même boîte sans être superposées.
    Le défaut est donc rapporté en avertissement, comme une suspicion à
    confirmer, jamais comme une certitude. Les entités dépourvues de boîte sont
    écartées, faute de pouvoir en juger.
    """
    tolerance = _positive(tol, "La tolérance")
    buckets: dict[tuple[str, str], list[tuple[EntityLike, BBox]]] = {}
    for entity in entities:
        box = _entity_box(entity)
        if box is None:
            continue
        buckets.setdefault((entity.kind, entity.layer), []).append((entity, box))

    problems: list[Problem] = []
    for (kind, layer), group in sorted(buckets.items()):
        seen: set[int] = set()
        for position, (entity, box) in enumerate(group):
            if position in seen:
                continue
            cluster = [entity]
            for other_position in range(position + 1, len(group)):
                if other_position in seen:
                    continue
                other, other_box = group[other_position]
                if _same_box(box, other_box, tolerance) and dict(entity.extra) == dict(
                    other.extra
                ):
                    seen.add(other_position)
                    cluster.append(other)
            if len(cluster) < 2:
                continue
            problems.append(
                Problem(
                    kind="duplicate",
                    severity="warning",
                    message=f"{len(cluster)} entités {kind} superposées sur le calque {layer}",
                    remedy="Vérifier ces entités superposées et n'en conserver qu'une.",
                    location=midpoint((box[0], box[1]), (box[2], box[3])),
                    refs=tuple(e.handle for e in cluster),
                    details={"type": kind, "layer": layer, "count": len(cluster)},
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Résidus
# ---------------------------------------------------------------------------


def find_tiny_entities(entities: Iterable[EntityLike], minimum: float) -> list[Problem]:
    """Entités trop petites pour être intentionnelles.

    La taille mesurée est la diagonale de la boîte englobante, ce qui couvre
    aussi bien le segment d'un millième de millimètre que le cercle de rayon
    nul. Ces résidus viennent d'un clic de trop ou d'un tracé repris; ils
    rendent une sélection imprévisible et faussent les comptes.

    ``minimum`` est une longueur dans l'unité du document, sans valeur par
    défaut: il n'existe pas de « petit » universel entre un plan en millimètres
    et un plan de masse en mètres. Les entités sans boîte englobante sont
    écartées. Lève ``InvalidParameter`` si ``minimum`` n'est pas positif.
    """
    threshold = _positive(minimum, "La taille minimale")
    problems: list[Problem] = []
    for entity in entities:
        box = _entity_box(entity)
        if box is None:
            continue
        size = _diagonal(box)
        if size >= threshold:
            continue
        problems.append(
            Problem(
                kind="tiny_entity",
                severity="warning",
                message=(
                    f"Entité {entity.kind} de taille négligeable: {size:g} "
                    f"pour un minimum de {threshold:g}"
                ),
                remedy=(
                    "Supprimer ce résidu, ou lui rendre sa taille s'il a été réduit "
                    "par erreur."
                ),
                location=midpoint((box[0], box[1]), (box[2], box[3])),
                refs=(entity.handle,),
                details={"type": entity.kind, "layer": entity.layer, "size": size},
            )
        )
    return problems


# ---------------------------------------------------------------------------
# Trous entre extrémités
# ---------------------------------------------------------------------------


def find_gaps(
    segments: Iterable[Segment],
    *,
    tol: float,
    joined_tol: float = EPS,
) -> list[Problem]:
    """Extrémités assez proches pour être voulues jointives, mais qui ne le sont pas.

    C'est le défaut qui fait échouer un hachurage sans rien montrer: à l'écran
    le contour paraît continu, mais deux extrémités se ratent d'un dixième de
    millimètre et la surface reste ouverte.

    Deux seuils, parce qu'il en faut deux: ``tol`` est le rayon de recherche,
    au-delà duquel deux extrémités sont simplement distinctes, et ``joined_tol``
    la distance en deçà de laquelle elles sont déjà jointives. Un défaut est
    donc un écart dans ``]joined_tol, tol]``. La valeur par défaut de
    ``joined_tol`` ne couvre que le bruit d'arrondi de la virgule flottante,
    ce qui traite comme jointives les extrémités réellement confondues.

    Lève ``InvalidParameter`` si ``joined_tol`` n'est pas strictement inférieur
    à ``tol``, auquel cas aucun écart ne pourrait jamais être rapporté.
    """
    tolerance = _positive(tol, "La tolérance")
    joined = float(joined_tol)
    if joined < 0.0:
        raise InvalidParameter("La tolérance de jonction ne peut pas être négative", value=joined)
    if joined >= tolerance:
        raise InvalidParameter(
            "La tolérance de jonction doit être inférieure au rayon de recherche",
            joined_tol=joined,
            tol=tolerance,
        )

    endpoints: list[tuple[int, Segment, Point2, str]] = []
    for index, segment in enumerate(segments):
        if distance(segment.start, segment.end) <= tolerance:
            continue
        endpoints.append((index, segment, segment.start, "début"))
        endpoints.append((index, segment, segment.end, "fin"))

    problems: list[Problem] = []
    for (i, a, pa, side_a), (j, b, pb, side_b) in combinations(endpoints, 2):
        if i == j:
            continue
        gap = distance(pa, pb)
        if gap <= joined or gap > tolerance:
            continue
        problems.append(
            Problem(
                kind="gap",
                severity="error",
                message=(
                    f"Deux extrémités distantes de {gap:g} ne se rejoignent pas "
                    f"({side_a} et {side_b})"
                ),
                remedy=(
                    "Joindre ces deux extrémités sur un même point, sinon le contour "
                    "restera ouvert et refusera la hachure."
                ),
                location=midpoint(pa, pb),
                refs=(_label(a.ref, f"segment[{i}]"), _label(b.ref, f"segment[{j}]")),
                details={"gap": gap, "a": list(pa), "b": list(pb)},
            )
        )
    return problems


# ---------------------------------------------------------------------------
# Rapport complet
# ---------------------------------------------------------------------------


def validate_plan(
    *,
    entities: Iterable[EntityLike] = (),
    contours: Iterable[Contour] = (),
    segments: Iterable[Segment] = (),
    unit: Unit | None = None,
    tol: float | None = None,
    minimum_size: float | None = None,
    near_ratio: float = NEAR_CLOSED_RATIO,
) -> Report:
    """Passe tous les contrôles et rend un rapport structuré.

    Les trois familles sont indépendantes: on peut ne valider que des contours,
    que des murs, ou tout à la fois. Le rapport dit ce qu'il a examiné, avec
    quels seuils, et range les erreurs avant les avertissements.

    Les seuils viennent de l'appelant ou de l'unité du document:
    ``tol`` vaut par défaut ``Defaults(unit).tolerance``, et ``minimum_size``
    vaut par défaut cette même tolérance — une entité plus petite que la
    précision du document ne peut pas être intentionnelle.

    Lève ``InvalidParameter`` si ni ``tol`` ni ``unit`` n'est donné: il n'existe
    pas de tolérance universelle, et en choisir une en silence reviendrait à
    valider un plan au millimètre avec les seuils d'un plan de masse.
    """
    if tol is not None:
        tolerance = _positive(tol, "La tolérance")
    elif unit is not None:
        tolerance = _positive(Defaults(unit).tolerance, "La tolérance")
    else:
        raise InvalidParameter(
            "Tolérance indéterminée: passer tol, ou unit pour la déduire du document",
            supported=["tol", "unit"],
        )
    smallest = _positive(tolerance if minimum_size is None else minimum_size, "La taille minimale")

    entity_list = list(entities)
    contour_list = list(contours)
    segment_list = list(segments)

    problems: list[Problem] = []
    problems.extend(find_open_contours(contour_list, tol=tolerance, near_ratio=near_ratio))
    problems.extend(find_crossing_walls(segment_list, tol=tolerance))
    problems.extend(find_gaps(segment_list, tol=tolerance))
    problems.extend(find_duplicates(entity_list, tol=tolerance))
    problems.extend(find_tiny_entities(entity_list, smallest))

    # Tri stable: les erreurs d'abord, l'ordre de détection ensuite. Le modèle
    # lit le rapport de haut en bas et doit tomber sur ce qui bloque.
    ordered = sorted(problems, key=lambda p: 0 if p.severity == "error" else 1)

    return Report(
        problems=tuple(ordered),
        tolerance=tolerance,
        minimum_size=smallest,
        checked={
            "entities": len(entity_list),
            "contours": len(contour_list),
            "segments": len(segment_list),
        },
    )
