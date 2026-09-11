"""Éléments de bâtiment: murs, portes, fenêtres, pièces.

Ces fonctions sont **pures**. Elles ne dessinent pas, elles décident quoi
dessiner et rendent une liste d'opérations. Elles ne connaissent ni AutoCAD,
ni `ezdxf`, ni COM, ce qui les rend testables sur n'importe quelle machine.

Deux différences de fond avec le code historique:

* un mur épais est **une polyligne fermée**, donc une entité unique,
  sélectionnable d'un clic, hachurable et dont l'aire est calculable. L'ancien
  code produisait quatre segments indépendants qui ne formaient un mur que pour
  l'œil ;
* toutes les longueurs viennent de `units.Defaults`, donc un mur de vingt
  centimètres reste un mur de vingt centimètres que le dessin soit en
  millimètres ou en pieds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from ..errors import InvalidParameter
from ..geometry import (
    Hand,
    angle_of,
    distance,
    door_swing_arc,
    midpoint,
    offset_segment,
    perpendicular,
    rectangle_points,
    split_run_by_openings,
    thick_segment_outline,
    wall_band,
)
from ..model.layers import STANDARD_LAYERS, LayerSpec
from ..model.ops import (
    AddArc,
    AddLine,
    AddPolyline,
    AddText,
    EnsureLayer,
    Operation,
    Point2,
    Style,
)
from ..units import Defaults

__all__ = [
    "Opening",
    "door",
    "door_in_wall",
    "label",
    "room",
    "wall",
    "wall_network",
    "wall_run",
    "window",
]


@dataclass(frozen=True, slots=True)
class Opening:
    """Baie percée dans un mur d'une enfilade.

    ``segment`` désigne le rang du mur dans l'enfilade, le premier portant le
    rang zéro. ``position`` est une fraction de sa longueur qui situe le
    **centre** de la baie. ``width`` est sa largeur, dans l'unité du document.

    Une baie coupe réellement le mur en deux tronçons. Le code précédent se
    contentait de superposer un symbole de fenêtre par-dessus un mur resté
    plein, ce qui se voyait au rendu et interdisait tout calcul de surface.
    """

    segment: int
    position: float
    width: float
    kind: Literal["door", "window", "passage"] = "door"
    hand: Hand = "left"
    opening_deg: float = 90.0


def _layer_op(spec: LayerSpec) -> EnsureLayer:
    return EnsureLayer(name=spec.name, color=spec.color, description=spec.description)


def _style(spec: LayerSpec, color: int | None) -> Style:
    """Style d'une entité. Sans couleur explicite, l'entité suit son calque."""
    return Style(layer=spec.name) if color is None else Style(layer=spec.name, color=color)


def wall(
    start: Point2,
    end: Point2,
    defaults: Defaults,
    *,
    thickness: float | None = None,
    color: int | None = None,
    layer: str | None = None,
) -> list[Operation]:
    """Mur d'épaisseur constante, tracé comme une polyligne fermée.

    Une épaisseur nulle ou négative produit un simple axe, ce qui reste utile
    pour esquisser. Sans épaisseur donnée, celle d'un mur de maçonnerie courant
    est utilisée, exprimée dans l'unité du document.
    """
    spec = STANDARD_LAYERS["walls"] if layer is None else LayerSpec(layer, 7, "")
    width = defaults.wall_thickness if thickness is None else float(thickness)
    style = _style(spec, color)
    ops: list[Operation] = [_layer_op(spec)]

    if width <= defaults.tolerance:
        # Mur sans épaisseur: un axe simple, pas un rectangle dégénéré.
        ops.append(AddLine(start=(*start, 0.0), end=(*end, 0.0), style=style))
        return ops

    outline = thick_segment_outline(start, end, width, tol=defaults.tolerance)
    ops.append(AddPolyline(points=outline, closed=True, style=style))
    return ops


def door(
    hinge: Point2,
    leaf_closed: Point2,
    defaults: Defaults,
    *,
    opening_deg: float = 90.0,
    hand: Hand = "left",
    color: int | None = None,
    show_swing: bool = True,
) -> list[Operation]:
    """Porte: le vantail en position ouverte, et son arc de débattement.

    ``hinge → leaf_closed`` décrit le battant **fermé**, donc couché dans l'axe
    du mur et occupant la baie. C'est de là que part l'arc. Le trait du vantail
    est en revanche tracé en position **ouverte**, à l'autre extrémité de
    l'arc, comme sur un plan d'architecte.

    Passer ici la position ouverte, comme le faisait ce module, rabattait le
    battant du mauvais côté du gond: sur une porte d'entrée, il se refermait à
    quatre-vingt-dix centimètres en dehors du bâtiment.
    """
    spec = STANDARD_LAYERS["doors"]
    style = _style(spec, color)
    ops: list[Operation] = [_layer_op(spec)]

    centre, rayon, a0, a1 = door_swing_arc(
        hinge, leaf_closed, opening_deg, hand, tol=defaults.tolerance
    )
    # L'arc tourne dans le sens direct: la position ouverte est à sa fin pour
    # une porte à gauche, à son début pour une porte à droite.
    ouvert = a1 if hand == "left" else a0
    vantail = (
        centre[0] + rayon * math.cos(ouvert),
        centre[1] + rayon * math.sin(ouvert),
    )
    ops.append(AddLine(start=(*centre, 0.0), end=(*vantail, 0.0), style=style))

    if show_swing:
        ops.append(
            AddArc(
                center=(*centre, 0.0),
                radius=rayon,
                start_angle=a0,
                end_angle=a1,
                style=style,
            )
        )
    return ops


def window(
    start: Point2,
    end: Point2,
    defaults: Defaults,
    *,
    thickness: float | None = None,
    color: int | None = None,
) -> list[Operation]:
    """Fenêtre: le dormant, et les deux traits de vitrage à l'intérieur.

    L'écart des traits dérive de l'épaisseur du mur, jamais d'une constante en
    dur. L'ancien code utilisait un décalage de cinq centièmes sans préciser
    l'unité, donc invisible dans un dessin en millimètres.
    """
    spec = STANDARD_LAYERS["windows"]
    style = _style(spec, color)
    ops: list[Operation] = [_layer_op(spec)]

    width = defaults.wall_thickness if thickness is None else float(thickness)
    if width <= defaults.tolerance:
        ops.append(AddLine(start=(*start, 0.0), end=(*end, 0.0), style=style))
        return ops

    # Dormant: le contour du mur percé.
    outline = thick_segment_outline(start, end, width, tol=defaults.tolerance)
    ops.append(AddPolyline(points=outline, closed=True, style=style))

    # Vitrage: deux traits parallèles à l'axe, au quart de l'épaisseur.
    quarter = width / 4.0
    for offset in (quarter, -quarter):
        a, b = offset_segment(start, end, offset, tol=defaults.tolerance)
        ops.append(AddLine(start=(*a, 0.0), end=(*b, 0.0), style=style))
    return ops


def room(
    corner1: Point2,
    corner2: Point2,
    defaults: Defaults,
    *,
    thickness: float | None = None,
    name: str | None = None,
    color: int | None = None,
    show_area: bool = True,
) -> list[Operation]:
    """Pièce rectangulaire, murs raccordés, plus une étiquette.

    Les quatre murs forment un contour fermé unique dont les angles sont
    mitrés, donc deux anneaux au lieu de quatre rectangles qui se chevauchent.
    L'étiquette porte le nom et la surface utile, mesure que le modèle peut
    relire.
    """
    corners = rectangle_points(corner1, corner2, tol=defaults.tolerance)
    ops = wall_network(
        list(corners), defaults, thickness=thickness, closed=True, color=color
    )

    if name or show_area:
        center = midpoint(corners[0], corners[2])
        width = distance(corners[0], corners[1])
        height = distance(corners[1], corners[2])
        epaisseur = defaults.wall_thickness if thickness is None else float(thickness)
        # Surface utile: à l'intérieur des murs, ce qu'attend un plan.
        utile_l = max(0.0, width - epaisseur)
        utile_h = max(0.0, height - epaisseur)
        parts = []
        if name:
            parts.append(name)
        if show_area:
            parts.append(f"{utile_l * utile_h:.2f} {defaults.unit.value}2")
        ops.extend(label(center, "  ".join(parts), defaults))

    return ops


def label(
    position: Point2,
    text: str,
    defaults: Defaults,
    *,
    height: float | None = None,
    rotation: float = 0.0,
    color: int | None = None,
) -> list[Operation]:
    """Étiquette centrée, déposée sur le calque d'annotation.

    Le calque est imposé: une annotation ne doit jamais se perdre au milieu des
    murs, sous peine de disparaître avec eux quand on gèle le calque.
    """
    if not text.strip():
        raise InvalidParameter("Étiquette vide", text=text)

    spec = STANDARD_LAYERS["annotation"]
    style = _style(spec, color)
    return [
        _layer_op(spec),
        AddText(
            position=(*position, 0.0),
            text=text,
            height=defaults.text_height if height is None else float(height),
            rotation=rotation,
            halign="center",
            valign="middle",
            style=style,
        ),
    ]


def wall_run(
    points: list[Point2],
    defaults: Defaults,
    *,
    thickness: float | None = None,
    closed: bool = False,
    color: int | None = None,
) -> list[Operation]:
    """Enfilade de murs suivant une polyligne.

    Délègue à :func:`wall_network`, donc les angles se raccordent. Auparavant
    chaque mur était tracé isolément et les coins portaient un recouvrement
    carré de la taille de l'épaisseur.
    """
    return wall_network(
        points, defaults, thickness=thickness, closed=closed, color=color
    )


def door_in_wall(
    wall_start: Point2,
    wall_end: Point2,
    defaults: Defaults,
    *,
    position: float = 0.5,
    width: float | None = None,
    hand: Hand = "left",
    opening_deg: float = 90.0,
) -> list[Operation]:
    """Porte posée le long d'un mur donné, sans percer celui-ci.

    ``position`` est une fraction de la longueur du mur, de zéro à un, qui
    situe le gond. Le battant fermé est couché dans l'axe du mur, vers l'avant
    pour une porte à gauche et vers l'arrière pour une porte à droite, puis il
    s'ouvre perpendiculairement.

    Pour percer réellement le mur, préférer :func:`wall_network` et ses baies.
    """
    if not 0.0 <= position <= 1.0:
        raise InvalidParameter(
            "La position doit être une fraction entre 0 et 1", position=position
        )

    leaf = defaults.door_width if width is None else float(width)
    length = distance(wall_start, wall_end)
    if length <= defaults.tolerance:
        raise InvalidParameter("Mur de longueur nulle", length=length)

    axis = angle_of(wall_start, wall_end, tol=defaults.tolerance)
    hx = wall_start[0] + (wall_end[0] - wall_start[0]) * position
    hy = wall_start[1] + (wall_end[1] - wall_start[1]) * position
    hinge = (hx, hy)

    # Battant fermé: couché dans le mur, du côté que désigne le sens.
    sens = 1.0 if hand == "left" else -1.0
    leaf_closed = (
        hx + leaf * sens * math.cos(axis),
        hy + leaf * sens * math.sin(axis),
    )

    return door(
        hinge, leaf_closed, defaults, opening_deg=opening_deg, hand=hand, show_swing=True
    )


def wall_network(
    points: list[Point2],
    defaults: Defaults,
    *,
    thickness: float | None = None,
    closed: bool = False,
    openings: list[Opening] | None = None,
    color: int | None = None,
    layer: str | None = None,
    show_symbols: bool = True,
) -> list[Operation]:
    """Réseau de murs raccordés, percé de ses baies.

    C'est la fonction qui corrige les trois défauts visibles au rendu.

    **Les angles se raccordent.** Les faces du mur sont les décalages mitrés de
    l'axe complet, donc deux murs perpendiculaires forment un angle net. Tracer
    chaque mur séparément laissait un recouvrement carré à chaque coin.

    **Un mur s'arrête où il doit.** Une enfilade fermée rend deux anneaux,
    extérieur et intérieur, au lieu de quatre rectangles qui se dépassent.

    **Une baie perce réellement le mur.** Elle le coupe en deux tronçons et
    ferme la coupe par un jambage, au lieu de poser un symbole par-dessus un
    mur resté plein.
    """
    if len(points) < 2:
        raise InvalidParameter(
            "Un réseau de murs demande au moins deux points", count=len(points)
        )

    spec = STANDARD_LAYERS["walls"] if layer is None else LayerSpec(layer, 7, "")
    width = defaults.wall_thickness if thickness is None else float(thickness)
    style = _style(spec, color)
    ops: list[Operation] = [_layer_op(spec)]

    baies = list(openings or [])

    # Sans baie, le ruban complet suffit et donne le meilleur dessin possible:
    # une ou deux polylignes fermées, mesurables et hachurables.
    if not baies:
        for ring in wall_band(
            points, width, closed=closed, tol=defaults.tolerance
        ):
            ops.append(AddPolyline(points=ring, closed=True, style=style))
        return ops

    ops.extend(
        _pierced_wall_solids(points, width, closed, style, baies, defaults)
    )

    if show_symbols:
        ops.extend(_opening_symbols(points, closed, baies, defaults, width))

    return ops


def _segment_count(points: list[Point2], closed: bool) -> int:
    return len(points) if closed else len(points) - 1


def _segment_ends(points: list[Point2], index: int) -> tuple[Point2, Point2]:
    return points[index], points[(index + 1) % len(points)]


def _pierced_wall_solids(
    points: list[Point2],
    width: float,
    closed: bool,
    style: Style,
    openings: list[Opening],
    defaults: Defaults,
) -> list[Operation]:
    """Tronçons pleins du mur, un quadrilatère par morceau restant.

    Les coins qui touchent un angle du réseau reprennent le sommet mitré, ce
    qui conserve le raccord. Les coins qui touchent une baie sont coupés
    perpendiculairement à l'axe, ce qui donne le jambage.
    """
    from ..geometry import offset_polyline_miter

    half = width / 2.0
    tol = defaults.tolerance
    left = offset_polyline_miter(points, half, closed=closed, tol=tol)
    right = offset_polyline_miter(points, -half, closed=closed, tol=tol)

    par_segment: dict[int, list[tuple[float, float]]] = {}
    for baie in openings:
        par_segment.setdefault(baie.segment, []).append((baie.position, baie.width))

    ops: list[Operation] = []
    total = _segment_count(points, closed)

    for index in range(total):
        a, b = _segment_ends(points, index)
        longueur = distance(a, b)
        if longueur <= tol:
            continue

        nx, ny = perpendicular(b[0] - a[0], b[1] - a[1], tol=tol)
        gauche_debut = left[index]
        gauche_fin = left[(index + 1) % len(left)]
        droite_debut = right[index]
        droite_fin = right[(index + 1) % len(right)]

        troncons = split_run_by_openings(a, b, par_segment.get(index, ()), tol=tol)

        for p0, p1 in troncons:
            debut_au_sommet = distance(p0, a) <= tol
            fin_au_sommet = distance(p1, b) <= tol

            gd = gauche_debut if debut_au_sommet else (p0[0] + nx * half, p0[1] + ny * half)
            gf = gauche_fin if fin_au_sommet else (p1[0] + nx * half, p1[1] + ny * half)
            dd = droite_debut if debut_au_sommet else (p0[0] - nx * half, p0[1] - ny * half)
            df = droite_fin if fin_au_sommet else (p1[0] - nx * half, p1[1] - ny * half)

            ops.append(
                AddPolyline(points=(gd, gf, df, dd), closed=True, style=style)
            )

    return ops


def _opening_symbols(
    points: list[Point2],
    closed: bool,
    openings: list[Opening],
    defaults: Defaults,
    width: float,
) -> list[Operation]:
    """Symboles des baies: battant de porte, vitrage de fenêtre.

    Le symbole se pose dans la coupe réellement pratiquée, et suit
    l'orientation du mur porteur.
    """
    ops: list[Operation] = []
    tol = defaults.tolerance
    total = _segment_count(points, closed)

    for baie in openings:
        if not 0 <= baie.segment < total:
            raise InvalidParameter(
                "Baie hors de l'enfilade",
                segment=baie.segment,
                segments=total,
            )
        a, b = _segment_ends(points, baie.segment)
        longueur = distance(a, b)
        if longueur <= tol:
            continue

        ux = (b[0] - a[0]) / longueur
        uy = (b[1] - a[1]) / longueur
        centre = (a[0] + ux * longueur * baie.position, a[1] + uy * longueur * baie.position)
        demi = baie.width / 2.0
        bord1 = (centre[0] - ux * demi, centre[1] - uy * demi)
        bord2 = (centre[0] + ux * demi, centre[1] + uy * demi)

        if baie.kind == "passage":
            continue  # une trémie nue n'a pas de symbole

        if baie.kind == "window":
            spec = STANDARD_LAYERS["windows"]
            style = _style(spec, None)
            ops.append(_layer_op(spec))
            # Vitrage: un trait dans l'axe de la baie, sur toute sa largeur.
            ops.append(AddLine(start=(*bord1, 0.0), end=(*bord2, 0.0), style=style))
            quart = width / 4.0
            for ecart in (quart, -quart):
                p, q = offset_segment(bord1, bord2, ecart, tol=tol)
                ops.append(AddLine(start=(*p, 0.0), end=(*q, 0.0), style=style))
            continue

        # Porte. Le battant fermé occupe exactement la baie: il va d'un
        # jambage à l'autre. Le gond est à l'un des deux bords, selon le sens.
        # Prendre la position ouverte comme référence rabattait le battant
        # hors de la baie, donc à l'extérieur du bâtiment sur une porte
        # d'entrée.
        gonds = bord1 if baie.hand == "left" else bord2
        ferme = bord2 if baie.hand == "left" else bord1
        ops.extend(
            door(
                gonds,
                ferme,
                defaults,
                opening_deg=baie.opening_deg,
                hand=baie.hand,
                show_swing=True,
            )
        )

    return ops
