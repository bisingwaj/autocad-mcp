"""Tests de la géométrie plane.

Deux séries méritent une attention particulière:

* les murs verticaux, où la pente est infinie et où toute formule en ``dy/dx``
  se casse ;
* le battant de porte dans les quatre quadrants. Le code historique appelait
  ``create_arc(start, width, 0, 90)``: l'arc partait toujours de zéro, donc le
  battant pointait vers l'est quelle que soit l'orientation du mur porteur.
  Les tests ``test_door_swing_*`` sont la non-régression de ce bug.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from autocad_mcp import geometry as g
from autocad_mcp.errors import InvalidGeometry, InvalidParameter
from autocad_mcp.model.ops import Point2
from autocad_mcp.units import Defaults, Unit

ABS = 1e-9


def polar(center: Point2, radius: float, angle: float) -> Point2:
    """Point sur un cercle, utilisé pour reconstruire un battant depuis son arc."""
    return (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))


def approx_point(p: Point2) -> object:
    """Comparaison d'un point unique. ``pytest.approx`` refuse les tuples imbriqués."""
    return pytest.approx(p, abs=ABS)


def assert_ring(actual: Sequence[Point2], expected: Sequence[Point2], tol: float = ABS) -> None:
    """Compare deux suites de points, sommet par sommet et dans l'ordre."""
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got == pytest.approx(want, abs=tol)


# --------------------------------------------------------------------------
# Constante de module
# --------------------------------------------------------------------------


def test_eps_is_positive_and_small() -> None:
    assert g.EPS > 0.0
    assert g.EPS < 1e-6


# --------------------------------------------------------------------------
# distance, midpoint, angle_of
# --------------------------------------------------------------------------


def test_distance_horizontal_and_vertical() -> None:
    assert g.distance((0.0, 0.0), (10.0, 0.0)) == pytest.approx(10.0)
    assert g.distance((5.0, -3.0), (5.0, 7.0)) == pytest.approx(10.0)


def test_distance_oblique_and_symmetry() -> None:
    a, b = (1.0, 2.0), (4.0, 6.0)
    assert g.distance(a, b) == pytest.approx(5.0)
    assert g.distance(b, a) == pytest.approx(5.0)


def test_distance_of_coincident_points_is_zero() -> None:
    assert g.distance((2.0, 2.0), (2.0, 2.0)) == 0.0


def test_midpoint() -> None:
    assert g.midpoint((0.0, 0.0), (10.0, 4.0)) == approx_point((5.0, 2.0))
    assert g.midpoint((-3.0, 8.0), (-3.0, -8.0)) == approx_point((-3.0, 0.0))


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ((1.0, 0.0), 0.0),
        ((0.0, 1.0), math.pi / 2),
        ((-1.0, 0.0), math.pi),
        ((0.0, -1.0), 3 * math.pi / 2),
        ((1.0, 1.0), math.pi / 4),
        ((-1.0, -1.0), 5 * math.pi / 4),
    ],
)
def test_angle_of_is_in_radians_on_zero_two_pi(target: Point2, expected: float) -> None:
    assert g.angle_of((0.0, 0.0), target) == pytest.approx(expected, abs=ABS)


def test_angle_of_coincident_points_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.angle_of((4.0, 4.0), (4.0, 4.0))


def test_angle_of_respects_document_tolerance() -> None:
    """Deux points distants de 0.05 mm sont confondus à l'échelle du document."""
    tol = Defaults(Unit.MILLIMETER).tolerance  # 0.1 unité de dessin
    assert g.angle_of((0.0, 0.0), (0.05, 0.0)) == pytest.approx(0.0)
    with pytest.raises(InvalidGeometry):
        g.angle_of((0.0, 0.0), (0.05, 0.0), tol=tol)


# --------------------------------------------------------------------------
# normalize, perpendicular
# --------------------------------------------------------------------------


def test_normalize_returns_unit_vector() -> None:
    vx, vy = g.normalize(3.0, 4.0)
    assert math.hypot(vx, vy) == pytest.approx(1.0)
    assert (vx, vy) == approx_point((0.6, 0.8))


def test_normalize_vertical_vector() -> None:
    assert g.normalize(0.0, -12.0) == approx_point((0.0, -1.0))


def test_normalize_null_vector_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.normalize(0.0, 0.0)


def test_normalize_below_tolerance_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.normalize(1e-4, 0.0, tol=1e-3)


@pytest.mark.parametrize(
    ("vector", "expected"),
    [
        ((1.0, 0.0), (0.0, 1.0)),  # vers l'est, gauche = nord
        ((0.0, 1.0), (-1.0, 0.0)),  # vers le nord, gauche = ouest
        ((-1.0, 0.0), (0.0, -1.0)),  # vers l'ouest, gauche = sud
        ((0.0, -1.0), (1.0, 0.0)),  # vers le sud, gauche = est
    ],
)
def test_perpendicular_is_left_unit_normal(vector: Point2, expected: Point2) -> None:
    assert g.perpendicular(*vector) == approx_point(expected)


def test_perpendicular_is_orthogonal_and_unit() -> None:
    vx, vy = 7.0, -2.5
    nx, ny = g.perpendicular(vx, vy)
    assert vx * nx + vy * ny == pytest.approx(0.0, abs=ABS)
    assert math.hypot(nx, ny) == pytest.approx(1.0)
    # Produit vectoriel de v par n positif: n est bien a gauche de v.
    assert vx * ny - vy * nx > 0.0


def test_perpendicular_null_vector_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.perpendicular(0.0, 0.0)


# --------------------------------------------------------------------------
# offset_segment
# --------------------------------------------------------------------------


def test_offset_segment_horizontal_wall() -> None:
    start, end = g.offset_segment((0.0, 0.0), (10.0, 0.0), 1.0)
    assert start == approx_point((0.0, 1.0))
    assert end == approx_point((10.0, 1.0))


def test_offset_segment_negative_distance_goes_right() -> None:
    start, end = g.offset_segment((0.0, 0.0), (10.0, 0.0), -1.0)
    assert start == approx_point((0.0, -1.0))
    assert end == approx_point((10.0, -1.0))


def test_offset_segment_vertical_wall_infinite_slope() -> None:
    """Mur vertical: la gauche du sens de parcours est l'ouest."""
    start, end = g.offset_segment((5.0, 0.0), (5.0, 10.0), 2.0)
    assert start == approx_point((3.0, 0.0))
    assert end == approx_point((3.0, 10.0))


def test_offset_segment_vertical_wall_reversed() -> None:
    """Le même mur parcouru vers le sud décale de l'autre côté."""
    start, end = g.offset_segment((5.0, 10.0), (5.0, 0.0), 2.0)
    assert start == approx_point((7.0, 10.0))
    assert end == approx_point((7.0, 0.0))


def test_offset_segment_keeps_length_and_parallelism() -> None:
    a, b = (1.0, 2.0), (7.0, 5.0)
    oa, ob = g.offset_segment(a, b, 0.35)
    assert g.distance(oa, ob) == pytest.approx(g.distance(a, b))
    assert g.angle_of(oa, ob) == pytest.approx(g.angle_of(a, b))
    assert g.distance(a, oa) == pytest.approx(0.35)


def test_offset_segment_zero_length_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.offset_segment((3.0, 3.0), (3.0, 3.0), 1.0)


def test_offset_segment_below_document_tolerance_raises() -> None:
    tol = Defaults(Unit.MILLIMETER).tolerance
    with pytest.raises(InvalidGeometry):
        g.offset_segment((0.0, 0.0), (0.05, 0.0), 1.0, tol=tol)


# --------------------------------------------------------------------------
# thick_segment_outline
# --------------------------------------------------------------------------


def test_thick_segment_outline_horizontal_wall() -> None:
    pts = g.thick_segment_outline((0.0, 0.0), (10.0, 0.0), 0.2)
    assert len(pts) == 4
    assert_ring(pts, ((0.0, -0.1), (10.0, -0.1), (10.0, 0.1), (0.0, 0.1)))


def test_thick_segment_outline_vertical_wall() -> None:
    pts = g.thick_segment_outline((0.0, 0.0), (0.0, 10.0), 0.2)
    assert_ring(pts, ((0.1, 0.0), (0.1, 10.0), (-0.1, 10.0), (-0.1, 0.0)))


def test_thick_segment_outline_is_counterclockwise_ring() -> None:
    pts = g.thick_segment_outline((1.0, 1.0), (4.0, 5.0), 0.3)
    assert g.polygon_is_clockwise(pts) is False
    assert pts[0] != pts[-1]  # pas de sommet dupliqué: la polyligne est fermée


def test_thick_segment_outline_area_equals_length_times_thickness() -> None:
    a, b, t = (2.0, -1.0), (8.0, 7.0), 0.25
    pts = g.thick_segment_outline(a, b, t)
    assert g.polygon_area(pts) == pytest.approx(g.distance(a, b) * t)


def test_thick_segment_outline_faces_are_centered_on_the_axis() -> None:
    a, b, t = (0.0, 0.0), (6.0, 3.0), 0.4
    p0, p1, p2, p3 = g.thick_segment_outline(a, b, t)
    assert g.midpoint(p0, p3) == approx_point(a)
    assert g.midpoint(p1, p2) == approx_point(b)
    assert g.distance(p0, p3) == pytest.approx(t)


def test_thick_segment_outline_zero_length_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.thick_segment_outline((1.0, 1.0), (1.0, 1.0), 0.2)


@pytest.mark.parametrize("thickness", [0.0, -0.2])
def test_thick_segment_outline_non_positive_thickness_raises(thickness: float) -> None:
    with pytest.raises(InvalidGeometry):
        g.thick_segment_outline((0.0, 0.0), (5.0, 0.0), thickness)


# --------------------------------------------------------------------------
# rectangle_points
# --------------------------------------------------------------------------


def test_rectangle_points_counterclockwise() -> None:
    pts = g.rectangle_points((0.0, 0.0), (4.0, 3.0))
    assert_ring(pts, ((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)))
    assert g.polygon_is_clockwise(pts) is False


@pytest.mark.parametrize(
    ("c1", "c2"),
    [
        ((0.0, 0.0), (4.0, 3.0)),
        ((4.0, 3.0), (0.0, 0.0)),
        ((0.0, 3.0), (4.0, 0.0)),
        ((4.0, 0.0), (0.0, 3.0)),
    ],
)
def test_rectangle_points_robust_to_corner_order(c1: Point2, c2: Point2) -> None:
    """Les quatre façons de donner la diagonale produisent le même rectangle."""
    assert_ring(g.rectangle_points(c1, c2), ((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)))


def test_rectangle_points_area_is_known() -> None:
    assert g.polygon_area(g.rectangle_points((-2.0, -1.0), (3.0, 5.0))) == pytest.approx(30.0)


@pytest.mark.parametrize(("c1", "c2"), [((0.0, 0.0), (0.0, 3.0)), ((0.0, 0.0), (4.0, 0.0))])
def test_rectangle_points_degenerate_raises(c1: Point2, c2: Point2) -> None:
    with pytest.raises(InvalidGeometry):
        g.rectangle_points(c1, c2)


# --------------------------------------------------------------------------
# door_swing_arc  -- non-régression du bug historique
# --------------------------------------------------------------------------

WALL_HEADINGS_DEG = [0.0, 30.0, 90.0, 135.0, 180.0, 210.0, 270.0, 315.0]


@pytest.mark.parametrize("heading_deg", WALL_HEADINGS_DEG)
def test_door_swing_left_hand_starts_on_the_wall_axis(heading_deg: float) -> None:
    """L'arc part du battant fermé, donc de l'axe du mur, pas de zéro."""
    hinge = (3.0, -2.0)
    radius = 0.9
    base = math.radians(heading_deg)
    leaf_end = polar(hinge, radius, base)

    center, r, start_angle, end_angle = g.door_swing_arc(hinge, leaf_end, 90.0, "left")

    assert center == approx_point(hinge)
    assert r == pytest.approx(radius)
    assert start_angle == pytest.approx(g.normalize_angle(base), abs=ABS)
    assert end_angle == pytest.approx(g.normalize_angle(base + math.pi / 2), abs=ABS)
    # Le point de départ de l'arc est exactement le battant fermé.
    assert polar(center, r, start_angle) == approx_point(leaf_end)


@pytest.mark.parametrize("heading_deg", WALL_HEADINGS_DEG)
def test_door_swing_right_hand_opens_the_other_way(heading_deg: float) -> None:
    """En main droite l'arc se termine sur l'axe du mur, le balayage est horaire."""
    hinge = (-1.5, 4.0)
    radius = 1.2
    base = math.radians(heading_deg)
    leaf_end = polar(hinge, radius, base)

    center, r, start_angle, end_angle = g.door_swing_arc(hinge, leaf_end, 90.0, "right")

    assert center == approx_point(hinge)
    assert r == pytest.approx(radius)
    assert end_angle == pytest.approx(g.normalize_angle(base), abs=ABS)
    assert start_angle == pytest.approx(g.normalize_angle(base - math.pi / 2), abs=ABS)
    # Le battant fermé reste une extrémité de l'arc.
    assert polar(center, r, end_angle) == approx_point(leaf_end)


@pytest.mark.parametrize("hand", ["left", "right"])
@pytest.mark.parametrize("heading_deg", WALL_HEADINGS_DEG)
@pytest.mark.parametrize("opening_deg", [45.0, 90.0, 120.0, 180.0])
def test_door_swing_sweep_equals_opening(hand: str, heading_deg: float, opening_deg: float) -> None:
    hinge = (0.0, 0.0)
    leaf_end = polar(hinge, 0.9, math.radians(heading_deg))
    _, _, start_angle, end_angle = g.door_swing_arc(hinge, leaf_end, opening_deg, hand)
    sweep = g.normalize_angle(end_angle - start_angle)
    assert sweep == pytest.approx(math.radians(opening_deg), abs=1e-9)


@pytest.mark.parametrize(
    ("leaf_end", "expected_start"),
    [
        ((0.0, 1.0), math.pi / 2),  # mur vers le nord
        ((-1.0, 0.0), math.pi),  # mur vers l'ouest
        ((0.0, -1.0), 3 * math.pi / 2),  # mur vers le sud
        ((-1.0, -1.0), 5 * math.pi / 4),  # mur oblique, troisième quadrant
    ],
)
def test_door_swing_is_not_hardcoded_zero_to_ninety(
    leaf_end: Point2, expected_start: float
) -> None:
    """Non-régression: ``create_arc(start, width, 0, 90)`` du code historique.

    Quelle que soit l'orientation du mur, l'ancien code traçait le même arc de
    0 à 90 degrés, donc un battant pointant vers l'est.
    """
    _, _, start_angle, end_angle = g.door_swing_arc((0.0, 0.0), leaf_end)
    assert start_angle == pytest.approx(expected_start, abs=ABS)
    assert (start_angle, end_angle) != pytest.approx((0.0, math.pi / 2), abs=ABS)


def test_door_swing_east_wall_still_matches_the_legacy_case() -> None:
    """Le seul cas où l'ancien code tombait juste doit rester juste."""
    _, _, start_angle, end_angle = g.door_swing_arc((0.0, 0.0), (0.9, 0.0))
    assert start_angle == pytest.approx(0.0, abs=ABS)
    assert end_angle == pytest.approx(math.pi / 2, abs=ABS)


def test_door_swing_zero_length_leaf_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.door_swing_arc((1.0, 1.0), (1.0, 1.0))


@pytest.mark.parametrize("opening_deg", [0.0, -30.0, 360.0, 400.0])
def test_door_swing_invalid_opening_raises(opening_deg: float) -> None:
    with pytest.raises(InvalidGeometry):
        g.door_swing_arc((0.0, 0.0), (0.9, 0.0), opening_deg)


def test_door_swing_unknown_hand_raises() -> None:
    with pytest.raises(InvalidParameter):
        g.door_swing_arc((0.0, 0.0), (0.9, 0.0), 90.0, "droite")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# polygon_area, polygon_is_clockwise
# --------------------------------------------------------------------------

CCW_SQUARE: tuple[Point2, ...] = ((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0))
CW_SQUARE: tuple[Point2, ...] = tuple(reversed(CCW_SQUARE))


def test_polygon_area_known_rectangle() -> None:
    assert g.polygon_area(CCW_SQUARE) == pytest.approx(12.0)


def test_polygon_area_is_absolute_for_both_orientations() -> None:
    assert g.polygon_area(CW_SQUARE) == pytest.approx(12.0)
    assert g.polygon_area(CW_SQUARE) == pytest.approx(g.polygon_area(CCW_SQUARE))


def test_polygon_area_triangle() -> None:
    assert g.polygon_area(((0.0, 0.0), (4.0, 0.0), (0.0, 3.0))) == pytest.approx(6.0)


def test_polygon_area_accepts_explicitly_closed_ring() -> None:
    closed = (*CCW_SQUARE, CCW_SQUARE[0])
    assert g.polygon_area(closed) == pytest.approx(12.0)


def test_polygon_area_of_l_shaped_room() -> None:
    room: tuple[Point2, ...] = (
        (0.0, 0.0),
        (6.0, 0.0),
        (6.0, 2.0),
        (3.0, 2.0),
        (3.0, 5.0),
        (0.0, 5.0),
    )
    assert g.polygon_area(room) == pytest.approx(6 * 2 + 3 * 3)


@pytest.mark.parametrize("points", [(), ((0.0, 0.0),), ((0.0, 0.0), (1.0, 1.0))])
def test_polygon_area_needs_three_vertices(points: tuple[Point2, ...]) -> None:
    with pytest.raises(InvalidGeometry):
        g.polygon_area(points)


def test_polygon_is_clockwise_both_orientations() -> None:
    assert g.polygon_is_clockwise(CW_SQUARE) is True
    assert g.polygon_is_clockwise(CCW_SQUARE) is False


def test_polygon_is_clockwise_on_vertical_sliver() -> None:
    """Un polygone très étroit garde une orientation lisible."""
    sliver: tuple[Point2, ...] = ((0.0, 0.0), (0.001, 0.0), (0.001, 10.0), (0.0, 10.0))
    assert g.polygon_is_clockwise(sliver) is False
    assert g.polygon_is_clockwise(tuple(reversed(sliver))) is True


def test_polygon_is_clockwise_collinear_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.polygon_is_clockwise(((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)))


def test_polygon_is_clockwise_too_few_points_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.polygon_is_clockwise(((0.0, 0.0), (1.0, 0.0)))


# --------------------------------------------------------------------------
# bbox et requêtes spatiales
# --------------------------------------------------------------------------


def test_bbox_of_points() -> None:
    assert g.bbox(((1.0, 5.0), (-2.0, 3.0), (4.0, -1.0))) == pytest.approx((-2.0, -1.0, 4.0, 5.0))


def test_bbox_of_single_point_is_degenerate_but_valid() -> None:
    assert g.bbox(((2.0, 3.0),)) == pytest.approx((2.0, 3.0, 2.0, 3.0))


def test_bbox_of_vertical_wall() -> None:
    assert g.bbox(((5.0, 0.0), (5.0, 10.0))) == pytest.approx((5.0, 0.0, 5.0, 10.0))


def test_bbox_empty_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.bbox(())


def test_bbox_union() -> None:
    boxes = [(0.0, 0.0, 1.0, 1.0), (5.0, -2.0, 6.0, 0.0)]
    assert g.bbox_union(boxes) == pytest.approx((0.0, -2.0, 6.0, 1.0))


def test_bbox_union_single_box_is_identity() -> None:
    assert g.bbox_union([(1.0, 2.0, 3.0, 4.0)]) == pytest.approx((1.0, 2.0, 3.0, 4.0))


def test_bbox_union_empty_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.bbox_union([])


def test_bbox_union_inverted_box_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.bbox_union([(3.0, 0.0, 1.0, 1.0)])


def test_bbox_contains() -> None:
    outer = (0.0, 0.0, 10.0, 10.0)
    assert g.bbox_contains(outer, (1.0, 1.0, 2.0, 2.0)) is True
    assert g.bbox_contains(outer, (0.0, 0.0, 10.0, 10.0)) is True
    assert g.bbox_contains(outer, (-1.0, 1.0, 2.0, 2.0)) is False
    assert g.bbox_contains(outer, (1.0, 1.0, 11.0, 2.0)) is False


def test_bbox_contains_uses_tolerance() -> None:
    outer = (0.0, 0.0, 10.0, 10.0)
    almost = (-0.05, 0.0, 10.0, 10.0)
    assert g.bbox_contains(outer, almost) is False
    assert g.bbox_contains(outer, almost, tol=0.1) is True


def test_bbox_intersects() -> None:
    a = (0.0, 0.0, 4.0, 4.0)
    assert g.bbox_intersects(a, (2.0, 2.0, 6.0, 6.0)) is True
    assert g.bbox_intersects(a, (4.0, 0.0, 8.0, 4.0)) is True  # contact franc
    assert g.bbox_intersects(a, (5.0, 0.0, 8.0, 4.0)) is False
    assert g.bbox_intersects(a, (0.0, 5.0, 4.0, 8.0)) is False


def test_bbox_intersects_is_symmetric() -> None:
    a, b = (0.0, 0.0, 4.0, 4.0), (3.0, 3.0, 9.0, 9.0)
    assert g.bbox_intersects(a, b) == g.bbox_intersects(b, a)


def test_bbox_intersects_uses_tolerance() -> None:
    a, b = (0.0, 0.0, 4.0, 4.0), (4.05, 0.0, 8.0, 4.0)
    assert g.bbox_intersects(a, b) is False
    assert g.bbox_intersects(a, b, tol=0.1) is True


# --------------------------------------------------------------------------
# segments_intersect
# --------------------------------------------------------------------------


def test_segments_intersect_crossing_walls() -> None:
    point = g.segments_intersect((0.0, 0.0), (10.0, 0.0), (5.0, -5.0), (5.0, 5.0))
    assert point == approx_point((5.0, 0.0))


def test_segments_intersect_vertical_and_oblique() -> None:
    point = g.segments_intersect((2.0, 0.0), (2.0, 10.0), (0.0, 0.0), (4.0, 8.0))
    assert point == approx_point((2.0, 4.0))


def test_segments_intersect_t_junction_at_endpoint() -> None:
    point = g.segments_intersect((0.0, 0.0), (10.0, 0.0), (5.0, 0.0), (5.0, 6.0))
    assert point == approx_point((5.0, 0.0))


def test_segments_intersect_shared_corner() -> None:
    point = g.segments_intersect((0.0, 0.0), (10.0, 0.0), (10.0, 0.0), (10.0, 7.0))
    assert point == approx_point((10.0, 0.0))


def test_segments_intersect_returns_none_when_they_miss() -> None:
    assert g.segments_intersect((0.0, 0.0), (4.0, 0.0), (5.0, -5.0), (5.0, 5.0)) is None


def test_segments_intersect_parallel_walls_return_none() -> None:
    assert g.segments_intersect((0.0, 0.0), (10.0, 0.0), (0.0, 2.0), (10.0, 2.0)) is None


def test_segments_intersect_collinear_overlap_returns_none() -> None:
    """Le recouvrement colinéaire n'a pas de point d'intersection unique."""
    assert g.segments_intersect((0.0, 0.0), (10.0, 0.0), (4.0, 0.0), (14.0, 0.0)) is None


def test_segments_intersect_zero_length_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.segments_intersect((1.0, 1.0), (1.0, 1.0), (0.0, 0.0), (5.0, 5.0))
    with pytest.raises(InvalidGeometry):
        g.segments_intersect((0.0, 0.0), (5.0, 5.0), (2.0, 2.0), (2.0, 2.0))


def test_segments_intersect_near_miss_within_tolerance() -> None:
    """Deux murs qui se ratent de 0.05 unité se croisent à l'échelle du document."""
    a1, a2 = (0.0, 0.0), (10.0, 0.0)
    b1, b2 = (5.0, 0.05), (5.0, 6.0)
    assert g.segments_intersect(a1, a2, b1, b2) is None
    assert g.segments_intersect(a1, a2, b1, b2, tol=0.1) == approx_point((5.0, 0.0))


# --------------------------------------------------------------------------
# close_ring
# --------------------------------------------------------------------------


def test_close_ring_drops_duplicated_last_point() -> None:
    points = (*CCW_SQUARE, (0.0, 0.0))
    assert_ring(g.close_ring(points), CCW_SQUARE)


def test_close_ring_leaves_open_ring_untouched() -> None:
    assert_ring(g.close_ring(CCW_SQUARE), CCW_SQUARE)


def test_close_ring_snaps_within_tolerance() -> None:
    points = (*CCW_SQUARE, (0.02, -0.01))
    # Sous EPS le dernier point reste un sommet à part entière.
    assert len(g.close_ring(points)) == 5
    # À la tolérance du document il ferme le contour et disparaît.
    assert_ring(g.close_ring(points, tol=0.1), CCW_SQUARE)


def test_close_ring_result_is_a_valid_polygon() -> None:
    ring = g.close_ring((*CCW_SQUARE, (0.0, 0.0)))
    assert g.polygon_area(ring) == pytest.approx(12.0)


@pytest.mark.parametrize("points", [(), ((0.0, 0.0),), ((0.0, 0.0), (1.0, 0.0))])
def test_close_ring_needs_three_vertices(points: tuple[Point2, ...]) -> None:
    with pytest.raises(InvalidGeometry):
        g.close_ring(points)


def test_close_ring_degenerate_after_closing_raises() -> None:
    """Un triangle dont le dernier point ferme le contour n'a plus que deux sommets."""
    with pytest.raises(InvalidGeometry):
        g.close_ring(((0.0, 0.0), (1.0, 0.0), (0.0, 0.0)))


# --------------------------------------------------------------------------
# offset_polyline_miter
# --------------------------------------------------------------------------
#
# Le défaut corrigé ici était visible au rendu: deux murs perpendiculaires
# dessinés chacun par ``thick_segment_outline`` se chevauchaient, et le coin
# portait un petit carré. Chaque mur ignorait ses voisins. Le raccord ne peut
# se calculer qu'en décalant la polyligne d'axe entière, puis en joignant les
# segments décalés par leur intersection.

L_AXIS: tuple[Point2, ...] = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))
Z_AXIS: tuple[Point2, ...] = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (20.0, 10.0))
SQUARE_AXIS: tuple[Point2, ...] = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))


def test_offset_polyline_miter_right_angle_left_vertex_is_exact() -> None:
    # Le sommet mitré est l'intersection de la droite y = 1 et de la droite
    # x = 9, pas le sommet (10, 0) translaté. C'est ce qui fait l'angle net.
    assert_ring(
        g.offset_polyline_miter(L_AXIS, 1.0),
        ((0.0, 1.0), (9.0, 1.0), (9.0, 10.0)),
    )


def test_offset_polyline_miter_right_angle_right_vertex_is_exact() -> None:
    # Décalage négatif: à droite du sens de parcours, le coin est saillant.
    assert_ring(
        g.offset_polyline_miter(L_AXIS, -1.0),
        ((0.0, -1.0), (11.0, -1.0), (11.0, 10.0)),
    )


def test_offset_polyline_miter_two_points_matches_offset_segment() -> None:
    start, end = (2.0, 3.0), (8.0, 11.0)
    assert_ring(
        g.offset_polyline_miter((start, end), 2.5),
        g.offset_segment(start, end, 2.5),
    )


def test_offset_polyline_miter_sign_convention_matches_offset_segment() -> None:
    # Positif = gauche du sens de parcours, comme offset_segment.
    left = g.offset_polyline_miter(((0.0, 0.0), (10.0, 0.0)), 3.0)
    assert left[0][1] == pytest.approx(3.0)


def test_offset_polyline_miter_vertical_polyline() -> None:
    # Mur vertical: la pente est infinie, toute formule en dy/dx se casse.
    axis = ((0.0, 0.0), (0.0, 10.0), (10.0, 10.0))
    assert_ring(
        g.offset_polyline_miter(axis, 1.0),
        ((-1.0, 0.0), (-1.0, 11.0), (10.0, 11.0)),
    )


def test_offset_polyline_miter_collinear_segments_keep_the_simple_offset() -> None:
    axis = ((0.0, 0.0), (5.0, 0.0), (10.0, 0.0))
    assert_ring(g.offset_polyline_miter(axis, 2.0), ((0.0, 2.0), (5.0, 2.0), (10.0, 2.0)))


def test_offset_polyline_miter_duplicate_points_are_ignored() -> None:
    axis = ((0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (10.0, 0.0), (10.0, 10.0))
    assert_ring(g.offset_polyline_miter(axis, 1.0), ((0.0, 1.0), (9.0, 1.0), (9.0, 10.0)))


def test_offset_polyline_miter_duplicates_use_the_tolerance() -> None:
    tol = Defaults(Unit.MILLIMETER).tolerance  # 0.1 unité de dessin
    axis = ((0.0, 0.0), (0.02, 0.0), (10.0, 0.0), (10.0, 10.0))
    assert len(g.offset_polyline_miter(axis, 1.0, tol=tol)) == 3


def test_offset_polyline_miter_reflex_corner_on_a_z_shape() -> None:
    # Le Z tourne à gauche puis à droite: chaque côté voit un angle sortant
    # puis un angle rentrant. Les deux doivent se raccorder.
    assert_ring(
        g.offset_polyline_miter(Z_AXIS, 1.0),
        ((0.0, 1.0), (9.0, 1.0), (9.0, 11.0), (20.0, 11.0)),
    )
    assert_ring(
        g.offset_polyline_miter(Z_AXIS, -1.0),
        ((0.0, -1.0), (11.0, -1.0), (11.0, 9.0), (20.0, 9.0)),
    )


def test_offset_polyline_miter_reflex_corner_on_a_u_shape() -> None:
    axis = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    assert_ring(
        g.offset_polyline_miter(axis, 1.0, closed=False),
        ((0.0, 1.0), (9.0, 1.0), (9.0, 9.0), (0.0, 9.0)),
    )


def test_offset_polyline_miter_sharp_corner_within_the_limit_is_a_single_point() -> None:
    # Déviation de 120°: le rapport d'onglet vaut 1 / cos(60°) = 2, sous la
    # limite de 4. Le sommet est donc a deux fois la distance du sommet d'origine.
    axis = ((-10.0, 0.0), (0.0, 0.0), (-5.0, 5.0 * math.sqrt(3.0)))
    result = g.offset_polyline_miter(axis, 1.0)
    assert len(result) == 3
    assert result[1] == approx_point((-math.sqrt(3.0), 1.0))
    assert g.distance(result[1], (0.0, 0.0)) == pytest.approx(2.0, abs=ABS)


def test_offset_polyline_miter_sharp_corner_beyond_the_limit_is_beveled() -> None:
    axis = ((-10.0, 0.0), (0.0, 0.0), (-5.0, 5.0 * math.sqrt(3.0)))
    result = g.offset_polyline_miter(axis, 1.0, miter_limit=1.5)
    assert len(result) == 4  # la pointe est remplacée par deux points
    assert_ring(
        result[1:3],
        ((0.0, 1.0), (-math.sqrt(3.0) / 2.0, -0.5)),
    )
    # Les deux points du biseau restent à la distance de décalage du sommet.
    for point in result[1:3]:
        assert g.distance(point, (0.0, 0.0)) == pytest.approx(1.0, abs=ABS)


def test_offset_polyline_miter_half_turn_is_beveled_not_infinite() -> None:
    # Repli complet: les deux droites décalées sont parallèles, la pointe part
    # à l'infini. Le biseau est la seule réponse finie.
    axis = ((0.0, 0.0), (10.0, 0.0), (5.0, 0.0))
    assert_ring(
        g.offset_polyline_miter(axis, 1.0),
        ((0.0, 1.0), (10.0, 1.0), (10.0, -1.0), (5.0, -1.0)),
    )


def test_offset_polyline_miter_closed_square_keeps_the_vertex_count() -> None:
    outer = g.offset_polyline_miter(SQUARE_AXIS, -1.0, closed=True)
    inner = g.offset_polyline_miter(SQUARE_AXIS, 1.0, closed=True)
    assert len(outer) == len(SQUARE_AXIS)
    assert len(inner) == len(SQUARE_AXIS)
    assert_ring(outer, ((-1.0, -1.0), (11.0, -1.0), (11.0, 11.0), (-1.0, 11.0)))
    assert_ring(inner, ((1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)))


def test_offset_polyline_miter_closed_accepts_an_explicitly_closed_ring() -> None:
    assert_ring(
        g.offset_polyline_miter((*SQUARE_AXIS, (0.0, 0.0)), 1.0, closed=True),
        g.offset_polyline_miter(SQUARE_AXIS, 1.0, closed=True),
    )


def test_offset_polyline_miter_zero_distance_returns_the_axis() -> None:
    assert_ring(g.offset_polyline_miter(L_AXIS, 0.0), L_AXIS)


@pytest.mark.parametrize(
    "points",
    [(), ((0.0, 0.0),), ((1.0, 1.0), (1.0, 1.0))],
)
def test_offset_polyline_miter_needs_two_distinct_points(points: tuple[Point2, ...]) -> None:
    with pytest.raises(InvalidGeometry):
        g.offset_polyline_miter(points, 1.0)


def test_offset_polyline_miter_closed_needs_three_distinct_points() -> None:
    with pytest.raises(InvalidGeometry):
        g.offset_polyline_miter(((0.0, 0.0), (10.0, 0.0)), 1.0, closed=True)


@pytest.mark.parametrize("miter_limit", [0.0, 0.99, -2.0])
def test_offset_polyline_miter_limit_below_one_raises(miter_limit: float) -> None:
    with pytest.raises(InvalidParameter):
        g.offset_polyline_miter(L_AXIS, 1.0, miter_limit=miter_limit)


# --------------------------------------------------------------------------
# wall_band
# --------------------------------------------------------------------------


def test_wall_band_single_segment_matches_thick_segment_outline() -> None:
    start, end = (0.0, 0.0), (10.0, 0.0)
    bands = g.wall_band((start, end), 2.0)
    assert len(bands) == 1
    assert_ring(bands[0], ((0.0, 1.0), (10.0, 1.0), (10.0, -1.0), (0.0, -1.0)))
    # Même rectangle que l'ancienne fonction, parcouru en sens inverse.
    assert_ring(bands[0], tuple(reversed(g.thick_segment_outline(start, end, 2.0))))


def test_wall_band_open_l_corner_is_mitered() -> None:
    # Épaisseur 2, donc demi-épaisseur 1. Face intérieure: y = 1 puis x = 9,
    # elles se coupent en (9, 1). Face extérieure: y = -1 puis x = 11, elles
    # se coupent en (11, -1). Aucun recouvrement, aucun petit carré au coin.
    (contour,) = g.wall_band(L_AXIS, 2.0)
    assert_ring(
        contour,
        (
            (0.0, 1.0),
            (9.0, 1.0),
            (9.0, 10.0),
            (11.0, 10.0),
            (11.0, -1.0),
            (0.0, -1.0),
        ),
    )


def test_wall_band_open_l_area_equals_axis_length_times_thickness() -> None:
    # À un angle droit, ce que le coin extérieur gagne, le coin intérieur le
    # perd exactement. Deux murs de 10 et 2 d'épaisseur: 40, pas 44 comme
    # avec deux rectangles indépendants qui se recouvrent.
    (contour,) = g.wall_band(L_AXIS, 2.0)
    assert g.polygon_area(contour) == pytest.approx(40.0, abs=ABS)


def test_wall_band_open_z_area_equals_axis_length_times_thickness() -> None:
    (contour,) = g.wall_band(Z_AXIS, 2.0)
    assert g.polygon_area(contour) == pytest.approx(60.0, abs=ABS)


def test_wall_band_closed_square_ring_areas_prove_the_joint() -> None:
    # Carré d'axe de côté c = 10, épaisseur e = 2. Si les coins sont
    # correctement raccordés, l'anneau extérieur est le carré de côté c + e
    # et l'anneau intérieur celui de côté c - e. Toute erreur de raccord
    # casse l'une des deux aires.
    outer, inner = g.wall_band(SQUARE_AXIS, 2.0, closed=True)
    assert g.polygon_area(outer) == pytest.approx(144.0, abs=ABS)  # (10 + 2)²
    assert g.polygon_area(inner) == pytest.approx(64.0, abs=ABS)  # (10 - 2)²
    assert g.polygon_area(outer) - g.polygon_area(inner) == pytest.approx(80.0, abs=ABS)


def test_wall_band_closed_square_ring_vertices_are_exact() -> None:
    outer, inner = g.wall_band(SQUARE_AXIS, 2.0, closed=True)
    assert_ring(outer, ((-1.0, -1.0), (11.0, -1.0), (11.0, 11.0), (-1.0, 11.0)))
    assert_ring(inner, ((1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)))


def test_wall_band_closed_is_independent_of_the_axis_orientation() -> None:
    # Un plan relevé dans le sens horaire décrit le même bâtiment.
    ccw_outer, ccw_inner = g.wall_band(SQUARE_AXIS, 2.0, closed=True)
    cw_outer, cw_inner = g.wall_band(tuple(reversed(SQUARE_AXIS)), 2.0, closed=True)
    assert g.polygon_area(cw_outer) == pytest.approx(g.polygon_area(ccw_outer), abs=ABS)
    assert g.polygon_area(cw_inner) == pytest.approx(g.polygon_area(ccw_inner), abs=ABS)
    assert g.bbox(cw_outer) == pytest.approx(g.bbox(ccw_outer), abs=ABS)
    assert g.bbox(cw_inner) == pytest.approx(g.bbox(ccw_inner), abs=ABS)


def test_wall_band_closed_returns_two_rings_of_the_same_size() -> None:
    rings = g.wall_band(SQUARE_AXIS, 2.0, closed=True)
    assert len(rings) == 2
    assert len(rings[0]) == len(rings[1]) == len(SQUARE_AXIS)


def test_wall_band_return_type_is_uniform() -> None:
    for rings in (g.wall_band(L_AXIS, 2.0), g.wall_band(SQUARE_AXIS, 2.0, closed=True)):
        assert isinstance(rings, tuple)
        for ring in rings:
            assert isinstance(ring, tuple)
            assert all(isinstance(coord, float) for point in ring for coord in point)


def test_wall_band_rings_are_valid_closed_polygons() -> None:
    for ring in g.wall_band(SQUARE_AXIS, 2.0, closed=True):
        assert_ring(g.close_ring(ring), ring)  # aucun sommet dupliqué


def test_wall_band_vertical_wall() -> None:
    (contour,) = g.wall_band(((0.0, 0.0), (0.0, 10.0)), 2.0)
    assert_ring(contour, ((-1.0, 0.0), (-1.0, 10.0), (1.0, 10.0), (1.0, 0.0)))


@pytest.mark.parametrize("thickness", [0.0, -1.0, -0.001])
def test_wall_band_non_positive_thickness_raises(thickness: float) -> None:
    with pytest.raises(InvalidGeometry):
        g.wall_band(L_AXIS, thickness)


@pytest.mark.parametrize("points", [(), ((0.0, 0.0),), ((2.0, 2.0), (2.0, 2.0))])
def test_wall_band_needs_two_distinct_points(points: tuple[Point2, ...]) -> None:
    with pytest.raises(InvalidGeometry):
        g.wall_band(points, 2.0)


def test_wall_band_closed_needs_three_distinct_points() -> None:
    with pytest.raises(InvalidGeometry):
        g.wall_band(((0.0, 0.0), (10.0, 0.0)), 2.0, closed=True)


# --------------------------------------------------------------------------
# split_run_by_openings
# --------------------------------------------------------------------------

RUN_START: Point2 = (0.0, 0.0)
RUN_END: Point2 = (10.0, 0.0)


def test_split_run_without_openings_returns_the_whole_segment() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ())
    assert runs == ((RUN_START, RUN_END),)


def test_split_run_one_centered_opening_gives_two_runs() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.5, 2.0),))
    assert len(runs) == 2
    assert_ring(runs[0], ((0.0, 0.0), (4.0, 0.0)))
    assert_ring(runs[1], ((6.0, 0.0), (10.0, 0.0)))


def test_split_run_two_openings_give_three_runs() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.25, 2.0), (0.75, 2.0)))
    assert len(runs) == 3
    assert_ring(runs[0], ((0.0, 0.0), (1.5, 0.0)))
    assert_ring(runs[1], ((3.5, 0.0), (6.5, 0.0)))
    assert_ring(runs[2], ((8.5, 0.0), (10.0, 0.0)))


def test_split_run_openings_are_sorted_by_position() -> None:
    ordered = g.split_run_by_openings(RUN_START, RUN_END, ((0.25, 2.0), (0.75, 2.0)))
    shuffled = g.split_run_by_openings(RUN_START, RUN_END, ((0.75, 2.0), (0.25, 2.0)))
    assert shuffled == ordered


def test_split_run_opening_straddling_the_start_gives_one_run() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.0, 2.0),))
    assert len(runs) == 1
    assert_ring(runs[0], ((1.0, 0.0), (10.0, 0.0)))


def test_split_run_opening_straddling_the_end_gives_one_run() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((1.0, 2.0),))
    assert len(runs) == 1
    assert_ring(runs[0], ((0.0, 0.0), (9.0, 0.0)))


def test_split_run_overlapping_openings_are_merged() -> None:
    # [2.5, 5.5] et [3.5, 6.5] se chevauchent: une seule baie de 2.5 à 6.5,
    # et surtout pas un tronçon plein inversé de 5.5 à 3.5.
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.4, 3.0), (0.5, 3.0)))
    assert len(runs) == 2
    assert_ring(runs[0], ((0.0, 0.0), (2.5, 0.0)))
    assert_ring(runs[1], ((6.5, 0.0), (10.0, 0.0)))


def test_split_run_touching_openings_are_merged() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.3, 2.0), (0.5, 2.0)))
    assert len(runs) == 2
    assert_ring(runs[0], ((0.0, 0.0), (2.0, 0.0)))
    assert_ring(runs[1], ((6.0, 0.0), (10.0, 0.0)))


def test_split_run_opening_covering_everything_gives_no_run() -> None:
    assert g.split_run_by_openings(RUN_START, RUN_END, ((0.5, 20.0),)) == ()


def test_split_run_nested_openings_are_merged() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.5, 6.0), (0.5, 2.0)))
    assert len(runs) == 2
    assert_ring(runs[0], ((0.0, 0.0), (2.0, 0.0)))
    assert_ring(runs[1], ((8.0, 0.0), (10.0, 0.0)))


def test_split_run_drops_the_slivers_it_would_leave() -> None:
    # Deux tronçons résiduels de 5e-13: en dessous de la tolérance, ils ne
    # sont pas renvoyés dégénérés, ils disparaissent.
    assert g.split_run_by_openings(RUN_START, RUN_END, ((0.5, 10.0 - 1e-12),)) == ()


def test_split_run_uses_the_tolerance_for_slivers() -> None:
    tol = Defaults(Unit.MILLIMETER).tolerance  # 0.1 unité de dessin
    openings = ((0.5, 9.9),)
    assert len(g.split_run_by_openings(RUN_START, RUN_END, openings)) == 2
    assert g.split_run_by_openings(RUN_START, RUN_END, openings, tol=tol) == ()


def test_split_run_on_an_oblique_wall_keeps_the_direction() -> None:
    runs = g.split_run_by_openings((0.0, 0.0), (6.0, 8.0), ((0.5, 2.0),))
    assert len(runs) == 2
    assert_ring(runs[0], ((0.0, 0.0), (2.4, 3.2)))
    assert_ring(runs[1], ((3.6, 4.8), (6.0, 8.0)))


def test_split_run_on_a_vertical_wall() -> None:
    runs = g.split_run_by_openings((5.0, 0.0), (5.0, 10.0), ((0.5, 2.0),))
    assert_ring(runs[0], ((5.0, 0.0), (5.0, 4.0)))
    assert_ring(runs[1], ((5.0, 6.0), (5.0, 10.0)))


def test_split_run_is_reversible_with_the_segment() -> None:
    forward = g.split_run_by_openings(RUN_START, RUN_END, ((0.25, 2.0),))
    backward = g.split_run_by_openings(RUN_END, RUN_START, ((0.75, 2.0),))
    assert len(forward) == len(backward) == 2
    # Même découpe, parcourue à l'envers.
    assert_ring(backward[0], tuple(reversed(forward[1])))
    assert_ring(backward[1], tuple(reversed(forward[0])))


def test_split_run_degenerate_segment_raises() -> None:
    with pytest.raises(InvalidGeometry):
        g.split_run_by_openings((3.0, 3.0), (3.0, 3.0), ((0.5, 1.0),))


@pytest.mark.parametrize("width", [0.0, -1.0])
def test_split_run_non_positive_opening_width_raises(width: float) -> None:
    with pytest.raises(InvalidGeometry):
        g.split_run_by_openings(RUN_START, RUN_END, ((0.5, width),))


@pytest.mark.parametrize("position", [-0.1, 1.5, 2.0])
def test_split_run_position_out_of_range_raises(position: float) -> None:
    with pytest.raises(InvalidParameter):
        g.split_run_by_openings(RUN_START, RUN_END, ((position, 1.0),))


def test_split_run_lengths_sum_to_the_wall_minus_its_openings() -> None:
    runs = g.split_run_by_openings(RUN_START, RUN_END, ((0.25, 2.0), (0.75, 2.0)))
    total = sum(g.distance(a, b) for a, b in runs)
    assert total == pytest.approx(10.0 - 4.0, abs=ABS)


def test_wall_functions_do_not_mutate_their_input() -> None:
    axis = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    openings = [(0.5, 2.0)]
    snapshot_axis, snapshot_openings = list(axis), list(openings)
    g.offset_polyline_miter(axis, 1.0)
    g.wall_band(axis, 2.0)
    g.split_run_by_openings(axis[0], axis[1], openings)
    assert axis == snapshot_axis
    assert openings == snapshot_openings


# --------------------------------------------------------------------------
# normalize_angle
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle", "expected"),
    [
        (0.0, 0.0),
        (math.pi, math.pi),
        (2 * math.pi, 0.0),
        (-math.pi / 2, 3 * math.pi / 2),
        (5 * math.pi, math.pi),
        (-5 * math.pi, math.pi),
    ],
)
def test_normalize_angle(angle: float, expected: float) -> None:
    assert g.normalize_angle(angle) == pytest.approx(expected, abs=ABS)


def test_normalize_angle_stays_in_range() -> None:
    for k in range(-20, 21):
        value = g.normalize_angle(k * 0.7)
        assert 0.0 <= value < 2 * math.pi


# --------------------------------------------------------------------------
# Pureté
# --------------------------------------------------------------------------


def test_functions_do_not_mutate_their_input() -> None:
    points = [(0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0), (0.0, 0.0)]
    snapshot = list(points)
    g.polygon_area(points)
    g.bbox(points)
    g.close_ring(points)
    g.polygon_is_clockwise(points[:-1])
    assert points == snapshot
