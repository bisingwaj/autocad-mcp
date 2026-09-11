"""La troisième dimension: un volume doit tomber sur son plan.

Ces tests portent surtout sur ce qu'une baie devient en volume. Un percement
en plan est un trou traversant; en volume il ne l'est qu'entre deux hauteurs,
et ce qui reste est de la maçonnerie. Un prototype qui perce sur toute la
hauteur produit des murs ajourés du sol au plafond.
"""

from __future__ import annotations

import pytest

from autocad_mcp.errors import InvalidParameter
from autocad_mcp.model.ops import validate
from autocad_mcp.ops.architecture import Opening, wall_network, wall_panels
from autocad_mcp.ops.volume import extrude_ring, slab, wall_volume
from autocad_mcp.units import Defaults, Unit

CARRE = [(0.0, 0.0), (8.0, 0.0), (8.0, 5.0), (0.0, 5.0)]


def altitudes(ops: list) -> set[float]:  # type: ignore[no-untyped-def]
    return {round(v[2], 4) for op in ops if op.kind == "mesh" for v in op.vertices}


def maillages(ops: list) -> list:  # type: ignore[no-untyped-def]
    return [op for op in ops if op.kind == "mesh"]


# ---------------------------------------------------------------------------
# Élévation d'un contour
# ---------------------------------------------------------------------------


def test_un_prisme_a_deux_fois_plus_de_sommets_que_son_contour() -> None:
    mesh = extrude_ring([(0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)], 0.0, 3.0)
    assert len(mesh.vertices) == 8
    # Quatre faces latérales, plus le dessus et le dessous.
    assert len(mesh.faces) == 6
    validate(mesh)


def test_un_prisme_sans_couvercle_n_a_que_ses_faces_laterales() -> None:
    mesh = extrude_ring([(0.0, 0.0), (2.0, 0.0), (2.0, 1.0)], 0.0, 3.0, cap=False)
    assert len(mesh.faces) == 3


def test_les_altitudes_sont_celles_demandees() -> None:
    mesh = extrude_ring([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], 1.5, 4.5)
    assert {round(v[2], 4) for v in mesh.vertices} == {1.5, 4.5}


def test_un_contour_trop_court_est_refuse() -> None:
    with pytest.raises(InvalidParameter):
        extrude_ring([(0.0, 0.0), (1.0, 0.0)], 0.0, 3.0)


def test_une_hauteur_nulle_est_refusee() -> None:
    """Un prisme plat n'est pas un volume, c'est un plan qu'on sait déjà tracer."""
    with pytest.raises(InvalidParameter):
        extrude_ring([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], 2.0, 2.0)


# ---------------------------------------------------------------------------
# Ce qu'une baie devient en volume
# ---------------------------------------------------------------------------


def test_un_mur_plein_monte_du_sol_au_plafond(defaults: Defaults) -> None:
    ops = wall_volume([(0.0, 0.0), (6.0, 0.0)], defaults)
    assert altitudes(ops) == {0.0, defaults.wall_height}


def test_une_fenetre_garde_son_allege_et_son_linteau(defaults: Defaults) -> None:
    """Le test qui sépare un mur percé d'un mur ajouré."""
    ops = wall_volume(
        [(0.0, 0.0), (6.0, 0.0)], defaults,
        openings=[Opening(segment=0, position=0.5, width=1.4, kind="window")],
    )
    assert altitudes(ops) == {
        0.0,
        defaults.sill_height,
        defaults.window_head,
        defaults.wall_height,
    }
    # Deux tronçons pleins, plus l'allège et le linteau de la baie.
    assert len(maillages(ops)) == 4


def test_une_porte_garde_son_linteau_seul(defaults: Defaults) -> None:
    """Le passage descend jusqu'au sol: pas d'allège sous une porte."""
    ops = wall_volume(
        [(0.0, 0.0), (6.0, 0.0)], defaults,
        openings=[Opening(segment=0, position=0.5, width=0.9, kind="door")],
    )
    assert altitudes(ops) == {0.0, defaults.door_head, defaults.wall_height}
    assert len(maillages(ops)) == 3


def test_un_passage_libre_ne_garde_rien(defaults: Defaults) -> None:
    ops = wall_volume(
        [(0.0, 0.0), (6.0, 0.0)], defaults,
        openings=[Opening(segment=0, position=0.5, width=1.0, kind="passage")],
    )
    assert len(maillages(ops)) == 2  # les deux tronçons pleins, rien entre eux


def test_un_mur_plus_bas_que_son_linteau_n_en_porte_pas(defaults: Defaults) -> None:
    """Une hauteur sous linteau demande que le mur la dépasse."""
    ops = wall_volume(
        [(0.0, 0.0), (6.0, 0.0)], defaults, height=defaults.door_head,
        openings=[Opening(segment=0, position=0.5, width=0.9, kind="door")],
    )
    assert len(maillages(ops)) == 2


# ---------------------------------------------------------------------------
# Le volume coïncide avec le plan
# ---------------------------------------------------------------------------


def test_le_volume_reprend_les_panneaux_du_plan(defaults: Defaults) -> None:
    """Plan et volume partagent le même découpage, donc le même raccord d'angle.

    Si les deux le calculaient séparément, un volume finirait par déborder de
    son dessin sans que rien ne le signale.
    """
    baies = [Opening(segment=0, position=0.5, width=0.9, kind="door")]
    panneaux = wall_panels(CARRE, defaults, thickness=0.2, closed=True, openings=baies)
    pleins = [p for p in panneaux if p.kind == "solid"]

    plan = wall_network(CARRE, defaults, thickness=0.2, closed=True, openings=baies)
    contours = [op.points for op in plan if op.kind == "polyline"]

    assert len(contours) == len(pleins)
    assert contours[0] == pleins[0].points


def test_l_emprise_au_sol_du_volume_egale_la_maconnerie_du_plan(
    defaults: Defaults,
) -> None:
    """La matière est la même vue de dessus et vue en volume.

    Le plan d'un contour fermé sans baie rend deux anneaux, l'extérieur et
    l'intérieur, alors que le volume élève un panneau par mur. Les deux
    représentations diffèrent, mais la maçonnerie qu'elles décrivent est la
    même: c'est cette aire qu'il faut comparer, pas la forme des contours.
    """
    from autocad_mcp.geometry import polygon_area

    cote, epaisseur = 8.0, 0.2
    axe = [(0.0, 0.0), (cote, 0.0), (cote, cote), (0.0, cote)]

    volume = wall_volume(axe, defaults, thickness=epaisseur, closed=True)
    au_sol = sum(
        polygon_area([(v[0], v[1]) for v in op.vertices if v[2] == 0.0])
        for op in maillages(volume)
    )

    plan = wall_network(axe, defaults, thickness=epaisseur, closed=True)
    aires = sorted(
        polygon_area(op.points) for op in plan if op.kind == "polyline"
    )
    couronne = aires[1] - aires[0]

    assert au_sol == pytest.approx(couronne)
    assert couronne == pytest.approx(4 * cote * epaisseur)


def test_toutes_les_operations_de_volume_sont_valides(defaults: Defaults) -> None:
    ops = wall_volume(
        CARRE, defaults, thickness=0.2, closed=True,
        openings=[Opening(segment=0, position=0.5, width=1.4, kind="window")],
    )
    for op in ops:
        validate(op)


# ---------------------------------------------------------------------------
# Unités et dalle
# ---------------------------------------------------------------------------


def test_la_hauteur_suit_l_unite_du_document() -> None:
    """Deux mètres cinquante restent deux mètres cinquante en millimètres."""
    en_m = wall_volume([(0.0, 0.0), (6.0, 0.0)], Defaults(Unit.METER))
    en_mm = wall_volume([(0.0, 0.0), (6000.0, 0.0)], Defaults(Unit.MILLIMETER))
    assert max(altitudes(en_m)) == pytest.approx(2.5)
    assert max(altitudes(en_mm)) == pytest.approx(2500.0)


def test_la_dalle_se_pose_sous_le_niveau(defaults: Defaults) -> None:
    """Les murs élevés depuis zéro doivent reposer dessus, pas la traverser."""
    ops = slab(CARRE, defaults)
    assert altitudes(ops) == {0.0, -defaults.slab_thickness}


def test_une_dalle_sans_epaisseur_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        slab(CARRE, defaults, thickness=0.0)


def test_un_reseau_d_un_seul_point_est_refuse(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        wall_volume([(0.0, 0.0)], defaults)


def test_une_hauteur_de_mur_nulle_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        wall_volume([(0.0, 0.0), (6.0, 0.0)], defaults, height=0.0)
