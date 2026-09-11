"""Suite de contrat, exécutée contre chaque backend.

Une seule suite, plusieurs moteurs. Elle tourne sur le backend d'enregistrement
et sur le backend DXF partout, et sur AutoCAD uniquement sous Windows.

C'est le mécanisme qui empêche les backends de diverger. Sans elle, un écart de
comportement ne se découvre que devant AutoCAD, une opération à la fois.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from autocad_mcp.backends.base import BatchResult, CadBackend, EntityFilter
from autocad_mcp.backends.ezdxf_be import EzdxfBackend
from autocad_mcp.backends.recording import RecordingBackend
from autocad_mcp.errors import ConfirmationRequired
from autocad_mcp.model.ops import (
    AddBlockRef,
    AddCircle,
    AddLine,
    AddPolyline,
    AddText,
    DefineBlock,
    EnsureLayer,
    OperationBatch,
    Style,
)
from autocad_mcp.ops.architecture import door_in_wall, room
from autocad_mcp.units import Defaults, Unit


def _make_recording() -> CadBackend:
    return RecordingBackend(unit=Unit.METER)


def _make_ezdxf() -> CadBackend:
    return EzdxfBackend(unit=Unit.METER)


def _make_autocad() -> CadBackend:
    from autocad_mcp.backends.acad_com import AcadComBackend

    return AcadComBackend()


BACKENDS = [
    pytest.param(_make_recording, id="recording"),
    pytest.param(_make_ezdxf, id="ezdxf"),
    pytest.param(
        _make_autocad,
        id="autocad",
        # Le marqueur `windows` compte autant que le saut: il rend la
        # couverture AutoCAD visible par `pytest -m windows`, donc vérifiable.
        # Un saut silencieux laisserait croire à une couverture qui aurait pu
        # disparaître sans que personne ne s'en aperçoive.
        marks=[
            pytest.mark.windows,
            pytest.mark.skipif(
                sys.platform != "win32", reason="exige Windows et AutoCAD ouvert"
            ),
        ],
    ),
]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> Iterator[CadBackend]:
    be = request.param()
    be.connect()
    yield be
    be.close()


STYLE = Style(layer="WALLS")


def _basic_batch() -> OperationBatch:
    return OperationBatch(
        (
            EnsureLayer("WALLS", 7, "murs"),
            AddLine((0.0, 0.0, 0.0), (5.0, 0.0, 0.0), style=STYLE),
            AddPolyline(((0.0, 0.0), (5.0, 0.0), (5.0, 3.0), (0.0, 3.0)), closed=True, style=STYLE),
            AddCircle((2.5, 1.5, 0.0), 0.5, style=STYLE),
            AddText((2.5, 1.5, 0.0), "SALON", 0.25, style=STYLE),
        ),
        label="contrat",
    )


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------


def test_un_lot_rend_un_handle_par_entite(backend: CadBackend) -> None:
    result = backend.execute(_basic_batch())
    assert result.ok
    # Quatre entités: la ligne, la polyligne, le cercle, le texte.
    assert len(result.created) == 4
    assert len(set(e.handle for e in result.created)) == 4


def test_un_calque_n_est_pas_compte_comme_une_entite(backend: CadBackend) -> None:
    """Écart réel constaté entre backends, verrouillé ici.

    Le backend DXF comptait le calque parmi les objets créés, ce qui gonflait
    le nombre d'entités dessinées et aurait fait supprimer des calques à
    l'annulation.
    """
    result = backend.execute(_basic_batch())
    assert len(result.created) == backend.count()
    assert "WALLS" in result.layers


def test_un_calque_non_declare_est_cree_a_la_volee(backend: CadBackend) -> None:
    """Arbitrage: un calque manquant est créé, pas rejeté.

    Le modèle oubliera parfois de déclarer son calque. Refuser l'opération le
    laisserait avec un dessin vide et un message peu actionnable. La création
    reste visible puisque le nom apparaît dans ``layers``, donc rien ne se
    produit en silence.

    Les trois backends doivent se comporter pareil, y compris AutoCAD, dont
    l'API ActiveX refuse par défaut d'affecter un calque inexistant.
    """
    result = backend.execute(
        OperationBatch(
            (AddLine((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), style=Style(layer="IMPROVISE")),),
            label="calque-implicite",
        )
    )
    assert result.ok, result.failures
    assert "IMPROVISE" in result.layers
    assert backend.count(EntityFilter(layer="IMPROVISE")) == 1


def test_un_nom_de_calque_vide_reste_une_erreur(backend: CadBackend) -> None:
    """La souplesse s'arrête où le fichier deviendrait illisible ailleurs."""
    result = backend.execute(
        OperationBatch((EnsureLayer("", 7),), label="calque-sans-nom")
    )
    assert not result.ok


def test_une_definition_de_bloc_n_est_pas_une_entite(backend: CadBackend) -> None:
    """Écart réel entre backends, verrouillé ici.

    Une définition vit dans la table des blocs, pas dans l'espace objet. La
    compter parmi les entités dessinées fausse le décompte et ferait porter
    l'annulation sur elle. Seule l'occurrence insérée est une entité.
    """
    lot = OperationBatch(
        (
            DefineBlock(
                name="REPERE_TEST",
                operations=(
                    AddCircle((0.0, 0.0, 0.0), 0.25, style=Style(layer="0")),
                ),
            ),
        ),
        label="definition",
    )
    result = backend.execute(lot)
    assert result.ok, result.failures
    assert not result.created
    assert "REPERE_TEST" in result.blocks
    assert backend.count() == 0


def test_une_occurrence_de_bloc_est_une_entite(backend: CadBackend) -> None:
    lot = OperationBatch(
        (
            DefineBlock(
                name="REPERE_POSE",
                operations=(AddCircle((0.0, 0.0, 0.0), 0.25, style=Style(layer="0")),),
            ),
            AddBlockRef(name="REPERE_POSE", insert=(2.0, 2.0, 0.0), style=STYLE),
        ),
        label="pose",
    )
    result = backend.execute(lot)
    assert result.ok, result.failures
    assert len(result.created) == 1
    assert backend.count() == 1


def test_redefinir_un_bloc_ne_l_ecrase_pas(backend: CadBackend) -> None:
    """Sémantique de garantie, comme pour un calque.

    Un plan qui insère dix fois la même porte redéfinit dix fois le bloc. Une
    bibliothèque ne doit jamais écraser le travail de quelqu'un d'autre.
    """
    def definition(rayon: float) -> OperationBatch:
        return OperationBatch(
            (
                DefineBlock(
                    name="STABLE",
                    operations=(
                        AddCircle((0.0, 0.0, 0.0), rayon, style=Style(layer="0")),
                    ),
                ),
            ),
            label="redefinition",
        )

    assert backend.execute(definition(1.0)).ok
    second = backend.execute(definition(9.0))
    assert second.ok, second.failures
    assert not second.created


def test_les_handles_sont_ceux_du_document(backend: CadBackend) -> None:
    """Régression: l'ancien code fabriquait des handles comme `line_created`."""
    result = backend.execute(_basic_batch())
    handles = {e.handle for e in result.created}
    relus = {info.handle for info in backend.query()}
    assert handles <= relus
    assert not any(h.startswith(("line_", "text_", "circle_")) for h in handles)


def test_un_echec_n_est_jamais_rapporte_comme_un_succes(backend: CadBackend) -> None:
    """Le bug le plus grave du projet historique.

    Une opération invalide doit produire un échec visible, pas un succès muet
    accompagné d'un faux handle.
    """
    mauvais = OperationBatch(
        (AddCircle((0.0, 0.0, 0.0), -1.0, style=STYLE),), label="invalide"
    )
    result = backend.execute(mauvais)
    assert not result.ok
    assert result.failures
    assert not result.created


def test_un_lot_partiel_rapporte_les_deux_cotes(backend: CadBackend) -> None:
    melange = OperationBatch(
        (
            AddLine((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), style=STYLE),
            AddCircle((0.0, 0.0, 0.0), -5.0, style=STYLE),
            AddLine((0.0, 1.0, 0.0), (1.0, 1.0, 0.0), style=STYLE),
        ),
        label="melange",
    )
    result = backend.execute(melange)
    assert not result.ok
    assert len(result.created) == 2
    assert len(result.failures) == 1
    assert result.failures[0]["index"] == 1


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------


def test_le_compte_suit_les_creations(backend: CadBackend) -> None:
    assert backend.count() == 0
    backend.execute(_basic_batch())
    assert backend.count() == 4


def test_le_filtre_par_calque(backend: CadBackend) -> None:
    backend.execute(_basic_batch())
    backend.execute_one(
        AddLine((0.0, 5.0, 0.0), (1.0, 5.0, 0.0), style=Style(layer="0")), label="autre"
    )
    assert backend.count(EntityFilter(layer="WALLS")) == 4
    assert backend.count(EntityFilter(layer="0")) == 1


def test_le_filtre_par_handle(backend: CadBackend) -> None:
    result = backend.execute(_basic_batch())
    cible = result.created[0].handle
    trouve = backend.query(EntityFilter(handles=(cible,)))
    assert len(trouve) == 1
    assert trouve[0].handle == cible


def test_la_limite_borne_la_reponse(backend: CadBackend) -> None:
    """Une inspection ne doit jamais saturer le contexte du modèle."""
    backend.execute(_basic_batch())
    assert len(backend.query(limit=2)) == 2


def test_les_infos_d_entite_sont_completes(backend: CadBackend) -> None:
    backend.execute(_basic_batch())
    for info in backend.query():
        payload = info.to_dict()
        assert payload["handle"]
        assert payload["type"]
        assert payload["layer"]
        assert isinstance(payload["color"], int)


def test_document_info_annonce_l_unite(backend: CadBackend) -> None:
    info = backend.document_info()
    assert "unit" in info or "units" in info


# --------------------------------------------------------------------------
# Suppression et annulation
# --------------------------------------------------------------------------


def test_la_suppression_par_calque(backend: CadBackend) -> None:
    backend.execute(_basic_batch())
    supprimes = backend.delete(EntityFilter(layer="WALLS"))
    assert len(supprimes) == 4
    assert backend.count() == 0


def test_la_suppression_par_handle(backend: CadBackend) -> None:
    result = backend.execute(_basic_batch())
    cible = result.created[0].handle
    assert backend.delete(EntityFilter(handles=(cible,))) == [cible]
    assert backend.count() == 3


def test_une_suppression_sans_filtre_exige_une_confirmation(backend: CadBackend) -> None:
    """Écart réel entre backends, verrouillé ici.

    Un filtre vide désigne le dessin entier. Le backend d'enregistrement
    effaçait tout sans broncher alors que les deux autres refusaient, donc il
    ne prouvait rien sur le comportement réel.
    """
    backend.execute(_basic_batch())
    with pytest.raises(ConfirmationRequired):
        backend.delete(EntityFilter())
    assert backend.count() == 4


def test_le_changement_de_couleur(backend: CadBackend) -> None:
    result = backend.execute(_basic_batch())
    cible = result.created[0].handle
    backend.set_color(cible, 1)
    relu = backend.query(EntityFilter(handles=(cible,)))[0]
    assert relu.color == 1


def test_l_annulation_retire_le_dernier_lot(backend: CadBackend) -> None:
    backend.execute(_basic_batch())
    avant = backend.count()
    backend.execute_one(AddLine((9.0, 9.0, 0.0), (10.0, 9.0, 0.0), style=STYLE), label="ajout")
    assert backend.count() == avant + 1
    backend.undo()
    assert backend.count() == avant


# --------------------------------------------------------------------------
# Intégration avec la logique métier
# --------------------------------------------------------------------------


def test_un_plan_complet_passe_sur_chaque_backend(backend: CadBackend) -> None:
    """La logique métier doit produire le même dessin quel que soit le moteur."""
    d = Defaults(Unit.METER)
    plan = []
    plan += room((0.0, 0.0), (5.0, 4.0), d, name="Salon")
    plan += room((5.0, 0.0), (9.0, 4.0), d, name="Cuisine")
    plan += door_in_wall((5.0, 0.0), (5.0, 4.0), d, position=0.5)

    result: BatchResult = backend.execute(OperationBatch(tuple(plan), label="plan"))

    assert result.ok, result.failures
    # Deux pièces à deux anneaux chacune, deux étiquettes, un vantail, un arc.
    # Les murs comptaient huit entités avant le raccord d'angle: chaque pièce
    # était faite de quatre rectangles indépendants qui se chevauchaient.
    assert len(result.created) == 8
    assert backend.count(EntityFilter(layer="WALLS")) == 4
    assert backend.count(EntityFilter(layer="ANNOTATION")) == 2
    assert backend.count(EntityFilter(layer="DOORS")) == 2
