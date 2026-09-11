"""Troisième dimension: donner une hauteur à un plan.

Le plan reste la source de vérité. Un volume s'obtient en élevant des contours
déjà tracés, jamais en dessinant deux fois la même chose: les murs, les baies
et les raccords d'angle sont ceux que `wall_panels` a calculés pour le plan.
C'est ce qui garantit qu'un volume tombe exactement sur son dessin.

## Ce qu'une baie devient en volume

Un percement en plan est un trou traversant. En volume, il ne l'est qu'entre
deux hauteurs, et ce qui reste est de la maçonnerie:

* une **fenêtre** garde son allège en dessous et son linteau au-dessus ;
* une **porte** garde son linteau seul, le passage allant jusqu'au sol ;
* un **passage** libre ne garde rien, du sol au plafond.

Un prototype qui se contente de percer sur toute la hauteur produit des murs
ajourés du sol au plafond, ce qui se voit immédiatement au premier rendu.
"""

from __future__ import annotations

from ..errors import InvalidParameter
from ..model.layers import STANDARD_LAYERS, LayerSpec
from ..model.ops import AddMesh, EnsureLayer, Operation, Point2, Style
from ..units import Defaults
from .architecture import Opening, Panel, wall_panels

__all__ = ["box", "extrude_ring", "slab", "wall_volume"]


def extrude_ring(
    contour: tuple[Point2, ...] | list[Point2],
    z_bas: float,
    z_haut: float,
    *,
    style: Style | None = None,
    cap: bool = True,
) -> AddMesh:
    """Élève un contour fermé en prisme droit.

    Les faces latérales suivent le contour; ``cap`` ajoute le dessus et le
    dessous, ce qu'il faut pour un volume fermé mais pas pour une simple
    façade.

    Lève ``InvalidParameter`` si le contour est trop court ou si la hauteur
    est nulle: un prisme plat n'est pas un volume, c'est un plan qu'on sait
    déjà dessiner.
    """
    points = list(contour)
    if len(points) < 3:
        raise InvalidParameter(
            "Un prisme demande un contour d'au moins trois sommets", count=len(points)
        )
    epaisseur = float(z_haut) - float(z_bas)
    if abs(epaisseur) <= 0.0:
        raise InvalidParameter(
            "Hauteur nulle: le résultat serait un plan, pas un volume",
            z_bas=z_bas,
            z_haut=z_haut,
        )

    n = len(points)
    sommets: list[tuple[float, float, float]] = [
        (float(x), float(y), float(z_bas)) for x, y in points
    ]
    sommets += [(float(x), float(y), float(z_haut)) for x, y in points]

    faces: list[tuple[int, ...]] = []
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, j + n, i + n))
    if cap:
        faces.append(tuple(range(n - 1, -1, -1)))  # dessous, normale vers le bas
        faces.append(tuple(range(n, 2 * n)))  # dessus

    return AddMesh(
        vertices=tuple(sommets),
        faces=tuple(faces),
        style=style or Style(layer=STANDARD_LAYERS["walls"].name),
    )


def _panel_slices(
    panneau: Panel, defaults: Defaults, hauteur: float
) -> list[tuple[float, float]]:
    """Tranches pleines d'un panneau, du bas vers le haut.

    C'est ici que se joue la différence entre un mur percé et un mur ajouré.
    """
    if panneau.kind == "solid":
        return [(0.0, hauteur)]
    if panneau.kind == "passage":
        return []
    if panneau.kind == "window":
        tranches = [(0.0, defaults.sill_height)]
        if defaults.window_head < hauteur:
            tranches.append((defaults.window_head, hauteur))
        return tranches
    # Porte: linteau seul, le passage descend jusqu'au sol.
    if defaults.door_head < hauteur:
        return [(defaults.door_head, hauteur)]
    return []


def wall_volume(
    points: list[Point2],
    defaults: Defaults,
    *,
    thickness: float | None = None,
    closed: bool = False,
    openings: list[Opening] | None = None,
    height: float | None = None,
    layer: str | None = None,
    color: int | None = None,
) -> list[Operation]:
    """Élève un réseau de murs en volumes, allèges et linteaux compris.

    Prend exactement les mêmes arguments que ``wall_network``, dont il est le
    pendant en trois dimensions. Appeler les deux avec les mêmes valeurs donne
    un plan et un volume qui coïncident.
    """
    if len(points) < 2:
        raise InvalidParameter(
            "Un réseau de murs demande au moins deux points", count=len(points)
        )

    spec = STANDARD_LAYERS["walls"] if layer is None else LayerSpec(layer, 7, "")
    haut = defaults.wall_height if height is None else float(height)
    if haut <= 0.0:
        raise InvalidParameter("Hauteur de mur nulle ou négative", height=haut)

    style = Style(layer=spec.name) if color is None else Style(layer=spec.name, color=color)
    ops: list[Operation] = [
        EnsureLayer(name=spec.name, color=spec.color, description=spec.description)
    ]

    for panneau in wall_panels(
        points, defaults, thickness=thickness, closed=closed, openings=openings
    ):
        for z_bas, z_haut in _panel_slices(panneau, defaults, haut):
            if z_haut - z_bas <= defaults.tolerance:
                continue
            ops.append(extrude_ring(panneau.points, z_bas, z_haut, style=style))

    return ops


def slab(
    contour: list[Point2],
    defaults: Defaults,
    *,
    thickness: float | None = None,
    z: float = 0.0,
    layer: str | None = None,
) -> list[Operation]:
    """Dalle de plancher, posée sous le niveau donné.

    Le dessus de la dalle affleure ``z``, donc les murs élevés depuis zéro
    reposent dessus au lieu de la traverser.
    """
    spec = STANDARD_LAYERS["structure"] if layer is None else LayerSpec(layer, 8, "")
    epaisseur = defaults.slab_thickness if thickness is None else float(thickness)
    if epaisseur <= 0.0:
        raise InvalidParameter("Épaisseur de dalle nulle ou négative", thickness=epaisseur)

    return [
        EnsureLayer(name=spec.name, color=spec.color, description=spec.description),
        extrude_ring(contour, z - epaisseur, z, style=Style(layer=spec.name)),
    ]


def box(
    corner1: Point2,
    corner2: Point2,
    *,
    z: float = 0.0,
    height: float,
    layer: str = "FURNITURE",
    color: int | None = None,
) -> list[Operation]:
    """Boîte droite, de quoi poser un meuble ou un appareil.

    Un volume habité se juge mieux qu'un volume vide: un lit et une table
    disent l'échelle du logement bien plus vite qu'une cote. La forme est
    volontairement sommaire, c'est une silhouette et non un modèle de
    fabricant.
    """
    from ..geometry import rectangle_points

    if height <= 0.0:
        raise InvalidParameter("Hauteur de boîte nulle ou négative", height=height)

    spec = STANDARD_LAYERS.get(layer.lower())
    nom = spec.name if spec else layer
    style = Style(layer=nom) if color is None else Style(layer=nom, color=color)
    contour = rectangle_points(corner1, corner2)

    return [
        EnsureLayer(
            name=nom,
            color=spec.color if spec else 2,
            description=spec.description if spec else "",
        ),
        extrude_ring(contour, z, z + height, style=style),
    ]
