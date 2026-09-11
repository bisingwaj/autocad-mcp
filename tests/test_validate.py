"""Détection des défauts de plan, sur des cas réellement rencontrés.

Le plan d'essai est en **millimètres**, l'échelle où les défauts se voient: la
tolérance du document y vaut un dixième de millimètre, et c'est exactement
l'ordre de grandeur des trous qui font échouer un hachurage sans rien montrer
à l'écran.

Aucun de ces tests ne touche AutoCAD, ni un fichier, ni un backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from autocad_mcp.backends.base import EntityInfo
from autocad_mcp.errors import InvalidParameter
from autocad_mcp.model.ops import BYLAYER
from autocad_mcp.ops.validate import (
    Contour,
    Segment,
    find_crossing_walls,
    find_duplicates,
    find_gaps,
    find_open_contours,
    find_tiny_entities,
    validate_plan,
)
from autocad_mcp.units import Defaults, Unit

#: Tolérance d'un document en millimètres: un dixième de millimètre.
TOL = Defaults(Unit.MILLIMETER).tolerance


def entity(
    handle: str,
    box: tuple[float, float, float, float] | None,
    *,
    kind: str = "LINE",
    layer: str = "WALLS",
    **extra: Any,
) -> EntityInfo:
    return EntityInfo(
        handle=handle,
        kind=kind,
        layer=layer,
        color=BYLAYER,
        bbox=box,
        extra=dict(extra),
    )


def test_la_tolerance_du_millimetre_vaut_un_dixieme() -> None:
    """Les cas de ce fichier reposent sur cette valeur: elle est affirmée ici."""
    tolerance = Defaults(Unit.MILLIMETER).tolerance

    assert tolerance == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Contours non fermés
# ---------------------------------------------------------------------------


#: Contour de pièce de cinq mètres sur cinq dont le tracé s'arrête à vingt
#: centimètres du point de départ. À l'écran, le trou se voit à peine.
MUR_NON_FERME = Contour(
    points=((0.0, 0.0), (5000.0, 0.0), (5000.0, 5000.0), (0.0, 5000.0), (0.0, 200.0)),
    ref="2F1",
    layer="WALLS",
)


def test_un_contour_de_mur_non_ferme_est_une_erreur() -> None:
    """Le défaut qui empêche de hachurer et de mesurer une pièce."""
    problems = find_open_contours([MUR_NON_FERME], tol=TOL)

    assert len(problems) == 1
    defaut = problems[0]
    assert defaut.kind == "open_contour"
    assert defaut.severity == "error"
    assert defaut.refs == ("2F1",)
    assert defaut.details["gap"] == pytest.approx(200.0)


def test_le_defaut_dit_ou_regarder() -> None:
    """Sans localisation, un rapport oblige le modèle à chercher au hasard."""
    defaut = find_open_contours([MUR_NON_FERME], tol=TOL)[0]

    assert defaut.location == pytest.approx((0.0, 100.0))


def test_un_contour_declare_ferme_n_est_pas_un_defaut() -> None:
    ferme = Contour(points=((0.0, 0.0), (5000.0, 0.0), (5000.0, 5000.0)), closed=True)

    assert find_open_contours([ferme], tol=TOL) == []


def test_un_contour_qui_retombe_sur_son_depart_n_est_pas_un_defaut() -> None:
    """Le sommet dupliqué relève de `close_ring`, au tracé, pas du plan."""
    retombe = Contour(
        points=((0.0, 0.0), (5000.0, 0.0), (5000.0, 5000.0), (0.0, 0.05)),
    )

    assert find_open_contours([retombe], tol=TOL) == []


def test_une_polyligne_franchement_ouverte_n_est_qu_un_avertissement() -> None:
    """Un L de cinq mètres est peut-être voulu ouvert: on signale sans accuser."""
    ouverte = Contour(points=((0.0, 0.0), (5000.0, 0.0), (5000.0, 5000.0)))

    problems = find_open_contours([ouverte], tol=TOL)

    assert [p.severity for p in problems] == ["warning"]


def test_un_segment_n_est_pas_un_contour_rate() -> None:
    assert find_open_contours([Contour(points=((0.0, 0.0), (1000.0, 0.0)))], tol=TOL) == []


def test_une_tolerance_nulle_est_refusee() -> None:
    """Aucune tolérance implicite: la comparer à zéro n'a pas de sens."""
    with pytest.raises(InvalidParameter):
        find_open_contours([MUR_NON_FERME], tol=0.0)


# ---------------------------------------------------------------------------
# Murs qui se croisent
# ---------------------------------------------------------------------------


#: Deux murs tracés en croix: ils se traversent au lieu de se rejoindre.
CROIX = (
    Segment(start=(0.0, 0.0), end=(4000.0, 4000.0), ref="2A1", layer="WALLS"),
    Segment(start=(0.0, 4000.0), end=(4000.0, 0.0), ref="2A2", layer="WALLS"),
)


def test_deux_murs_en_croix_sont_signales_avec_leur_intersection() -> None:
    problems = find_crossing_walls(CROIX, tol=TOL)

    assert len(problems) == 1
    defaut = problems[0]
    assert defaut.kind == "crossing_walls"
    assert defaut.severity == "error"
    assert defaut.location == pytest.approx((2000.0, 2000.0))
    assert set(defaut.refs) == {"2A1", "2A2"}


def test_une_jonction_en_L_n_est_pas_un_defaut() -> None:
    """Deux murs qui partagent un coin forment un angle, pas un croisement."""
    angle = (
        Segment(start=(0.0, 0.0), end=(4000.0, 0.0)),
        Segment(start=(4000.0, 0.0), end=(4000.0, 3000.0)),
    )

    assert find_crossing_walls(angle, tol=TOL) == []


def test_une_jonction_en_T_n_est_pas_un_defaut() -> None:
    """Un refend qui s'arrête contre l'enveloppe est un assemblage correct."""
    te = (
        Segment(start=(0.0, 0.0), end=(4000.0, 0.0)),
        Segment(start=(2000.0, 0.0), end=(2000.0, 3000.0)),
    )

    assert find_crossing_walls(te, tol=TOL) == []


def test_deux_murs_paralleles_ne_sont_pas_des_murs_qui_se_croisent() -> None:
    """Leur intersection n'est pas un point: c'est `find_duplicates` qui juge."""
    paralleles = (
        Segment(start=(0.0, 0.0), end=(4000.0, 0.0)),
        Segment(start=(0.0, 0.0), end=(4000.0, 0.0)),
    )

    assert find_crossing_walls(paralleles, tol=TOL) == []


def test_un_segment_degenere_ne_fait_pas_echouer_le_controle() -> None:
    """Un résidu de longueur nulle n'a pas de direction: il est écarté ici et
    rapporté par `find_tiny_entities`."""
    avec_residu = (*CROIX, Segment(start=(1000.0, 1000.0), end=(1000.0, 1000.0)))

    assert len(find_crossing_walls(avec_residu, tol=TOL)) == 1


# ---------------------------------------------------------------------------
# Doublons
# ---------------------------------------------------------------------------


def test_deux_lignes_identiques_superposees_sont_signalees() -> None:
    """Défaut classique d'un plan redessiné: invisible, et il double les quantités."""
    problems = find_duplicates(
        [entity("A1", (0.0, 0.0, 3000.0, 0.0)), entity("A2", (0.0, 0.0, 3000.0, 0.0))],
        tol=TOL,
    )

    assert len(problems) == 1
    defaut = problems[0]
    assert defaut.kind == "duplicate"
    assert defaut.severity == "warning"
    assert set(defaut.refs) == {"A1", "A2"}
    assert defaut.details["count"] == 2


def test_trois_superpositions_ne_font_qu_un_defaut() -> None:
    problems = find_duplicates(
        [entity(h, (0.0, 0.0, 3000.0, 0.0)) for h in ("A1", "A2", "A3")],
        tol=TOL,
    )

    assert len(problems) == 1
    assert problems[0].refs == ("A1", "A2", "A3")


def test_deux_lignes_sur_des_calques_differents_ne_sont_pas_un_doublon() -> None:
    problems = find_duplicates(
        [
            entity("A1", (0.0, 0.0, 3000.0, 0.0), layer="WALLS"),
            entity("A2", (0.0, 0.0, 3000.0, 0.0), layer="PARTITIONS"),
        ],
        tol=TOL,
    )

    assert problems == []


def test_deux_textes_de_contenus_differents_ne_sont_pas_un_doublon() -> None:
    """La boîte englobante ne suffit pas: la description est comparée aussi."""
    problems = find_duplicates(
        [
            entity("C1", (0.0, 0.0, 600.0, 250.0), kind="TEXT", text="Salon"),
            entity("C2", (0.0, 0.0, 600.0, 250.0), kind="TEXT", text="Cuisine"),
        ],
        tol=TOL,
    )

    assert problems == []


def test_une_entite_sans_limites_n_est_pas_declaree_doublon() -> None:
    """Faute de pouvoir en juger, on n'accuse pas."""
    problems = find_duplicates([entity("A1", None), entity("A2", None)], tol=TOL)

    assert problems == []


# ---------------------------------------------------------------------------
# Résidus
# ---------------------------------------------------------------------------


def test_un_segment_d_un_milliemme_de_millimetre_est_un_residu() -> None:
    """Trop petit pour être vu, assez gros pour fausser une sélection."""
    problems = find_tiny_entities([entity("A1", (0.0, 0.0, 0.001, 0.0))], TOL)

    assert len(problems) == 1
    assert problems[0].kind == "tiny_entity"
    assert problems[0].details["size"] == pytest.approx(0.001)
    assert problems[0].refs == ("A1",)


def test_un_mur_de_trois_metres_n_est_pas_un_residu() -> None:
    assert find_tiny_entities([entity("A1", (0.0, 0.0, 3000.0, 200.0))], TOL) == []


def test_le_seuil_de_taille_doit_etre_positif() -> None:
    """Il n'existe pas de « petit » universel: le seuil vient de l'appelant."""
    with pytest.raises(InvalidParameter):
        find_tiny_entities([entity("A1", (0.0, 0.0, 1.0, 1.0))], 0.0)


# ---------------------------------------------------------------------------
# Trous entre extrémités
# ---------------------------------------------------------------------------


#: Deux murs dont les extrémités se ratent d'un dixième de millimètre.
#: À l'écran le contour paraît continu; la hachure, elle, échoue.
PRESQUE_JOINTS = (
    Segment(start=(-3000.0, 0.0), end=(0.0, 0.0), ref="2B1"),
    Segment(start=(0.1, 0.0), end=(0.1, 3000.0), ref="2B2"),
)


def test_deux_extremites_distantes_d_un_dixieme_de_millimetre_sont_un_trou() -> None:
    problems = find_gaps(PRESQUE_JOINTS, tol=TOL)

    assert len(problems) == 1
    defaut = problems[0]
    assert defaut.kind == "gap"
    assert defaut.severity == "error"
    assert defaut.details["gap"] == pytest.approx(0.1)
    assert set(defaut.refs) == {"2B1", "2B2"}
    assert defaut.location == pytest.approx((0.05, 0.0))


def test_deux_extremites_reellement_jointes_ne_sont_pas_un_trou() -> None:
    jointes = (
        Segment(start=(-3000.0, 0.0), end=(0.0, 0.0)),
        Segment(start=(0.0, 0.0), end=(0.0, 3000.0)),
    )

    assert find_gaps(jointes, tol=TOL) == []


def test_deux_extremites_franchement_eloignees_ne_sont_pas_un_trou() -> None:
    """Un mur qui s'arrête à cinq millimètres d'un autre est un choix de tracé."""
    ecartees = (
        Segment(start=(-3000.0, 0.0), end=(0.0, 0.0)),
        Segment(start=(5.0, 0.0), end=(5.0, 3000.0)),
    )

    assert find_gaps(ecartees, tol=TOL) == []


def test_un_rayon_de_recherche_plus_petit_que_la_jonction_est_refuse() -> None:
    """Sinon aucun écart ne pourrait jamais être rapporté, et le contrôle
    passerait toujours au vert sans rien vérifier."""
    with pytest.raises(InvalidParameter):
        find_gaps(PRESQUE_JOINTS, tol=TOL, joined_tol=TOL)


# ---------------------------------------------------------------------------
# Rapport complet
# ---------------------------------------------------------------------------


#: Deux murs superposés, loin des autres défauts.
DOUBLONS = [
    entity("A1", (0.0, 10000.0, 3000.0, 10000.0)),
    entity("A2", (0.0, 10000.0, 3000.0, 10000.0)),
]
#: Un résidu d'un millième de millimètre.
RESIDU = [entity("A3", (100.0, 20000.0, 100.001, 20000.0))]
#: Les deux murs presque joints, déplacés hors du chemin des autres.
TROU = (
    Segment(start=(-3000.0, 30000.0), end=(0.0, 30000.0), ref="2B1"),
    Segment(start=(0.1, 30000.0), end=(0.1, 33000.0), ref="2B2"),
)


def rapport_complet() -> Any:
    return validate_plan(
        entities=[*DOUBLONS, *RESIDU],
        contours=[MUR_NON_FERME],
        segments=[*CROIX, *TROU],
        unit=Unit.MILLIMETER,
    )


def test_le_rapport_rassemble_les_cinq_familles_de_defauts() -> None:
    report = rapport_complet()

    assert [p.kind for p in report.problems] == [
        "open_contour",
        "crossing_walls",
        "gap",
        "duplicate",
        "tiny_entity",
    ]


def test_le_rapport_met_les_erreurs_avant_les_avertissements() -> None:
    """Le modèle lit de haut en bas: il doit tomber d'abord sur ce qui bloque."""
    severites = [p.severity for p in rapport_complet().problems]

    assert severites == sorted(severites, key=lambda s: 0 if s == "error" else 1)
    assert severites.count("error") == 3


def test_un_plan_defectueux_n_est_pas_declare_correct() -> None:
    """Régression de fond: l'ancien serveur renvoyait un succès sur échec."""
    report = rapport_complet()

    assert report.ok is False
    assert len(report.errors) == 3
    assert len(report.warnings) == 2


def test_chaque_defaut_porte_un_remede_en_une_phrase() -> None:
    """C'est ce qui permet au modèle de se corriger seul plutôt que de redessiner."""
    for defaut in rapport_complet().problems:
        assert defaut.remedy.endswith(".")
        assert len(defaut.remedy.split()) >= 4
        assert defaut.refs
        assert defaut.location is not None


def test_le_rapport_dit_ce_qu_il_a_examine_et_avec_quels_seuils() -> None:
    """Un rapport vert sur zéro objet examiné n'est pas un plan correct."""
    report = rapport_complet()

    assert report.checked == {"entities": 3, "contours": 1, "segments": 4}
    assert report.tolerance == pytest.approx(TOL)
    assert report.minimum_size == pytest.approx(TOL)


def test_la_tolerance_se_deduit_de_l_unite_du_document() -> None:
    """Un plan en mètres et un plan en millimètres n'ont pas les mêmes seuils."""
    en_metres = validate_plan(segments=[], unit=Unit.METER)

    assert en_metres.tolerance == pytest.approx(Defaults(Unit.METER).tolerance)
    assert en_metres.tolerance != pytest.approx(TOL)


def test_une_tolerance_explicite_prime_sur_l_unite() -> None:
    report = validate_plan(contours=[MUR_NON_FERME], unit=Unit.MILLIMETER, tol=500.0)

    assert report.tolerance == pytest.approx(500.0)
    # Un écart de 200 mm passe sous une tolérance de 500 mm.
    assert report.problems == ()


def test_valider_sans_tolerance_ni_unite_est_refuse() -> None:
    """Choisir une tolérance en silence reviendrait à valider un plan au
    millimètre avec les seuils d'un plan de masse."""
    with pytest.raises(InvalidParameter):
        validate_plan(contours=[MUR_NON_FERME])


def test_un_plan_propre_ne_produit_aucun_defaut() -> None:
    """Le contrôle doit pouvoir dire oui, sinon il ne dit rien."""
    carre = Contour(
        points=((0.0, 0.0), (5000.0, 0.0), (5000.0, 5000.0), (0.0, 5000.0)),
        closed=True,
        ref="2F9",
    )
    murs = [
        Segment(start=(0.0, 0.0), end=(5000.0, 0.0), ref="2A9"),
        Segment(start=(5000.0, 0.0), end=(5000.0, 5000.0), ref="2A8"),
    ]
    report = validate_plan(
        entities=[entity("A9", (0.0, 0.0, 5000.0, 5000.0))],
        contours=[carre],
        segments=murs,
        unit=Unit.MILLIMETER,
    )

    assert report.ok is True
    assert report.problems == ()


def test_le_rapport_se_serialise_pour_le_modele() -> None:
    payload = rapport_complet().to_dict()

    assert payload["ok"] is False
    assert payload["error_count"] == 3
    assert payload["warning_count"] == 2
    assert payload["checked"]["segments"] == 4
    premier = payload["problems"][0]
    assert premier["kind"] == "open_contour"
    assert premier["location"] == [0.0, 100.0]
    assert premier["remedy"]
