"""Tests de la logique métier, contre le backend d'enregistrement.

Aucun de ces tests ne touche AutoCAD ni un fichier. Ils affirment sur ce que la
logique a **décidé** de dessiner, ce qui est exactement la propriété que la
séparation logique/backend rend possible.
"""

from __future__ import annotations

import math

import pytest

from autocad_mcp.backends.recording import RecordingBackend
from autocad_mcp.errors import InvalidParameter
from autocad_mcp.geometry import polygon_area
from autocad_mcp.model.ops import OperationBatch
from autocad_mcp.ops.architecture import (
    door,
    door_in_wall,
    label,
    room,
    wall,
    wall_run,
    window,
)
from autocad_mcp.units import Defaults, Unit


def run(backend: RecordingBackend, ops: list, tag: str = "test"):  # type: ignore[no-untyped-def]
    return backend.execute(OperationBatch(tuple(ops), label=tag))


# --------------------------------------------------------------------------
# Murs
# --------------------------------------------------------------------------


def test_un_mur_epais_est_une_polyligne_fermee(defaults: Defaults) -> None:
    """Régression: l'ancien code produisait quatre segments indépendants.

    Un mur en quatre lignes n'est ni sélectionnable d'un clic, ni hachurable,
    et son aire n'est pas calculable. C'était un mur pour l'œil seulement.
    """
    ops = wall((0.0, 0.0), (5.0, 0.0), defaults)
    polylines = [op for op in ops if op.kind == "polyline"]
    lines = [op for op in ops if op.kind == "line"]

    assert len(polylines) == 1
    assert not lines
    assert polylines[0].closed is True
    assert len(polylines[0].points) == 4


def test_l_aire_d_un_mur_vaut_longueur_par_epaisseur(defaults: Defaults) -> None:
    ops = wall((0.0, 0.0), (5.0, 0.0), defaults, thickness=0.2)
    outline = next(op for op in ops if op.kind == "polyline").points
    assert polygon_area(outline) == pytest.approx(5.0 * 0.2)


def test_un_mur_sans_epaisseur_reste_un_axe(defaults: Defaults) -> None:
    """Une épaisseur nulle doit donner un trait, pas un rectangle dégénéré."""
    ops = wall((0.0, 0.0), (5.0, 0.0), defaults, thickness=0.0)
    assert [op.kind for op in ops] == ["ensure_layer", "line"]


def test_un_mur_atterrit_sur_le_calque_des_murs(recorder: RecordingBackend, defaults: Defaults) -> None:
    run(recorder, wall((0.0, 0.0), (3.0, 0.0), defaults))
    assert "WALLS" in recorder.layers
    assert len(recorder.ops_on_layer("WALLS")) == 1


def test_l_epaisseur_par_defaut_suit_l_unite_du_document() -> None:
    """Régression: l'ancien code codait 0.1 en dur, sans unité.

    Dans un dessin en millimètres, un mur d'un dixième de millimètre est
    invisible. La même intention doit donner la même épaisseur physique quelle
    que soit l'unité.
    """
    epaisseurs = {}
    for unit, longueur in ((Unit.METER, 5.0), (Unit.MILLIMETER, 5000.0), (Unit.CENTIMETER, 500.0)):
        d = Defaults(unit)
        ops = wall((0.0, 0.0), (longueur, 0.0), d)
        aire = polygon_area(next(op for op in ops if op.kind == "polyline").points)
        # Aire ramenée en mètres carrés: doit être identique partout.
        facteur = {Unit.METER: 1.0, Unit.MILLIMETER: 1e-6, Unit.CENTIMETER: 1e-4}[unit]
        epaisseurs[unit] = aire * facteur

    valeurs = list(epaisseurs.values())
    assert valeurs[0] == pytest.approx(valeurs[1])
    assert valeurs[0] == pytest.approx(valeurs[2])


# --------------------------------------------------------------------------
# Portes: la régression la plus visible du projet
# --------------------------------------------------------------------------


ORIENTATIONS = [
    ("est", (0.0, 0.0), (5.0, 0.0)),
    ("nord", (0.0, 0.0), (0.0, 5.0)),
    ("ouest", (5.0, 0.0), (0.0, 0.0)),
    ("sud", (0.0, 5.0), (0.0, 0.0)),
    ("oblique", (0.0, 0.0), (4.0, 3.0)),
]


@pytest.mark.parametrize(("nom", "debut", "fin"), ORIENTATIONS)
def test_le_battant_suit_le_mur_porteur(
    nom: str, debut: tuple[float, float], fin: tuple[float, float], defaults: Defaults
) -> None:
    """Régression: l'arc était tracé de 0 à 90 degrés en dur.

    Le battant pointait donc vers l'est quelle que soit l'orientation du mur,
    ce qui est faux sur trois plans sur quatre.
    """
    ops = door_in_wall(debut, fin, defaults)
    arc = next(op for op in ops if op.kind == "arc")
    ouverture = (arc.end_angle - arc.start_angle) % (2 * math.pi)
    assert ouverture == pytest.approx(math.pi / 2, abs=1e-9), nom


def test_quatre_orientations_donnent_quatre_arcs_differents(defaults: Defaults) -> None:
    """Le test le plus direct: l'ancien code donnait quatre fois le même arc."""
    departs = set()
    for _nom, debut, fin in ORIENTATIONS[:4]:
        ops = door_in_wall(debut, fin, defaults)
        arc = next(op for op in ops if op.kind == "arc")
        departs.add(round(arc.start_angle, 6))
    assert len(departs) == 4


def test_le_sens_d_ouverture_change_le_battant(defaults: Defaults) -> None:
    gauche = door_in_wall((0.0, 0.0), (5.0, 0.0), defaults, hand="left")
    droite = door_in_wall((0.0, 0.0), (5.0, 0.0), defaults, hand="right")
    a = next(op for op in gauche if op.kind == "arc")
    b = next(op for op in droite if op.kind == "arc")
    assert a.start_angle != pytest.approx(b.start_angle)


def test_la_porte_atterrit_sur_le_calque_des_portes(recorder: RecordingBackend, defaults: Defaults) -> None:
    run(recorder, door((0.0, 0.0), (0.0, 0.9), defaults))
    assert "DOORS" in recorder.layers
    assert len(recorder.ops_on_layer("DOORS")) == 2  # vantail et arc


def test_position_de_porte_hors_bornes_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        door_in_wall((0.0, 0.0), (5.0, 0.0), defaults, position=1.5)


def test_porte_sur_mur_de_longueur_nulle_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        door_in_wall((2.0, 2.0), (2.0, 2.0), defaults)


# --------------------------------------------------------------------------
# Fenêtres
# --------------------------------------------------------------------------


def test_la_fenetre_a_un_dormant_et_deux_traits_de_vitrage(defaults: Defaults) -> None:
    ops = window((0.0, 0.0), (1.2, 0.0), defaults)
    assert len([op for op in ops if op.kind == "polyline"]) == 1
    assert len([op for op in ops if op.kind == "line"]) == 2


def test_l_ecart_du_vitrage_suit_l_epaisseur_et_non_une_constante(defaults: Defaults) -> None:
    """Régression: l'ancien code décalait de 0.05 sans unité ni rapport au mur."""
    minces = window((0.0, 0.0), (1.0, 0.0), defaults, thickness=0.1)
    epais = window((0.0, 0.0), (1.0, 0.0), defaults, thickness=0.4)
    ecart_mince = abs(next(op for op in minces if op.kind == "line").start[1])
    ecart_epais = abs(next(op for op in epais if op.kind == "line").start[1])
    assert ecart_epais == pytest.approx(ecart_mince * 4)


# --------------------------------------------------------------------------
# Pièces et enfilades
# --------------------------------------------------------------------------


def test_une_piece_est_un_anneau_de_murs_raccordes(recorder: RecordingBackend, defaults: Defaults) -> None:
    """Régression: quatre rectangles indépendants se chevauchaient aux coins.

    Une pièce rend désormais deux contours fermés, la face extérieure et la
    face intérieure du mur d'enceinte, dont les angles sont mitrés.
    """
    run(recorder, room((0.0, 0.0), (5.0, 4.0), defaults, name="Salon"))
    assert len(recorder.ops_of_kind("polyline")) == 2
    assert len(recorder.ops_of_kind("text")) == 1


def test_les_deux_anneaux_d_une_piece_ont_les_aires_attendues(defaults: Defaults) -> None:
    """La preuve arithmétique du raccord d'angle.

    Pour un axe carré de côté c et une épaisseur e, l'anneau extérieur vaut
    exactement (c + e) au carré et l'intérieur (c - e) au carré. Des murs
    tracés séparément donneraient quatre recouvrements de e sur e.
    """
    ops = room((0.0, 0.0), (6.0, 6.0), defaults, thickness=0.2, show_area=False)
    aires = sorted(
        polygon_area(op.points) for op in ops if op.kind == "polyline"
    )
    assert aires[1] == pytest.approx(6.2 * 6.2)
    assert aires[0] == pytest.approx(5.8 * 5.8)


def test_l_etiquette_porte_la_surface_utile(defaults: Defaults) -> None:
    """La surface annoncée est celle du dedans, pas celle des axes.

    Un plan cote la surface habitable, à l'intérieur des murs. Pour un axe de
    5 sur 4 et des murs de 20 cm, cela fait 4,8 sur 3,8.
    """
    ops = room((0.0, 0.0), (5.0, 4.0), defaults, thickness=0.2, name="Salon")
    texte = next(op for op in ops if op.kind == "text").text
    assert "Salon" in texte
    assert f"{4.8 * 3.8:.2f}" in texte


def test_l_etiquette_va_sur_le_calque_d_annotation(recorder: RecordingBackend, defaults: Defaults) -> None:
    """Une annotation mêlée aux murs disparaît quand on gèle leur calque."""
    run(recorder, room((0.0, 0.0), (3.0, 3.0), defaults, name="Bureau"))
    textes = recorder.ops_of_kind("text")
    assert all(op.style.layer == "ANNOTATION" for op in textes)


def test_une_piece_se_construit_quel_que_soit_l_ordre_des_coins(defaults: Defaults) -> None:
    a = room((0.0, 0.0), (5.0, 4.0), defaults, show_area=True, name=None)
    b = room((5.0, 4.0), (0.0, 0.0), defaults, show_area=True, name=None)
    aires = [
        next(op for op in ops if op.kind == "text").text for ops in (a, b)
    ]
    assert aires[0] == aires[1]


def test_une_enfilade_fermee_rend_deux_anneaux(recorder: RecordingBackend, defaults: Defaults) -> None:
    """Un contour fermé est un mur d'enceinte: une face dehors, une dedans."""
    points = [(0.0, 0.0), (6.0, 0.0), (6.0, 4.0), (0.0, 4.0)]
    run(recorder, wall_run(points, defaults, closed=True))
    assert len(recorder.ops_of_kind("polyline")) == 2


def test_une_enfilade_ouverte_est_un_seul_contour(recorder: RecordingBackend, defaults: Defaults) -> None:
    """Une enfilade ouverte forme un ruban unique, aboutée à ses deux bouts.

    Trois murs distincts laissaient deux recouvrements aux angles.
    """
    points = [(0.0, 0.0), (6.0, 0.0), (6.0, 4.0), (0.0, 4.0)]
    run(recorder, wall_run(points, defaults, closed=False))
    contours = recorder.ops_of_kind("polyline")
    assert len(contours) == 1
    # Deux faces de trois sommets, plus rien: les abouts sont portés par la
    # fermeture du contour.
    assert len(contours[0].points) == 8


def test_une_enfilade_d_un_seul_point_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        wall_run([(0.0, 0.0)], defaults)


def test_une_etiquette_vide_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        label((0.0, 0.0), "   ", defaults)


# --------------------------------------------------------------------------
# Propriété d'ensemble
# --------------------------------------------------------------------------


def test_la_logique_metier_ne_produit_que_des_donnees(defaults: Defaults) -> None:
    """Aucune opération ne doit porter de méthode qui dessine.

    C'est la garantie que la couche métier reste inerte et donc testable.
    """
    ops = room((0.0, 0.0), (5.0, 4.0), defaults, name="Salon")
    for op in ops:
        methodes = [
            nom
            for nom in dir(op)
            if not nom.startswith("_") and callable(getattr(op, nom))
        ]
        assert not methodes, f"{op.kind} expose {methodes}"


def test_un_plan_complet_s_execute_en_un_seul_lot(recorder: RecordingBackend, defaults: Defaults) -> None:
    """Régression de performance: l'ancien code dessinait entité par entité.

    Un lot unique permet une seule marque d'annulation et un seul
    rafraîchissement, au lieu d'un par entité.
    """
    plan = []
    plan += room((0.0, 0.0), (5.0, 4.0), defaults, name="Salon")
    plan += room((5.0, 0.0), (9.0, 4.0), defaults, name="Cuisine")
    plan += door_in_wall((5.0, 0.0), (5.0, 4.0), defaults, position=0.5)
    plan += window((0.0, 4.0), (2.0, 4.0), defaults)

    result = run(recorder, plan, tag="plan")

    assert result.ok
    assert len(recorder.batches) == 1
    assert result.created
    assert all(not h.startswith(("line_", "text_")) for h in result.to_dict()["handles"])
