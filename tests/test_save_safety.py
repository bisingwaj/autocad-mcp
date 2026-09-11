"""L'enregistrement ne doit jamais produire un dessin qu'AutoCAD refuse d'éditer.

Trois causes d'ouverture en lecture seule, constatées à l'usage, et qui ne
disent jamais leur nom quand on les subit: un fichier à moitié écrit, un verrou
laissé par AutoCAD, et des droits perdus au remplacement.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from autocad_mcp.backends.ezdxf_be import LOCK_SUFFIXES, EzdxfBackend
from autocad_mcp.errors import OperationFailed
from autocad_mcp.model.ops import AddLine, OperationBatch
from autocad_mcp.units import Unit


@pytest.fixture
def moteur() -> EzdxfBackend:
    backend = EzdxfBackend(unit=Unit.METER)
    backend.connect()
    backend.execute(
        OperationBatch((AddLine((0.0, 0.0, 0.0), (5.0, 0.0, 0.0)),), label="essai")
    )
    return backend


def test_l_ecriture_ne_laisse_aucun_temporaire(moteur: EzdxfBackend, tmp_path: Path) -> None:
    """Le fichier part dans un voisin puis remplace la cible en une fois.

    Écrire directement exposerait un dessin tronqué à qui le relirait pendant
    l'opération.
    """
    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    assert cible.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["plan.dxf"]


def test_le_fichier_ecrit_est_relisible(moteur: EzdxfBackend, tmp_path: Path) -> None:
    import ezdxf

    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    audit = ezdxf.readfile(str(cible)).audit()
    assert not audit.errors


@pytest.mark.parametrize("suffixe", LOCK_SUFFIXES)
def test_un_verrou_autocad_arrete_l_ecriture(
    moteur: EzdxfBackend, tmp_path: Path, suffixe: str
) -> None:
    """AutoCAD dépose un témoin à côté d'un dessin ouvert.

    Écrire par-dessus donnerait un dessin que l'utilisateur ne pourrait pas
    modifier, sans qu'aucun message n'explique pourquoi.
    """
    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    (tmp_path / f"plan.dxf{suffixe}").write_text("occupé")

    with pytest.raises(OperationFailed) as info:
        moteur.save(str(cible))
    assert "verrouillé" in info.value.message
    assert "AutoCAD" in info.value.details["remedy"]


def test_le_verrou_a_l_ancienne_forme_est_vu_aussi(
    moteur: EzdxfBackend, tmp_path: Path
) -> None:
    """Certaines versions posent le témoin en remplaçant l'extension."""
    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    (tmp_path / "plan.dwl").write_text("occupé")
    with pytest.raises(OperationFailed):
        moteur.save(str(cible))


def test_un_fichier_en_lecture_seule_est_signale(
    moteur: EzdxfBackend, tmp_path: Path
) -> None:
    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    os.chmod(cible, 0o444)
    try:
        with pytest.raises(OperationFailed) as info:
            moteur.save(str(cible))
        assert "lecture seule" in info.value.message
    finally:
        os.chmod(cible, 0o644)


@pytest.mark.parametrize("droits", [0o664, 0o600, 0o666])
def test_les_droits_du_fichier_sont_preserves(
    moteur: EzdxfBackend, tmp_path: Path, droits: int
) -> None:
    """Le remplacement fait hériter les droits du temporaire.

    Sans report explicite, un fichier partagé en écriture avec un groupe
    retombe en lecture seule pour lui dès le premier enregistrement. C'était
    mesuré: 664 devenait 644.
    """
    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    os.chmod(cible, droits)
    moteur.save(str(cible))
    assert stat.S_IMODE(cible.stat().st_mode) == droits


def test_le_verrou_leve_l_ecriture_repasse(moteur: EzdxfBackend, tmp_path: Path) -> None:
    """Un refus doit être réversible: fermer le dessin suffit à repartir."""
    cible = tmp_path / "plan.dxf"
    moteur.save(str(cible))
    verrou = tmp_path / "plan.dxf.dwl"
    verrou.write_text("occupé")
    with pytest.raises(OperationFailed):
        moteur.save(str(cible))
    verrou.unlink()
    assert moteur.save(str(cible)) == str(cible)


def test_un_dossier_absent_est_cree(moteur: EzdxfBackend, tmp_path: Path) -> None:
    cible = tmp_path / "projets" / "2026" / "plan.dxf"
    moteur.save(str(cible))
    assert cible.exists()
