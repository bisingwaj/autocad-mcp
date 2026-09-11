"""Mesures et requêtes spatiales, sur des entités déjà lues.

Ces tests ne touchent ni AutoCAD, ni un fichier, ni même un backend: ils
fabriquent des ``EntityInfo`` à la main et affirment sur ce que la logique en
déduit. C'est précisément ce que la séparation logique/backend rend possible.

Le jeu d'essai est un plan minuscule mais réaliste: deux murs et une cloison
sur trois calques, en mètres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from autocad_mcp.backends.base import EntityInfo
from autocad_mcp.errors import InvalidParameter
from autocad_mcp.model.ops import BYLAYER
from autocad_mcp.ops.query import (
    EntityLike,
    Measure,
    bill_of_quantities,
    group_by_layer,
    group_by_type,
    in_window,
    nearest,
    summarize,
    total_area,
    total_length,
)

Box = tuple[float, float, float, float]


def entity(
    handle: str,
    *,
    kind: str = "LINE",
    layer: str = "WALLS",
    box: Box | None = (0.0, 0.0, 1.0, 1.0),
    **extra: Any,
) -> EntityInfo:
    """Une entité telle qu'un backend la rendrait après lecture."""
    return EntityInfo(
        handle=handle,
        kind=kind,
        layer=layer,
        color=BYLAYER,
        bbox=box,
        extra=dict(extra),
    )


@pytest.fixture
def plan() -> list[EntityInfo]:
    """Deux murs, une cloison, un texte: trois calques, trois types."""
    return [
        entity("A1", kind="LWPOLYLINE", layer="WALLS", box=(0.0, 0.0, 5.0, 0.2), length=10.4),
        entity("A2", kind="LWPOLYLINE", layer="WALLS", box=(0.0, 0.0, 0.2, 4.0), length=8.4),
        entity("B1", kind="LINE", layer="PARTITIONS", box=(1.0, 1.0, 3.0, 1.0), length=2.0),
        entity("C1", kind="TEXT", layer="ANNOTATION", box=(2.0, 2.0, 2.6, 2.25), text="Salon"),
    ]


# ---------------------------------------------------------------------------
# Résumé
# ---------------------------------------------------------------------------


def test_le_resume_compte_par_calque_et_par_type(plan: list[EntityInfo]) -> None:
    """C'est ce qu'on renvoie au lieu de déverser le dessin entier."""
    summary = summarize(plan)

    assert summary.count == 4
    assert summary.by_layer == {"WALLS": 2, "ANNOTATION": 1, "PARTITIONS": 1}
    assert summary.by_type == {"LWPOLYLINE": 2, "LINE": 1, "TEXT": 1}


def test_le_resume_classe_du_plus_fourni_au_moins_fourni(plan: list[EntityInfo]) -> None:
    """L'ordre est déterministe et met en tête ce qui pèse dans le dessin."""
    summary = summarize(plan)

    assert list(summary.by_layer) == ["WALLS", "ANNOTATION", "PARTITIONS"]


def test_le_resume_rend_les_limites_globales(plan: list[EntityInfo]) -> None:
    summary = summarize(plan)

    assert summary.extents == (0.0, 0.0, 5.0, 4.0)
    assert summary.without_bbox == 0


def test_le_resume_compte_a_part_les_entites_sans_limites() -> None:
    """Une entité sans boîte n'est ni placée au hasard, ni passée sous silence."""
    summary = summarize([entity("A1", box=None), entity("A2", box=(0.0, 0.0, 2.0, 2.0))])

    assert summary.count == 2
    assert summary.without_bbox == 1
    assert summary.extents == (0.0, 0.0, 2.0, 2.0)


def test_le_resume_d_un_dessin_vide_ne_ment_pas() -> None:
    summary = summarize([])

    assert summary.count == 0
    assert summary.extents is None
    assert summary.by_layer == {}
    assert summary.length.total == 0.0
    assert summary.length.missing == 0


def test_le_resume_se_serialise_pour_le_modele(plan: list[EntityInfo]) -> None:
    payload = summarize(plan).to_dict()

    assert payload["count"] == 4
    assert payload["extents"] == [0.0, 0.0, 5.0, 4.0]
    assert payload["length"]["counted"] == 3
    assert payload["length"]["complete"] is False


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------


def test_la_longueur_totale_dit_ce_qu_elle_ignore(plan: list[EntityInfo]) -> None:
    """Un total silencieusement partiel est pire qu'absent: il a l'air juste."""
    measure = total_length(plan)

    assert measure.total == pytest.approx(20.8)
    assert measure.counted == 3
    assert measure.missing == 1
    assert measure.complete is False


def test_une_mesure_complete_le_declare() -> None:
    measure = total_area([entity("A1", area=12.0), entity("A2", area=8.0)])

    assert measure == Measure(total=20.0, counted=2, missing=0)
    assert measure.complete is True


def test_une_mesure_refuse_ce_qui_n_est_pas_un_nombre() -> None:
    """Un booléen n'est pas une longueur, un infini empoisonnerait le total."""
    measure = total_length(
        [
            entity("A1", length=3.0),
            entity("A2", length=True),
            entity("A3", length="4.0"),
            entity("A4", length=float("inf")),
            entity("A5", length=float("nan")),
        ]
    )

    assert measure.total == pytest.approx(3.0)
    assert measure.counted == 1
    assert measure.missing == 4


# ---------------------------------------------------------------------------
# Fenêtres
# ---------------------------------------------------------------------------


def test_la_fenetre_stricte_ne_retient_que_ce_qui_tient_dedans() -> None:
    """Sélection par fenêtre d'AutoCAD: l'entité doit être entièrement dedans."""
    dedans = entity("A1", box=(1.0, 1.0, 2.0, 2.0))
    a_cheval = entity("A2", box=(2.5, 1.0, 6.0, 2.0))
    dehors = entity("A3", box=(9.0, 9.0, 10.0, 10.0))

    retenues = in_window([dedans, a_cheval, dehors], (0.0, 0.0, 5.0, 5.0), mode="inside")

    assert [e.handle for e in retenues] == ["A1"]


def test_la_fenetre_capturante_retient_aussi_ce_qui_la_traverse() -> None:
    dedans = entity("A1", box=(1.0, 1.0, 2.0, 2.0))
    a_cheval = entity("A2", box=(2.5, 1.0, 6.0, 2.0))
    dehors = entity("A3", box=(9.0, 9.0, 10.0, 10.0))

    retenues = in_window([dedans, a_cheval, dehors], (0.0, 0.0, 5.0, 5.0), mode="crossing")

    assert [e.handle for e in retenues] == ["A1", "A2"]


def test_les_deux_coins_de_la_fenetre_arrivent_dans_n_importe_quel_ordre() -> None:
    """Un modèle qui désigne une zone ne trie pas ses coins."""
    cible = entity("A1", box=(1.0, 1.0, 2.0, 2.0))

    assert in_window([cible], (5.0, 5.0, 0.0, 0.0), mode="inside") == [cible]


def test_la_fenetre_ecarte_les_entites_sans_limites() -> None:
    assert in_window([entity("A1", box=None)], (0.0, 0.0, 5.0, 5.0), mode="crossing") == []


def test_un_mode_de_fenetre_inconnu_est_refuse() -> None:
    with pytest.raises(InvalidParameter):
        in_window([], (0.0, 0.0, 1.0, 1.0), mode="touching")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Proximité
# ---------------------------------------------------------------------------


def test_les_plus_proches_arrivent_dans_l_ordre_avec_leur_distance() -> None:
    loin = entity("A1", box=(10.0, 0.0, 11.0, 1.0))
    proche = entity("A2", box=(1.0, 0.0, 2.0, 1.0))

    voisins = nearest([loin, proche], (0.0, 0.0), limit=2)

    assert [e.handle for e, _ in voisins] == ["A2", "A1"]
    assert voisins[0][1] == pytest.approx(1.0)
    assert voisins[1][1] == pytest.approx(10.0)


def test_un_point_dans_l_entite_est_a_distance_nulle() -> None:
    dedans = entity("A1", box=(0.0, 0.0, 4.0, 4.0))

    assert nearest([dedans], (2.0, 2.0))[0][1] == 0.0


def test_la_proximite_borne_sa_reponse() -> None:
    """Renvoyer tout le dessin trié saturerait le contexte du modèle."""
    entities = [entity(f"A{i}", box=(float(i), 0.0, float(i) + 0.5, 1.0)) for i in range(20)]

    assert len(nearest(entities, (0.0, 0.0), limit=3)) == 3


def test_deux_appels_identiques_rendent_le_meme_ordre() -> None:
    """À distance égale, le handle départage: pas de tri instable."""
    box = (1.0, 0.0, 2.0, 1.0)
    voisins = nearest([entity("B"), entity("A", box=box), entity("C", box=box)], (0.0, 0.0))

    assert [e.handle for e, _ in voisins][1:] == ["A", "C"]


def test_un_nombre_de_voisins_nul_est_refuse() -> None:
    with pytest.raises(InvalidParameter):
        nearest([], (0.0, 0.0), limit=0)


# ---------------------------------------------------------------------------
# Regroupements
# ---------------------------------------------------------------------------


def test_le_regroupement_par_calque_range_tout(plan: list[EntityInfo]) -> None:
    groupes = group_by_layer(plan)

    assert list(groupes) == ["ANNOTATION", "PARTITIONS", "WALLS"]
    assert [e.handle for e in groupes["WALLS"]] == ["A1", "A2"]


def test_le_regroupement_par_calque_compare_le_nom_entier() -> None:
    """Régression: la correspondance par sous-chaîne attrapait `light` dans
    `skylight` et `table` dans `portable`."""
    groupes = group_by_layer([entity("A1", layer="LIGHT"), entity("A2", layer="SKYLIGHT")])

    assert list(groupes) == ["LIGHT", "SKYLIGHT"]
    assert len(groupes["LIGHT"]) == 1


def test_le_regroupement_par_type_range_tout(plan: list[EntityInfo]) -> None:
    groupes = group_by_type(plan)

    assert list(groupes) == ["LINE", "LWPOLYLINE", "TEXT"]
    assert [e.handle for e in groupes["LWPOLYLINE"]] == ["A1", "A2"]


# ---------------------------------------------------------------------------
# Nomenclature
# ---------------------------------------------------------------------------


def test_la_nomenclature_donne_les_quantites_par_calque_et_par_type(
    plan: list[EntityInfo],
) -> None:
    rows = bill_of_quantities(plan)

    assert [(r.layer, r.kind, r.count) for r in rows] == [
        ("ANNOTATION", "TEXT", 1),
        ("PARTITIONS", "LINE", 1),
        ("WALLS", "LWPOLYLINE", 2),
    ]
    murs = rows[2]
    assert murs.length.total == pytest.approx(18.8)
    assert murs.length.complete is True


def test_une_ligne_de_nomenclature_avoue_ses_lacunes(plan: list[EntityInfo]) -> None:
    """Le texte n'a pas de longueur: la ligne le dit au lieu de rendre zéro."""
    annotation = bill_of_quantities(plan)[0]

    assert annotation.length.total == 0.0
    assert annotation.length.missing == 1
    assert annotation.to_dict()["length"]["complete"] is False


# ---------------------------------------------------------------------------
# Frontière
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FauxEntite:
    """Ce module n'exige pas ``EntityInfo``, seulement sa forme."""

    handle: str
    kind: str
    layer: str
    bbox: tuple[float, float, float, float] | None
    extra: dict[str, Any] = field(default_factory=dict)


def test_la_logique_n_exige_aucun_type_de_backend() -> None:
    """La frontière est structurelle: tout objet de la bonne forme passe.

    Si ce test compile et passe, c'est que rien ici ne dépend d'un backend.
    """
    faux: list[FauxEntite] = [
        FauxEntite("X1", "LINE", "WALLS", (0.0, 0.0, 3.0, 0.0), {"length": 3.0}),
        FauxEntite("X2", "LINE", "WALLS", None),
    ]

    summary = summarize(faux)
    assert summary.count == 2
    assert summary.without_bbox == 1
    assert summary.length.total == pytest.approx(3.0)
    assert [e.handle for e in in_window(faux, (0.0, 0.0, 5.0, 5.0), mode="inside")] == ["X1"]


def test_une_entite_info_reelle_satisfait_la_forme_attendue() -> None:
    """Le contrat vise bien ``EntityInfo``, pas une abstraction parallèle."""
    info = EntityInfo(handle="A1", kind="LINE", layer="WALLS", color=BYLAYER)
    comme_entite: EntityLike = info

    assert comme_entite.handle == "A1"
    assert comme_entite.bbox is None
    assert summarize([info]).count == 1
