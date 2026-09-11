"""Tests du réseau de murs raccordés et des baies qui percent le mur.

Ces trois défauts étaient visibles au rendu et sont la raison d'être de
``wall_network``:

* deux murs perpendiculaires se chevauchaient au lieu de former un angle ;
* une enfilade traversait ce qu'elle rencontrait ;
* une baie posait un symbole par-dessus un mur resté plein.
"""

from __future__ import annotations

import math

import pytest

from autocad_mcp.errors import InvalidParameter
from autocad_mcp.geometry import bbox, polygon_area
from autocad_mcp.ops.architecture import Opening, wall_network
from autocad_mcp.units import Defaults, Unit


def polylines(ops: list) -> list:  # type: ignore[no-untyped-def]
    return [op for op in ops if op.kind == "polyline"]


# --------------------------------------------------------------------------
# Raccord d'angle
# --------------------------------------------------------------------------


def test_un_contour_ferme_rend_deux_anneaux(defaults: Defaults) -> None:
    ops = wall_network([(0.0, 0.0), (6.0, 0.0), (6.0, 6.0), (0.0, 6.0)], defaults, closed=True)
    assert len(polylines(ops)) == 2


@pytest.mark.parametrize(("cote", "epaisseur"), [(6.0, 0.2), (10.0, 0.5), (3.0, 0.07)])
def test_les_aires_prouvent_le_raccord(cote: float, epaisseur: float, defaults: Defaults) -> None:
    """Le test décisif.

    Pour un axe carré de côté c et une épaisseur e, l'anneau extérieur vaut
    exactement (c + e) au carré et l'intérieur (c - e) au carré. Quatre
    rectangles indépendants donneraient quatre recouvrements de e sur e, donc
    une aire de mur fausse de 4 e carrés.
    """
    axe = [(0.0, 0.0), (cote, 0.0), (cote, cote), (0.0, cote)]
    ops = wall_network(axe, defaults, thickness=epaisseur, closed=True)
    aires = sorted(polygon_area(op.points) for op in polylines(ops))
    assert aires[1] == pytest.approx((cote + epaisseur) ** 2)
    assert aires[0] == pytest.approx((cote - epaisseur) ** 2)


def test_l_aire_du_mur_est_exacte(defaults: Defaults) -> None:
    """La matière du mur, déduite des deux anneaux, vaut le périmètre fois l'épaisseur."""
    cote, e = 8.0, 0.25
    ops = wall_network(
        [(0.0, 0.0), (cote, 0.0), (cote, cote), (0.0, cote)],
        defaults, thickness=e, closed=True,
    )
    aires = sorted(polygon_area(op.points) for op in polylines(ops))
    matiere = aires[1] - aires[0]
    assert matiere == pytest.approx(4 * cote * e)


def test_une_enfilade_ouverte_est_un_ruban_unique(defaults: Defaults) -> None:
    ops = wall_network([(0.0, 0.0), (6.0, 0.0), (6.0, 4.0)], defaults)
    contours = polylines(ops)
    assert len(contours) == 1
    assert len(contours[0].points) == 6


def test_un_mur_seul_reste_un_rectangle(defaults: Defaults) -> None:
    ops = wall_network([(0.0, 0.0), (5.0, 0.0)], defaults, thickness=0.2)
    contours = polylines(ops)
    assert len(contours) == 1
    assert polygon_area(contours[0].points) == pytest.approx(5.0 * 0.2)


def test_un_angle_rentrant_se_raccorde_aussi(defaults: Defaults) -> None:
    """Une forme en L rentrant ne doit pas produire de recouvrement."""
    ops = wall_network(
        [(0.0, 0.0), (8.0, 0.0), (8.0, 8.0), (4.0, 8.0), (4.0, 4.0), (0.0, 4.0)],
        defaults, thickness=0.2, closed=True,
    )
    assert len(polylines(ops)) == 2


# --------------------------------------------------------------------------
# Baies: le mur est réellement percé
# --------------------------------------------------------------------------


def test_une_baie_coupe_le_mur_en_deux(defaults: Defaults) -> None:
    """Régression: le symbole se posait sur un mur resté plein."""
    ops = wall_network(
        [(0.0, 0.0), (6.0, 0.0)], defaults, thickness=0.2,
        openings=[Opening(segment=0, position=0.5, width=1.0)],
    )
    assert len(polylines(ops)) == 2


def test_la_matiere_perdue_vaut_la_baie(defaults: Defaults) -> None:
    """Percer enlève exactement la largeur de la baie, ni plus ni moins."""
    e, largeur = 0.2, 1.2
    plein = wall_network([(0.0, 0.0), (6.0, 0.0)], defaults, thickness=e)
    perce = wall_network(
        [(0.0, 0.0), (6.0, 0.0)], defaults, thickness=e,
        openings=[Opening(segment=0, position=0.5, width=largeur)],
        show_symbols=False,
    )
    aire_pleine = sum(polygon_area(op.points) for op in polylines(plein))
    aire_percee = sum(polygon_area(op.points) for op in polylines(perce))
    assert aire_pleine - aire_percee == pytest.approx(largeur * e)


def test_deux_baies_donnent_trois_troncons(defaults: Defaults) -> None:
    ops = wall_network(
        [(0.0, 0.0), (10.0, 0.0)], defaults,
        openings=[
            Opening(segment=0, position=0.25, width=1.0),
            Opening(segment=0, position=0.75, width=1.0),
        ],
        show_symbols=False,
    )
    assert len(polylines(ops)) == 3


def test_une_baie_sur_un_contour_ferme_ne_casse_pas_les_angles(defaults: Defaults) -> None:
    """Percer un mur d'enceinte ne doit pas rouvrir les coins."""
    ops = wall_network(
        [(0.0, 0.0), (8.0, 0.0), (8.0, 6.0), (0.0, 6.0)], defaults, closed=True,
        openings=[Opening(segment=0, position=0.5, width=1.5, kind="window")],
        show_symbols=False,
    )
    contours = polylines(ops)
    # Un segment coupé en deux, trois segments entiers: cinq tronçons.
    assert len(contours) == 5
    limites = bbox([p for op in contours for p in op.points])
    assert limites == pytest.approx((-0.1, -0.1, 8.1, 6.1))


def test_le_symbole_de_porte_suit_le_mur_porteur(defaults: Defaults) -> None:
    """Le bug historique orientait tous les battants vers l'est."""
    departs = set()
    axes = [
        [(0.0, 0.0), (6.0, 0.0)],
        [(0.0, 0.0), (0.0, 6.0)],
        [(6.0, 0.0), (0.0, 0.0)],
        [(0.0, 6.0), (0.0, 0.0)],
    ]
    for axe in axes:
        ops = wall_network(
            axe, defaults, openings=[Opening(segment=0, position=0.5, width=0.9)]
        )
        arc = next(op for op in ops if op.kind == "arc")
        departs.add(round(arc.start_angle, 6))
        ouverture = (arc.end_angle - arc.start_angle) % (2 * math.pi)
        assert ouverture == pytest.approx(math.pi / 2)
    assert len(departs) == 4


@pytest.mark.parametrize("hand", ["left", "right"])
def test_le_battant_ferme_occupe_la_baie(hand: str, defaults: Defaults) -> None:
    """Régression: le battant se refermait hors de la baie.

    Le module passait à la fonction d'arc la position ouverte du battant alors
    qu'elle attend la position fermée. Résultat mesuré sur une porte d'entrée:
    le battant se refermait quatre-vingt-dix centimètres en dehors du bâtiment,
    ce qui décalait aussi les limites du dessin.

    La position fermée doit tomber sur l'un des deux jambages, et la position
    ouverte doit être perpendiculaire au mur.
    """
    largeur = 0.9
    ops = wall_network(
        [(0.0, 0.0), (10.0, 0.0)], defaults,
        openings=[Opening(segment=0, position=0.5, width=largeur, kind="door", hand=hand)],
    )
    arc = next(op for op in ops if op.kind == "arc")
    cx, cy = arc.center[0], arc.center[1]

    ferme_angle = arc.start_angle if hand == "left" else arc.end_angle
    ouvert_angle = arc.end_angle if hand == "left" else arc.start_angle
    ferme = (cx + arc.radius * math.cos(ferme_angle), cy + arc.radius * math.sin(ferme_angle))
    ouvert = (cx + arc.radius * math.cos(ouvert_angle), cy + arc.radius * math.sin(ouvert_angle))

    # La baie va de 4,55 à 5,45 sur un mur de dix mètres.
    assert ferme[1] == pytest.approx(0.0, abs=1e-9), "le battant fermé quitte l'axe du mur"
    assert 5.0 - largeur / 2 - 1e-9 <= ferme[0] <= 5.0 + largeur / 2 + 1e-9, (
        "le battant fermé tombe hors de la baie"
    )
    assert ouvert[1] == pytest.approx(largeur), "le battant ouvert n'est pas perpendiculaire"


def test_le_battant_ouvert_est_le_trait_dessine(defaults: Defaults) -> None:
    """Le trait du vantail rejoint bien l'extrémité libre de l'arc."""
    ops = wall_network(
        [(0.0, 0.0), (6.0, 0.0)], defaults,
        openings=[Opening(segment=0, position=0.5, width=0.9, kind="door")],
    )
    arc = next(op for op in ops if op.kind == "arc")
    trait = next(op for op in ops if op.kind == "line")
    longueur = math.hypot(trait.end[0] - trait.start[0], trait.end[1] - trait.start[1])
    assert longueur == pytest.approx(arc.radius)
    assert trait.start[:2] == pytest.approx(arc.center[:2])


def test_une_porte_d_entree_ne_deborde_pas_du_batiment(defaults: Defaults) -> None:
    """Le symptôme qui a révélé le défaut: des limites de dessin négatives."""
    ops = wall_network(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 7.0), (0.0, 7.0)], defaults,
        thickness=0.25, closed=True,
        openings=[Opening(segment=0, position=0.1, width=0.9, kind="door", hand="left")],
    )
    points = [p for op in ops if op.kind == "polyline" for p in op.points]
    points += [op.center[:2] for op in ops if op.kind == "arc"]
    xmin, ymin, _, _ = bbox(points)
    assert xmin >= -0.125 - 1e-9
    assert ymin >= -0.125 - 1e-9


def test_une_fenetre_pose_son_vitrage_dans_la_coupe(defaults: Defaults) -> None:
    ops = wall_network(
        [(0.0, 0.0), (6.0, 0.0)], defaults, thickness=0.2,
        openings=[Opening(segment=0, position=0.5, width=1.2, kind="window")],
    )
    traits = [op for op in ops if op.kind == "line"]
    assert len(traits) == 3
    for trait in traits:
        assert trait.start[0] == pytest.approx(2.4)
        assert trait.end[0] == pytest.approx(3.6)


def test_un_passage_n_a_pas_de_symbole(defaults: Defaults) -> None:
    ops = wall_network(
        [(0.0, 0.0), (6.0, 0.0)], defaults,
        openings=[Opening(segment=0, position=0.5, width=1.0, kind="passage")],
    )
    assert not [op for op in ops if op.kind in {"line", "arc"}]
    assert len(polylines(ops)) == 2


def test_une_baie_hors_enfilade_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        wall_network(
            [(0.0, 0.0), (6.0, 0.0)], defaults,
            openings=[Opening(segment=5, position=0.5, width=1.0)],
        )


def test_un_reseau_d_un_seul_point_est_refuse(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        wall_network([(0.0, 0.0)], defaults)


def test_l_epaisseur_suit_l_unite(defaults: Defaults) -> None:
    """Le même mur physique quelle que soit l'unité du document."""
    mesures = {}
    for unit, cote in ((Unit.METER, 6.0), (Unit.MILLIMETER, 6000.0), (Unit.FOOT, 19.685)):
        d = Defaults(unit)
        ops = wall_network([(0.0, 0.0), (cote, 0.0)], d)
        aire = polygon_area(polylines(ops)[0].points)
        # Épaisseur déduite, ramenée en mètres.
        facteur = {Unit.METER: 1.0, Unit.MILLIMETER: 0.001, Unit.FOOT: 0.3048}[unit]
        mesures[unit] = (aire / cote) * facteur
    valeurs = list(mesures.values())
    assert valeurs[0] == pytest.approx(valeurs[1])
    assert valeurs[0] == pytest.approx(valeurs[2], rel=1e-4)
