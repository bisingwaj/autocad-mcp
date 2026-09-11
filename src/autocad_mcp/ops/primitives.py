"""Primitives de dessin.

Fonctions pures qui rendent des opérations. Elles existent pour donner au
modèle un vocabulaire de base, et pour appliquer les conventions du projet:
calque explicite, couleur suivant le calque par défaut, angles en radians.
"""

from __future__ import annotations

import math

from ..errors import InvalidParameter
from ..geometry import close_ring, rectangle_points
from ..model.layers import STANDARD_LAYERS, color_index
from ..model.ops import (
    AddArc,
    AddCircle,
    AddHatch,
    AddLine,
    AddMText,
    AddPolyline,
    AddText,
    EnsureLayer,
    Operation,
    Point2,
    Style,
)
from ..units import Defaults

__all__ = [
    "arc",
    "circle",
    "hatch",
    "line",
    "mtext",
    "polyline",
    "rectangle",
    "text",
]


def _style(layer: str, color: str | int | None) -> tuple[Style, list[Operation]]:
    """Construit le style et l'opération de calque qui doit le précéder.

    Une entité déposée sur un calque inexistant est acceptée par certains
    moteurs et refusée par d'autres. On crée donc toujours le calque d'abord.
    """
    spec = None
    for candidate in STANDARD_LAYERS.values():
        if candidate.name == layer.upper():
            spec = candidate
            break

    prelude: list[Operation] = [
        EnsureLayer(
            name=layer,
            color=spec.color if spec else 7,
            description=spec.description if spec else "",
        )
    ]
    style = Style(layer=layer) if color is None else Style(layer=layer, color=color_index(color))
    return style, prelude


def line(
    start: Point2,
    end: Point2,
    *,
    layer: str = "0",
    color: str | int | None = None,
) -> list[Operation]:
    style, ops = _style(layer, color)
    ops.append(AddLine(start=(*start, 0.0), end=(*end, 0.0), style=style))
    return ops


def polyline(
    points: list[Point2],
    *,
    closed: bool = False,
    width: float = 0.0,
    layer: str = "0",
    color: str | int | None = None,
) -> list[Operation]:
    """Polyligne, ouverte ou fermée.

    Un contour fermé ne doit pas répéter son premier sommet: le segment de
    longueur nulle qui en résulterait empêche le hachurage.
    """
    if len(points) < 2:
        raise InvalidParameter("Une polyligne demande au moins deux points", count=len(points))
    cleaned = close_ring(points) if closed else tuple(points)
    style, ops = _style(layer, color)
    ops.append(AddPolyline(points=cleaned, closed=closed, width=width, style=style))
    return ops


def rectangle(
    corner1: Point2,
    corner2: Point2,
    defaults: Defaults,
    *,
    layer: str = "0",
    color: str | int | None = None,
) -> list[Operation]:
    """Rectangle, tracé comme une polyligne fermée.

    Régression historique: l'ancien code produisait quatre segments
    indépendants, donc ni sélectionnables d'un clic, ni hachurables, et dont
    l'aire n'était pas calculable.
    """
    corners = rectangle_points(corner1, corner2, tol=defaults.tolerance)
    style, ops = _style(layer, color)
    ops.append(AddPolyline(points=corners, closed=True, style=style))
    return ops


def circle(
    center: Point2,
    radius: float,
    *,
    layer: str = "0",
    color: str | int | None = None,
) -> list[Operation]:
    style, ops = _style(layer, color)
    ops.append(AddCircle(center=(*center, 0.0), radius=radius, style=style))
    return ops


def arc(
    center: Point2,
    radius: float,
    start_deg: float,
    end_deg: float,
    *,
    layer: str = "0",
    color: str | int | None = None,
) -> list[Operation]:
    """Arc de cercle, reçu en degrés et converti en radians.

    Les degrés sont l'unité naturelle pour un utilisateur, les radians celle du
    modèle. La conversion se fait ici, à la frontière publique.
    """
    style, ops = _style(layer, color)
    ops.append(
        AddArc(
            center=(*center, 0.0),
            radius=radius,
            start_angle=math.radians(start_deg),
            end_angle=math.radians(end_deg),
            style=style,
        )
    )
    return ops


def text(
    position: Point2,
    content: str,
    defaults: Defaults,
    *,
    height: float | None = None,
    rotation_deg: float = 0.0,
    layer: str = "ANNOTATION",
    color: str | int | None = None,
    halign: str = "left",
) -> list[Operation]:
    style, ops = _style(layer, color)
    ops.append(
        AddText(
            position=(*position, 0.0),
            text=content,
            height=defaults.text_height if height is None else height,
            rotation=math.radians(rotation_deg),
            halign=halign,  # type: ignore[arg-type]
            style=style,
        )
    )
    return ops


def mtext(
    position: Point2,
    content: str,
    defaults: Defaults,
    *,
    height: float | None = None,
    width: float = 0.0,
    rotation_deg: float = 0.0,
    layer: str = "ANNOTATION",
    color: str | int | None = None,
) -> list[Operation]:
    style, ops = _style(layer, color)
    ops.append(
        AddMText(
            position=(*position, 0.0),
            text=content,
            height=defaults.text_height if height is None else height,
            width=width,
            rotation=math.radians(rotation_deg),
            style=style,
        )
    )
    return ops


def hatch(
    boundaries: list[list[Point2]],
    *,
    pattern: str = "SOLID",
    scale: float = 1.0,
    angle_deg: float = 0.0,
    layer: str = "HATCH",
    color: str | int | None = None,
) -> list[Operation]:
    """Hachure délimitée par un ou plusieurs contours fermés.

    Chaque contour est fermé automatiquement si besoin, car un contour ouvert
    produit une hachure vide sans message d'erreur.
    """
    if not boundaries:
        raise InvalidParameter("Une hachure demande au moins un contour")
    rings = tuple(close_ring(ring) for ring in boundaries)
    style, ops = _style(layer, color)
    ops.append(
        AddHatch(
            boundaries=rings,
            pattern=pattern,
            scale=scale,
            angle=math.radians(angle_deg),
            style=style,
        )
    )
    return ops
