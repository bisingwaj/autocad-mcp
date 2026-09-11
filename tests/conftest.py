"""Fixtures communes.

Le chemin d'import est réglé par le paquet installé en mode éditable via uv,
donc aucune manipulation de sys.path n'est nécessaire ici.
"""

from __future__ import annotations

import sys

import pytest

from autocad_mcp.backends.recording import RecordingBackend
from autocad_mcp.units import Defaults, Unit


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Saute les tests AutoCAD hors Windows.

    Ils ne disparaissent pas: ils restent collectés et visibles, ce qui permet
    de vérifier que la couverture Windows existe toujours.
    """
    if item.get_closest_marker("windows") and sys.platform != "win32":
        pytest.skip("exige Windows et une instance AutoCAD ouverte")


@pytest.fixture
def recorder() -> RecordingBackend:
    backend = RecordingBackend(unit=Unit.METER)
    backend.connect()
    return backend


@pytest.fixture
def defaults() -> Defaults:
    return Defaults(Unit.METER)
