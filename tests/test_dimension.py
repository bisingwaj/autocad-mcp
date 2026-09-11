"""Cotation automatique.

Coter à la main est le travail le plus mécanique d'un plan: les mesures
existent déjà dans la géométrie. Ces tests vérifient les trois choses qui font
la différence entre des cotes posées et un plan coté: la justesse des mesures,
l'échelonnement des lignes, et le sens de lecture du texte.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from autocad_mcp.errors import InvalidParameter
from autocad_mcp.geometry import distance
from autocad_mcp.model.ops import validate
from autocad_mcp.ops.architecture import Opening
from autocad_mcp.ops.dimension import dimension_run, dimension_walls
from autocad_mcp.units import Defaults, Unit

CONTOUR = [(0.0, 0.0), (9.0, 0.0), (9.0, 6.0), (0.0, 6.0)]

BAIES = [
    Opening(segment=0, position=0.18, width=1.6, kind="window"),
    Opening(segment=0, position=0.62, width=1.0, kind="door"),
    Opening(segment=1, position=0.50, width=1.4, kind="window"),
]


def cotes(ops: list) -> list:  # type: ignore[no-untyped-def]
    return [op for op in ops if op.kind == "dim_aligned"]


def mesure(cote) -> float:  # type: ignore[no-untyped-def]
    return distance(cote.p1, cote.p2)


# ---------------------------------------------------------------------------
# Justesse des mesures
# ---------------------------------------------------------------------------


def test_un_mur_nu_recoit_sa_cote_hors_tout(defaults: Defaults) -> None:
    lignes = cotes(dimension_run((0.0, 0.0), (7.5, 0.0), defaults))
    assert len(lignes) == 1
    assert mesure(lignes[0]) == pytest.approx(7.5)


def test_les_percements_totalisent_la_longueur(defaults: Defaults) -> None:
    """Le contrôle qu'un dessinateur fait d'abord: la somme doit tomber juste."""
    baies = [Opening(segment=0, position=0.3, width=1.2, kind="window")]
    lignes = cotes(dimension_run((0.0, 0.0), (10.0, 0.0), defaults, openings=baies))
    percements = [round(mesure(c), 6) for c in lignes if round(mesure(c), 6) != 10.0]
    assert sum(percements) == pytest.approx(10.0)


def test_une_baie_est_cotee_a_sa_largeur(defaults: Defaults) -> None:
    baies = [Opening(segment=0, position=0.5, width=1.35, kind="window")]
    lignes = cotes(dimension_run((0.0, 0.0), (8.0, 0.0), defaults, openings=baies))
    assert any(mesure(c) == pytest.approx(1.35) for c in lignes)


def test_deux_baies_ajoutent_la_ligne_des_axes(defaults: Defaults) -> None:
    """Un plan se lit par couches: les tableaux, puis l'implantation."""
    baies = [
        Opening(segment=0, position=0.25, width=1.0, kind="window"),
        Opening(segment=0, position=0.75, width=1.0, kind="window"),
    ]
    lignes = cotes(dimension_run((0.0, 0.0), (12.0, 0.0), defaults, openings=baies))
    axes = [c for c in lignes if mesure(c) == pytest.approx(6.0)]
    assert axes, "l'écart d'axe en axe doit être coté"


def test_sans_hors_tout_la_ligne_generale_disparait(defaults: Defaults) -> None:
    lignes = cotes(dimension_run((0.0, 0.0), (7.0, 0.0), defaults, overall=False))
    assert not lignes


# ---------------------------------------------------------------------------
# Sens de lecture: le défaut que seul un rendu révèle
# ---------------------------------------------------------------------------


def tete_en_bas(cote) -> bool:  # type: ignore[no-untyped-def]
    angle = math.degrees(math.atan2(cote.p2[1] - cote.p1[1], cote.p2[0] - cote.p1[0])) % 360
    return 90.0 < angle <= 270.0


def test_aucune_cote_ne_se_lit_a_l_envers(defaults: Defaults) -> None:
    """Régression: la moitié d'un plan sortait avec ses chiffres retournés.

    Une cote alignée oriente son texte sur la droite qui joint ses points.
    Prise dans le mauvais sens, elle s'écrit tête en bas, ce qui arrive sur la
    façade nord et le pignon ouest d'un contour relevé dans le sens direct.
    """
    lignes = cotes(
        dimension_walls(CONTOUR, defaults, openings=BAIES, closed=True, thickness=0.25)
    )
    assert lignes
    assert not [c for c in lignes if tete_en_bas(c)]


@pytest.mark.parametrize(
    ("depart", "arrivee"),
    [
        ((0.0, 0.0), (6.0, 0.0)),
        ((6.0, 0.0), (0.0, 0.0)),
        ((0.0, 0.0), (0.0, 6.0)),
        ((0.0, 6.0), (0.0, 0.0)),
        ((0.0, 0.0), (4.0, 3.0)),
        ((4.0, 3.0), (0.0, 0.0)),
    ],
)
def test_le_sens_de_lecture_tient_dans_toutes_les_orientations(
    depart: tuple[float, float], arrivee: tuple[float, float], defaults: Defaults
) -> None:
    for cote in cotes(dimension_run(depart, arrivee, defaults)):
        assert not tete_en_bas(cote)


def test_inverser_les_points_ne_change_pas_la_mesure(defaults: Defaults) -> None:
    """Le remède ne doit toucher que la lecture, jamais le chiffre."""
    aller = cotes(dimension_run((0.0, 0.0), (7.3, 0.0), defaults))
    retour = cotes(dimension_run((7.3, 0.0), (0.0, 0.0), defaults))
    assert mesure(aller[0]) == pytest.approx(mesure(retour[0]))


# ---------------------------------------------------------------------------
# Échelonnement des lignes
# ---------------------------------------------------------------------------


def test_les_lignes_s_eloignent_les_unes_des_autres(defaults: Defaults) -> None:
    """Sans échelonnement, les chiffres d'une ligne touchent la suivante."""
    baies = [
        Opening(segment=0, position=0.25, width=1.0, kind="window"),
        Opening(segment=0, position=0.75, width=1.0, kind="window"),
    ]
    lignes = cotes(dimension_run((0.0, 0.0), (12.0, 0.0), defaults, openings=baies))
    deports = sorted({round(abs(c.location[1]), 3) for c in lignes})
    assert len(deports) >= 3, "percements, axes et hors tout doivent être distincts"
    ecarts = [b - a for a, b in pairwise(deports)]
    assert all(e > defaults.text_height for e in ecarts)


def test_les_cotes_se_placent_dehors(defaults: Defaults) -> None:
    """Coter à l'intérieur recouvrirait les pièces de chiffres."""
    lignes = cotes(dimension_walls(CONTOUR, defaults, closed=True, thickness=0.25))
    sud = [c for c in lignes if abs(c.p1[1]) < 0.01 and abs(c.p2[1]) < 0.01]
    assert sud and all(c.location[1] < 0 for c in sud)


def test_le_cote_s_inverse_quand_on_le_demande(defaults: Defaults) -> None:
    dehors = cotes(dimension_walls(CONTOUR, defaults, closed=True, outside=True))
    dedans = cotes(dimension_walls(CONTOUR, defaults, closed=True, outside=False))
    sud_dehors = next(c for c in dehors if abs(c.p1[1]) < 0.01)
    sud_dedans = next(c for c in dedans if abs(c.p1[1]) < 0.01)
    assert sud_dehors.location[1] < 0 < sud_dedans.location[1]


def test_le_sens_du_releve_ne_change_pas_le_cote(defaults: Defaults) -> None:
    """Un contour relevé en horaire doit coter dehors comme un antihoraire."""
    horaire = list(reversed(CONTOUR))
    lignes = cotes(dimension_walls(horaire, defaults, closed=True, thickness=0.25))
    sud = [c for c in lignes if abs(c.p1[1]) < 0.01 and abs(c.p2[1]) < 0.01]
    assert sud and all(c.location[1] < 0 for c in sud)


# ---------------------------------------------------------------------------
# Réseau entier
# ---------------------------------------------------------------------------


def test_les_quatre_facades_sont_cotees(defaults: Defaults) -> None:
    lignes = cotes(dimension_walls(CONTOUR, defaults, closed=True))
    assert len(lignes) == 4


def test_on_peut_ne_coter_qu_une_facade(defaults: Defaults) -> None:
    """Un plan coté de partout devient illisible."""
    lignes = cotes(dimension_walls(CONTOUR, defaults, closed=True, segments=[0]))
    assert len(lignes) == 1
    assert mesure(lignes[0]) == pytest.approx(9.0)


def test_le_calque_de_cotation_n_est_declare_qu_une_fois(defaults: Defaults) -> None:
    ops = dimension_walls(CONTOUR, defaults, openings=BAIES, closed=True)
    assert len([op for op in ops if op.kind == "ensure_layer"]) == 1
    assert ops[0].name == "DIMENSIONS"


def test_toutes_les_cotes_produites_sont_valides(defaults: Defaults) -> None:
    for op in dimension_walls(CONTOUR, defaults, openings=BAIES, closed=True):
        validate(op)


def test_la_cotation_suit_l_unite_du_document() -> None:
    en_mm = dimension_run(
        (0.0, 0.0), (9000.0, 0.0), Defaults(Unit.MILLIMETER)
    )
    ligne = cotes(en_mm)[0]
    assert mesure(ligne) == pytest.approx(9000.0)
    # Le déport doit rester lisible, donc à l'échelle du dessin.
    assert abs(ligne.location[1]) > 100.0


# ---------------------------------------------------------------------------
# Cas dégénérés
# ---------------------------------------------------------------------------


def test_un_mur_de_longueur_nulle_est_refuse(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        dimension_run((2.0, 2.0), (2.0, 2.0), defaults)


def test_un_reseau_d_un_seul_point_est_refuse(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        dimension_walls([(0.0, 0.0)], defaults)


def test_une_facade_hors_de_l_enfilade_est_refusee(defaults: Defaults) -> None:
    with pytest.raises(InvalidParameter):
        dimension_walls(CONTOUR, defaults, closed=True, segments=[9])
