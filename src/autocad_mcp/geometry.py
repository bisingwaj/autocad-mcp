"""Géométrie plane du dessin technique.

Toutes les fonctions de ce module sont pures: elles prennent des nombres et des
tuples, elles retournent des nombres et des tuples. Aucune entrée-sortie, aucun
état global, aucun appel COM. C'est ce qui rend la logique métier testable sans
AutoCAD.

Trois conventions tiennent tout le module:

* **Les angles sont en radians**, mesurés depuis l'axe des X et croissants dans
  le sens direct, comme dans le DXF. Seule ``door_swing_arc`` accepte des degrés,
  et sa signature le dit (``opening_deg``).
* **La gauche est la gauche du sens de parcours.** La normale unitaire gauche
  d'un vecteur ``(vx, vy)`` est ``(-vy, vx)``. Un décalage positif va donc à
  gauche, un décalage négatif à droite. La notion reste valable pour un mur
  vertical, où la pente est infinie et où toute formule en ``dy/dx`` se casse.
* **Aucune tolérance n'est codée en dur.** Toute comparaison de flottants passe
  par ``EPS`` ou par le paramètre ``tol``. Les dessins vont du millimètre au
  kilomètre: à l'échelle du mm, ``EPS`` est trop fin pour juger deux points
  confondus, et l'appelant passe alors ``Defaults(unit).tolerance``.

Les cas dégénérés lèvent ``InvalidGeometry``. Aucune fonction ne renvoie
silencieusement une valeur fausse pour une entrée impossible. Le seul ``None``
du module est celui de ``segments_intersect``, où il signifie « ces deux
segments ne se croisent pas », ce qui est une réponse et non un échec.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Literal, TypeAlias

from .errors import InvalidGeometry, InvalidParameter
from .model.ops import Point2

#: Tolérance par défaut des comparaisons de flottants.
#:
#: Elle ne couvre que le bruit d'arrondi de la virgule flottante. Elle ne décide
#: pas de ce qui est « proche » dans un dessin: cela dépend de l'unité du
#: document, et l'appelant passe alors ``Defaults(unit).tolerance`` en ``tol``.
EPS: float = 1e-9

#: Boîte englobante alignée sur les axes: ``(xmin, ymin, xmax, ymax)``.
BBox: TypeAlias = tuple[float, float, float, float]

#: Sens d'ouverture d'un battant, vu depuis le gond, dans le sens direct pour
#: ``"left"`` et dans le sens horaire pour ``"right"``.
Hand: TypeAlias = Literal["left", "right"]

#: Baie percée dans un mur: ``(position, largeur)``. ``position`` est une
#: fraction de 0 à 1 de la longueur du mur et désigne le centre de la baie,
#: ``largeur`` est en unités de dessin.
Opening: TypeAlias = tuple[float, float]

__all__ = [
    "EPS",
    "BBox",
    "Hand",
    "Opening",
    "angle_of",
    "bbox",
    "bbox_contains",
    "bbox_intersects",
    "bbox_union",
    "close_ring",
    "distance",
    "door_swing_arc",
    "midpoint",
    "normalize",
    "normalize_angle",
    "offset_polyline_miter",
    "offset_segment",
    "perpendicular",
    "polygon_area",
    "polygon_is_clockwise",
    "rectangle_points",
    "segments_intersect",
    "split_run_by_openings",
    "thick_segment_outline",
    "wall_band",
]


# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------


def _pt(p: Point2) -> Point2:
    """Normalise un point en tuple de flottants."""
    return (float(p[0]), float(p[1]))


def _signed_area(points: Sequence[Point2]) -> float:
    """Aire signée par la formule du lacet.

    Positive dans le sens antihoraire. Un contour explicitement fermé, dont le
    dernier sommet répète le premier, donne le même résultat qu'un contour
    ouvert: le terme surnuméraire est nul.
    """
    total = 0.0
    count = len(points)
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _require_ring(points: Sequence[Point2], what: str) -> tuple[Point2, ...]:
    """Valide qu'une suite de sommets peut former un polygone."""
    ring = tuple(_pt(p) for p in points)
    if len(ring) < 3:
        raise InvalidGeometry(
            f"{what} exige au moins trois sommets",
            vertex_count=len(ring),
        )
    return ring


def _check_box(box: BBox) -> BBox:
    """Valide une boîte englobante et la normalise en flottants."""
    xmin, ymin, xmax, ymax = (float(v) for v in box)
    if xmin > xmax or ymin > ymax:
        raise InvalidGeometry("Boîte englobante inversée", box=(xmin, ymin, xmax, ymax))
    return (xmin, ymin, xmax, ymax)


def normalize_angle(angle: float) -> float:
    """Ramène un angle en radians dans l'intervalle ``[0, 2π)``.

    Un arc dont l'angle de départ vaut ``-π/2`` et un arc partant de ``3π/2``
    sont le même arc. Le module ne renvoie jamais que la seconde forme, ce qui
    rend les comparaisons et les tests déterministes.
    """
    value = math.fmod(float(angle), math.tau)
    if value < 0.0:
        value += math.tau
    if value >= math.tau:  # garde-fou contre l'arrondi de fmod près de 2π
        return 0.0
    return value


# ---------------------------------------------------------------------------
# Mesures élémentaires
# ---------------------------------------------------------------------------


def distance(a: Point2, b: Point2) -> float:
    """Distance euclidienne entre deux points."""
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def midpoint(a: Point2, b: Point2) -> Point2:
    """Milieu du segment ``ab``. Défini même si les deux points sont confondus."""
    return ((float(a[0]) + float(b[0])) / 2.0, (float(a[1]) + float(b[1])) / 2.0)


def angle_of(a: Point2, b: Point2, *, tol: float = EPS) -> float:
    """Angle du vecteur ``a → b``, en radians dans ``[0, 2π)``.

    Lève ``InvalidGeometry`` si les deux points sont confondus à ``tol`` près:
    la direction est alors indéfinie, et renvoyer zéro produirait un mur orienté
    vers l'est sans que rien ne le signale.
    """
    dx = float(b[0]) - float(a[0])
    dy = float(b[1]) - float(a[1])
    if math.hypot(dx, dy) <= tol:
        raise InvalidGeometry(
            "Angle indéfini: les deux points sont confondus",
            a=_pt(a),
            b=_pt(b),
            tol=tol,
        )
    return normalize_angle(math.atan2(dy, dx))


def normalize(vx: float, vy: float, *, tol: float = EPS) -> Point2:
    """Vecteur unitaire colinéaire à ``(vx, vy)``.

    Lève ``InvalidGeometry`` si le vecteur est nul à ``tol`` près.
    """
    length = math.hypot(float(vx), float(vy))
    if length <= tol:
        raise InvalidGeometry(
            "Vecteur nul: direction indéfinie",
            vector=(float(vx), float(vy)),
            length=length,
            tol=tol,
        )
    return (float(vx) / length, float(vy) / length)


def perpendicular(vx: float, vy: float, *, tol: float = EPS) -> Point2:
    """Normale unitaire **gauche** de ``(vx, vy)``, soit ``(-vy, vx)`` normalisé.

    Pour un vecteur dirigé vers l'est, la normale pointe vers le nord. Pour un
    mur vertical dirigé vers le nord, elle pointe vers l'ouest.
    """
    ux, uy = normalize(vx, vy, tol=tol)
    return (-uy, ux)


# ---------------------------------------------------------------------------
# Segments et murs
# ---------------------------------------------------------------------------


def _segment_direction(start: Point2, end: Point2, tol: float) -> tuple[float, float]:
    """Vecteur unitaire du segment, avec message d'erreur propre au segment."""
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    length = math.hypot(dx, dy)
    if length <= tol:
        raise InvalidGeometry(
            "Segment dégénéré: longueur nulle",
            start=_pt(start),
            end=_pt(end),
            length=length,
            tol=tol,
        )
    return (dx / length, dy / length)


def offset_segment(
    start: Point2,
    end: Point2,
    distance: float,
    *,
    tol: float = EPS,
) -> tuple[Point2, Point2]:
    """Segment parallèle décalé de ``distance``.

    Le décalage est perpendiculaire au segment. Une valeur positive place le
    résultat à **gauche** du sens de parcours ``start → end``, une valeur
    négative à droite. Le segment retourné a la même longueur et la même
    direction que l'original, mur vertical compris.

    Lève ``InvalidGeometry`` si le segment est de longueur nulle à ``tol`` près.
    """
    ux, uy = _segment_direction(start, end, tol)
    nx, ny = -uy, ux
    shift = float(distance)
    return (
        (float(start[0]) + nx * shift, float(start[1]) + ny * shift),
        (float(end[0]) + nx * shift, float(end[1]) + ny * shift),
    )


def thick_segment_outline(
    start: Point2,
    end: Point2,
    thickness: float,
    *,
    tol: float = EPS,
) -> tuple[Point2, Point2, Point2, Point2]:
    """Les quatre sommets du rectangle d'un mur épais.

    Le mur est centré sur l'axe ``start → end``: chaque face est à
    ``thickness / 2`` de l'axe. Les sommets sont rendus dans le sens
    antihoraire, en partant du côté droit du sens de parcours, et **sans
    répéter le premier sommet**: la suite est prête à devenir une polyligne
    fermée, c'est-à-dire une entité unique, sélectionnable d'un clic,
    hachurable et dont l'aire est calculable. Le code historique traçait à la
    place quatre segments indépendants, qui ne formaient un mur que pour l'œil.

    Lève ``InvalidGeometry`` si le segment est dégénéré ou si l'épaisseur n'est
    pas strictement positive à ``tol`` près.
    """
    width = float(thickness)
    if width <= tol:
        raise InvalidGeometry(
            "Épaisseur de mur nulle ou négative",
            thickness=width,
            tol=tol,
        )
    ux, uy = _segment_direction(start, end, tol)
    nx, ny = -uy, ux  # normale gauche
    half = width / 2.0
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    return (
        (sx - nx * half, sy - ny * half),
        (ex - nx * half, ey - ny * half),
        (ex + nx * half, ey + ny * half),
        (sx + nx * half, sy + ny * half),
    )


def rectangle_points(
    corner1: Point2,
    corner2: Point2,
    *,
    tol: float = EPS,
) -> tuple[Point2, Point2, Point2, Point2]:
    """Les quatre sommets d'un rectangle aligné sur les axes, en antihoraire.

    Les deux points donnés sont les extrémités d'une diagonale quelconque. Le
    résultat ne dépend pas de l'ordre dans lequel ils arrivent: les quatre
    façons de désigner le même rectangle produisent la même suite, en partant
    du coin inférieur gauche.

    Lève ``InvalidGeometry`` si le rectangle est plat, c'est-à-dire si sa
    largeur ou sa hauteur est nulle à ``tol`` près.
    """
    x1, y1 = _pt(corner1)
    x2, y2 = _pt(corner2)
    xmin, xmax = (x1, x2) if x1 <= x2 else (x2, x1)
    ymin, ymax = (y1, y2) if y1 <= y2 else (y2, y1)
    if xmax - xmin <= tol or ymax - ymin <= tol:
        raise InvalidGeometry(
            "Rectangle dégénéré: largeur ou hauteur nulle",
            width=xmax - xmin,
            height=ymax - ymin,
            tol=tol,
        )
    return ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))


# ---------------------------------------------------------------------------
# Murs composés: raccords d'angle et baies
# ---------------------------------------------------------------------------


def _clean_polyline(
    points: Sequence[Point2],
    *,
    closed: bool,
    tol: float,
    what: str,
) -> tuple[Point2, ...]:
    """Suite de sommets utilisable comme axe de polyligne.

    Les points consécutifs confondus à ``tol`` près sont supprimés: ils ne
    portent aucune direction, et un segment de longueur nulle ferait échouer
    tout le calcul de raccord. Pour un axe fermé, un dernier sommet qui répète
    le premier est également supprimé, la fermeture étant portée par l'entité.

    Lève ``InvalidGeometry`` s'il reste moins de deux sommets distincts, ou
    moins de trois pour un axe fermé.
    """
    cleaned: list[Point2] = []
    for point in points:
        current = _pt(point)
        if cleaned and math.hypot(current[0] - cleaned[-1][0], current[1] - cleaned[-1][1]) <= tol:
            continue
        cleaned.append(current)
    if closed:
        while (
            len(cleaned) >= 2
            and math.hypot(cleaned[-1][0] - cleaned[0][0], cleaned[-1][1] - cleaned[0][1]) <= tol
        ):
            cleaned.pop()
    minimum = 3 if closed else 2
    if len(cleaned) < minimum:
        raise InvalidGeometry(
            f"{what} exige au moins {minimum} sommets distincts",
            vertex_count=len(cleaned),
            closed=closed,
            tol=tol,
        )
    return tuple(cleaned)


def _miter_joint(
    vertex: Point2,
    normal_in: Point2,
    normal_out: Point2,
    shift: float,
    miter_limit: float,
) -> tuple[Point2, ...]:
    """Sommet(s) du raccord entre deux segments décalés adjacents.

    ``normal_in`` et ``normal_out`` sont les normales gauches unitaires des
    segments entrant et sortant. Le point rendu est l'**intersection** des deux
    droites décalées, c'est-à-dire l'unique point situé à ``shift`` de chacune
    des deux droites d'axe. Il vaut ``vertex + shift * m`` avec

        m = (n1 + n2) / (1 + n1·n2)

    qui vérifie ``m·n1 = m·n2 = 1`` par construction. Le rapport d'onglet
    ``|m| = 1 / cos(θ/2)``, où ``θ`` est la déviation entre les deux segments,
    vaut 1 pour deux segments colinéaires — la formule redonne alors le simple
    décalage, sans cas particulier — et tend vers l'infini quand la polyligne
    se replie sur elle-même.

    Au-delà de ``miter_limit`` fois la distance de décalage, la pointe est
    rabattue en biseau: deux points au lieu d'un, l'extrémité du premier
    segment décalé puis l'origine du second. Le test porte sur la longueur
    d'onglet, qui ne dépend pas du côté du décalage: un angle trop aigu est
    biseauté aussi bien sur sa face saillante que sur sa face rentrante. C'est
    délibéré, et c'est ce qui garde les deux anneaux d'un mur en correspondance
    sommet pour sommet.
    """
    cos_turn = normal_in[0] * normal_out[0] + normal_in[1] * normal_out[1]
    cos_turn = min(1.0, max(-1.0, cos_turn))  # l'arrondi peut sortir de [-1, 1]
    half_cos = math.sqrt((1.0 + cos_turn) / 2.0)  # cos(θ/2), inverse du rapport
    if half_cos * miter_limit < 1.0:
        return (
            (vertex[0] + normal_in[0] * shift, vertex[1] + normal_in[1] * shift),
            (vertex[0] + normal_out[0] * shift, vertex[1] + normal_out[1] * shift),
        )
    # half_cos >= 1 / miter_limit > 0, donc le dénominateur 2 * half_cos² aussi.
    denom = 1.0 + cos_turn
    mx = (normal_in[0] + normal_out[0]) / denom
    my = (normal_in[1] + normal_out[1]) / denom
    return ((vertex[0] + mx * shift, vertex[1] + my * shift),)


def offset_polyline_miter(
    points: Sequence[Point2],
    distance: float,
    *,
    closed: bool = False,
    miter_limit: float = 4.0,
    tol: float = EPS,
) -> tuple[Point2, ...]:
    """Décale une polyligne entière, sommets raccordés par intersection.

    Chaque sommet du résultat est l'**intersection des deux segments décalés
    adjacents**, et non le sommet d'origine translaté. C'est toute la
    différence: un mur en L décalé sommet par sommet laisse ses deux faces se
    croiser, ce qui se voit au rendu sous la forme d'un petit carré au coin,
    alors que l'intersection donne un angle net.

    ``distance`` positif place le résultat à **gauche** du sens de parcours,
    négatif à droite, comme ``offset_segment``. Les angles sortants et les
    angles rentrants sont traités par la même formule: sur un angle rentrant,
    l'intersection est simplement située du côté concave.

    ``miter_limit`` est le rapport maximal entre la longueur d'onglet et
    ``abs(distance)``. Sur un angle très aigu la pointe part à l'infini; au-delà
    de la limite elle est rabattue en biseau, qui rend deux sommets au lieu
    d'un. La valeur 4 est celle du DXF et de la plupart des traceurs: elle
    laisse passer tous les angles jusqu'à 29° environ.

    Deux segments colinéaires n'ont pas d'intersection: la formule d'onglet y
    redonne exactement le décalage simple, et le sommet est conservé. Les points
    consécutifs confondus à ``tol`` près sont ignorés.

    Avec ``closed=True``, le dernier segment rejoint le premier et chaque sommet
    de l'axe donne un sommet du résultat: le contour rendu a autant de sommets
    que l'axe, sauf aux angles biseautés, qui en donnent deux.

    Lève ``InvalidGeometry`` s'il reste moins de deux sommets distincts, moins
    de trois pour un axe fermé, et ``InvalidParameter`` si ``miter_limit`` est
    inférieur à 1, ce qui biseauterait jusqu'aux segments alignés.
    """
    limit = float(miter_limit)
    if limit < 1.0:
        raise InvalidParameter(
            "Limite d'onglet inférieure à 1: aucun angle ne serait mitré",
            miter_limit=limit,
        )
    axis = _clean_polyline(points, closed=closed, tol=tol, what="Le décalage d'une polyligne")
    shift = float(distance)
    count = len(axis)

    span = count if closed else count - 1
    normals: list[Point2] = []
    for index in range(span):
        ux, uy = _segment_direction(axis[index], axis[(index + 1) % count], tol)
        normals.append((-uy, ux))  # normale gauche du segment

    result: list[Point2] = []
    if closed:
        joints = range(count)
    else:
        first = axis[0]
        result.append((first[0] + normals[0][0] * shift, first[1] + normals[0][1] * shift))
        joints = range(1, count - 1)
    for index in joints:
        result.extend(
            _miter_joint(
                axis[index],
                normals[index - 1],  # -1 boucle sur le dernier segment si fermé
                normals[index % span],
                shift,
                limit,
            )
        )
    if not closed:
        last = axis[-1]
        result.append((last[0] + normals[-1][0] * shift, last[1] + normals[-1][1] * shift))
    return tuple(result)


def wall_band(
    points: Sequence[Point2],
    thickness: float,
    *,
    closed: bool = False,
    miter_limit: float = 4.0,
    tol: float = EPS,
) -> tuple[tuple[Point2, ...], ...]:
    """Ruban d'un mur d'épaisseur constante centré sur son axe.

    Le résultat est toujours un tuple de contours fermés, chacun sans sommet
    dupliqué, prêt à devenir une polyligne fermée. Le nombre de contours dépend
    de la nature du mur, jamais du contenu:

    * ``closed=False`` rend **un seul** contour, ``(contour,)``: la face gauche
      parcourue dans le sens de l'axe, l'about de fin, la face droite parcourue
      à l'envers, puis l'about de début que porte la fermeture du contour. Les
      abouts sont droits, perpendiculaires à l'axe.
    * ``closed=True`` rend **deux** contours, ``(exterieur, interieur)``: le mur
      d'enceinte d'un bâtiment vu en plan. Le côté extérieur est déterminé par
      l'orientation de l'axe, de sorte qu'un relevé horaire et un relevé
      antihoraire du même bâtiment donnent le même couple d'anneaux.

    Les angles sont raccordés par ``offset_polyline_miter``: pour un carré d'axe
    de côté ``c`` et une épaisseur ``e``, l'anneau extérieur est exactement le
    carré de côté ``c + e`` et l'anneau intérieur celui de côté ``c - e``. Des
    rectangles indépendants, un par mur, donneraient à la place quatre
    recouvrements de ``e`` par ``e`` aux coins.

    Lève ``InvalidGeometry`` si l'épaisseur n'est pas strictement positive à
    ``tol`` près, s'il reste moins de deux sommets distincts — trois pour un
    axe fermé — ou si un axe fermé est d'aire nulle, auquel cas l'extérieur
    n'existe pas.
    """
    width = float(thickness)
    if width <= tol:
        raise InvalidGeometry(
            "Épaisseur de mur nulle ou négative",
            thickness=width,
            tol=tol,
        )
    axis = _clean_polyline(points, closed=closed, tol=tol, what="Le ruban d'un mur")
    half = width / 2.0
    left = offset_polyline_miter(axis, half, closed=closed, miter_limit=miter_limit, tol=tol)
    right = offset_polyline_miter(axis, -half, closed=closed, miter_limit=miter_limit, tol=tol)
    if not closed:
        return (left + tuple(reversed(right)),)
    # Sur un axe antihoraire, la gauche du sens de parcours regarde l'intérieur.
    if polygon_is_clockwise(axis, tol=tol):
        return (left, right)
    return (right, left)


def split_run_by_openings(
    start: Point2,
    end: Point2,
    openings: Sequence[Opening],
    *,
    tol: float = EPS,
) -> tuple[tuple[Point2, Point2], ...]:
    """Découpe un mur en tronçons pleins, de part et d'autre de ses baies.

    ``openings`` est une suite de ``(position, largeur)``. ``position`` est une
    fraction de 0 à 1 de la longueur du segment et désigne le **centre** de la
    baie, ``largeur`` est exprimée en unités de dessin. Une porte au milieu d'un
    mur de 10, large de 2, c'est ``(0.5, 2.0)``.

    Le résultat est la suite des tronçons pleins restants, dans le sens de
    parcours ``start → end``. Une baie qui déborde du segment est rognée à ses
    bornes; des baies qui se chevauchent ou s'emboîtent sont fusionnées, ce qui
    interdit le tronçon inversé qu'une soustraction naïve produirait. Un tronçon
    résiduel de longueur inférieure ou égale à ``tol`` est supprimé plutôt que
    rendu dégénéré: un mur de quelques microns n'est pas un mur, et il ferait
    échouer les hachures. Une baie qui couvre tout le mur rend un tuple vide,
    qui est une réponse — il n'y a plus de maçonnerie — et non un échec.

    Lève ``InvalidGeometry`` si le segment est de longueur nulle à ``tol`` près
    ou si une largeur de baie n'est pas strictement positive, et
    ``InvalidParameter`` si une position sort de ``[0, 1]``.
    """
    ux, uy = _segment_direction(start, end, tol)
    sx, sy = _pt(start)
    length = distance(start, end)

    spans: list[tuple[float, float]] = []
    for index, opening in enumerate(openings):
        position = float(opening[0])
        opening_width = float(opening[1])
        if not 0.0 <= position <= 1.0:
            raise InvalidParameter(
                "Position de baie hors de [0, 1]",
                index=index,
                position=position,
            )
        if opening_width <= tol:
            raise InvalidGeometry(
                "Largeur de baie nulle ou négative",
                index=index,
                width=opening_width,
                tol=tol,
            )
        center = position * length
        low = max(0.0, center - opening_width / 2.0)
        high = min(length, center + opening_width / 2.0)
        if high - low > tol:  # baie entièrement hors du mur après rognage
            spans.append((low, high))
    spans.sort()

    runs: list[tuple[float, float]] = []
    cursor = 0.0
    for low, high in spans:
        if low - cursor > tol:
            runs.append((cursor, low))
        # max: une baie emboîtée dans la précédente ne fait pas reculer le bord.
        cursor = max(cursor, high)
    if length - cursor > tol:
        runs.append((cursor, length))

    return tuple(
        ((sx + ux * begin, sy + uy * begin), (sx + ux * finish, sy + uy * finish))
        for begin, finish in runs
    )


# ---------------------------------------------------------------------------
# Portes
# ---------------------------------------------------------------------------


def door_swing_arc(
    hinge: Point2,
    leaf_end: Point2,
    opening_deg: float = 90.0,
    hand: Hand = "left",
    *,
    tol: float = EPS,
) -> tuple[Point2, float, float, float]:
    """Arc de battant d'une porte, en radians.

    Renvoie ``(center, radius, start_angle, end_angle)``, directement
    consommable par ``AddArc``. Le centre est le gond, le rayon est la largeur
    du battant, et les deux angles sont dans ``[0, 2π)``.

    ``hinge → leaf_end`` est le battant **fermé**, donc couché dans l'axe du mur
    porteur. L'arc part de cette position et balaye ``opening_deg`` degrés. Les
    arcs tournant dans le sens direct, ``end_angle`` est toujours atteint depuis
    ``start_angle`` en tournant dans le sens direct, quel que soit le sens
    d'ouverture:

    * ``hand="left"``: le battant s'ouvre dans le sens direct, l'arc part donc
      de l'axe du mur et finit ``opening_deg`` plus loin ;
    * ``hand="right"``: le battant s'ouvre dans le sens horaire, l'arc part
      donc ``opening_deg`` avant l'axe du mur et finit sur cet axe.

    C'est le correctif du bug historique. L'ancien code appelait
    ``create_arc(start, width, 0, 90)``: l'arc partait toujours de zéro, donc le
    battant pointait vers l'est, que le mur soit orienté au nord, à l'ouest ou
    au sud. Ici l'angle de base est mesuré sur le mur, ce qui est juste dans les
    quatre quadrants.

    Lève ``InvalidGeometry`` si le battant est de longueur nulle à ``tol`` près
    ou si l'ouverture n'est pas dans ``]0, 360[`` — un tour complet n'est pas un
    arc mais un cercle, et ses deux angles seraient confondus. Lève
    ``InvalidParameter`` si ``hand`` n'est ni ``"left"`` ni ``"right"``.
    """
    center = _pt(hinge)
    radius = distance(center, leaf_end)
    if radius <= tol:
        raise InvalidGeometry(
            "Battant de longueur nulle: le gond et l'extrémité sont confondus",
            hinge=center,
            leaf_end=_pt(leaf_end),
            tol=tol,
        )
    opening = float(opening_deg)
    if not 0.0 < opening < 360.0:
        raise InvalidGeometry(
            "Angle d'ouverture hors domaine: attendu dans ]0, 360[ degrés",
            opening_deg=opening,
        )
    if hand not in ("left", "right"):
        raise InvalidParameter(
            f"Sens d'ouverture inconnu: {hand!r}",
            supported=["left", "right"],
        )

    base = angle_of(center, leaf_end, tol=tol)
    sweep = math.radians(opening)
    if hand == "left":
        start_angle, end_angle = base, base + sweep
    else:
        start_angle, end_angle = base - sweep, base
    return (center, radius, normalize_angle(start_angle), normalize_angle(end_angle))


# ---------------------------------------------------------------------------
# Polygones
# ---------------------------------------------------------------------------


def polygon_area(points: Sequence[Point2]) -> float:
    """Aire d'un polygone simple par la formule du lacet, en valeur absolue.

    Le contour peut être donné ouvert ou explicitement fermé, dans un sens ou
    dans l'autre: l'aire est la même. Lève ``InvalidGeometry`` en dessous de
    trois sommets.
    """
    ring = _require_ring(points, "Le calcul d'aire")
    return abs(_signed_area(ring))


def polygon_is_clockwise(points: Sequence[Point2], *, tol: float = EPS) -> bool:
    """Indique si les sommets tournent dans le sens horaire.

    L'orientation décide du sens des décalages, du côté extérieur d'un contour
    et du sens de lecture des hachures: elle doit être explicite, jamais
    supposée.

    ``tol`` est une longueur; le seuil appliqué à l'aire est donc ``tol * tol``.
    Un polygone d'aire nulle — sommets alignés, contour aplati — lève
    ``InvalidGeometry``, car son orientation n'existe pas.
    """
    ring = _require_ring(points, "L'orientation d'un polygone")
    area = _signed_area(ring)
    if abs(area) <= tol * tol:
        raise InvalidGeometry(
            "Polygone dégénéré: aire nulle, orientation indéfinie",
            signed_area=area,
            tol=tol,
        )
    return area < 0.0


# ---------------------------------------------------------------------------
# Boîtes englobantes
# ---------------------------------------------------------------------------


def bbox(points: Iterable[Point2]) -> BBox:
    """Boîte englobante d'un nuage de points: ``(xmin, ymin, xmax, ymax)``.

    Un point unique donne une boîte plate, ce qui est légitime et reste
    utilisable par ``bbox_union`` et ``bbox_intersects``. Lève
    ``InvalidGeometry`` si la suite est vide.
    """
    xs: list[float] = []
    ys: list[float] = []
    for p in points:
        xs.append(float(p[0]))
        ys.append(float(p[1]))
    if not xs:
        raise InvalidGeometry("Boîte englobante indéfinie: aucun point")
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(boxes: Iterable[BBox]) -> BBox:
    """Plus petite boîte contenant toutes les boîtes données.

    Lève ``InvalidGeometry`` si la suite est vide ou si une boîte est inversée.
    """
    result: BBox | None = None
    for box in boxes:
        xmin, ymin, xmax, ymax = _check_box(box)
        if result is None:
            result = (xmin, ymin, xmax, ymax)
        else:
            result = (
                min(result[0], xmin),
                min(result[1], ymin),
                max(result[2], xmax),
                max(result[3], ymax),
            )
    if result is None:
        raise InvalidGeometry("Union de boîtes indéfinie: aucune boîte")
    return result


def bbox_contains(outer: BBox, inner: BBox, *, tol: float = EPS) -> bool:
    """Vrai si ``inner`` tient entièrement dans ``outer``, à ``tol`` près.

    Le contact des bords compte comme une inclusion: une pièce dont un mur est
    sur la limite de l'étage est dans l'étage.
    """
    oxmin, oymin, oxmax, oymax = _check_box(outer)
    ixmin, iymin, ixmax, iymax = _check_box(inner)
    return (
        ixmin >= oxmin - tol
        and iymin >= oymin - tol
        and ixmax <= oxmax + tol
        and iymax <= oymax + tol
    )


def bbox_intersects(a: BBox, b: BBox, *, tol: float = EPS) -> bool:
    """Vrai si les deux boîtes se recouvrent ou se touchent, à ``tol`` près."""
    axmin, aymin, axmax, aymax = _check_box(a)
    bxmin, bymin, bxmax, bymax = _check_box(b)
    return not (
        axmax < bxmin - tol or bxmax < axmin - tol or aymax < bymin - tol or bymax < aymin - tol
    )


# ---------------------------------------------------------------------------
# Intersections
# ---------------------------------------------------------------------------


def segments_intersect(
    a1: Point2,
    a2: Point2,
    b1: Point2,
    b2: Point2,
    *,
    tol: float = EPS,
) -> Point2 | None:
    """Point d'intersection de deux segments, ou ``None`` s'ils ne se croisent pas.

    Sert à détecter les murs qui se croisent. Le contact par une extrémité est
    une intersection: une jonction en T ou en L est détectée.

    ``tol`` est une longueur, appliquée dans l'espace du dessin: deux segments
    qui se ratent de moins de ``tol`` sont considérés comme sécants, et le point
    rendu est ramené sur le premier segment.

    Renvoie ``None`` pour deux segments parallèles, y compris colinéaires et
    superposés: leur intersection n'est alors pas un point mais un segment, et
    aucune valeur unique ne serait honnête. Lève ``InvalidGeometry`` si l'un des
    deux segments est de longueur nulle, car sa direction est indéfinie.
    """
    ax, ay = _pt(a1)
    bx, by = _pt(a2)
    cx, cy = _pt(b1)
    dx_, dy_ = _pt(b2)

    rx, ry = bx - ax, by - ay
    sx, sy = dx_ - cx, dy_ - cy
    len_r = math.hypot(rx, ry)
    len_s = math.hypot(sx, sy)
    if len_r <= tol:
        raise InvalidGeometry(
            "Segment dégénéré: longueur nulle",
            start=(ax, ay),
            end=(bx, by),
            length=len_r,
            tol=tol,
        )
    if len_s <= tol:
        raise InvalidGeometry(
            "Segment dégénéré: longueur nulle",
            start=(cx, cy),
            end=(dx_, dy_),
            length=len_s,
            tol=tol,
        )

    denom = rx * sy - ry * sx
    # |denom| vaut len_r * len_s * |sin θ|. Le comparer à tol * max(len_r, len_s)
    # revient à dire que les deux segments s'écartent de moins de tol sur leur
    # longueur: à cette finesse, le point d'intersection n'est plus calculable.
    if abs(denom) <= tol * max(len_r, len_s):
        return None

    qpx, qpy = cx - ax, cy - ay
    t = (qpx * sy - qpy * sx) / denom
    u = (qpx * ry - qpy * rx) / denom
    # tol est une longueur, t et u sont sans dimension: la conversion passe par
    # la longueur de chaque segment.
    t_tol = tol / len_r
    u_tol = tol / len_s
    if not (-t_tol <= t <= 1.0 + t_tol) or not (-u_tol <= u <= 1.0 + u_tol):
        return None

    t_clamped = min(1.0, max(0.0, t))
    return (ax + rx * t_clamped, ay + ry * t_clamped)


# ---------------------------------------------------------------------------
# Contours
# ---------------------------------------------------------------------------


def close_ring(points: Sequence[Point2], tol: float = EPS) -> tuple[Point2, ...]:
    """Ferme un contour dont le dernier sommet rejoint presque le premier.

    Les contours issus d'un relevé ou d'un import finissent souvent par un point
    qui vaut le premier à un cheveu près. Tracé tel quel en polyligne fermée, ce
    point produit un segment de longueur quasi nulle, qui fait échouer les
    hachures et fausse les longueurs.

    Si le dernier sommet est à moins de ``tol`` du premier, il est supprimé. Le
    contour rendu ne répète donc jamais son premier sommet: c'est la forme
    attendue par ``AddPolyline(..., closed=True)``, où la fermeture est portée
    par l'entité et non par un sommet dupliqué.

    Lève ``InvalidGeometry`` si le contour compte moins de trois sommets, avant
    comme après fermeture.
    """
    ring = _require_ring(points, "La fermeture d'un contour")
    if math.hypot(ring[-1][0] - ring[0][0], ring[-1][1] - ring[0][1]) <= tol:
        ring = ring[:-1]
    if len(ring) < 3:
        raise InvalidGeometry(
            "Contour dégénéré: moins de trois sommets une fois fermé",
            vertex_count=len(ring),
            tol=tol,
        )
    return ring
