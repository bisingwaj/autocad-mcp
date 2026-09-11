"""Configuration d'exécution du serveur.

Tout est réglable par variable d'environnement, ce qui permet de changer de
backend sans toucher au code ni à la configuration du client MCP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .units import Defaults, Unit, parse_unit

BackendName = Literal["autocad", "ezdxf", "recording"]


def _default_backend() -> BackendName:
    """AutoCAD sous Windows, fichier DXF ailleurs.

    Ce choix est ce qui rend le projet utilisable et testable hors Windows.
    """
    explicit = os.environ.get("AUTOCAD_MCP_BACKEND")
    if explicit:
        name = explicit.strip().lower()
        if name not in ("autocad", "ezdxf", "recording"):
            from .errors import InvalidParameter

            raise InvalidParameter(
                f"Backend inconnu: {explicit!r}",
                supported=["autocad", "ezdxf", "recording"],
            )
        return name  # type: ignore[return-value]
    import sys

    return "autocad" if sys.platform == "win32" else "ezdxf"


@dataclass(slots=True)
class Config:
    backend: BackendName
    unit: Unit
    #: Fichier DXF ouvert ou créé par le backend ezdxf.
    dxf_path: str | None = None
    #: Nombre maximal d'entités renvoyées par un appel d'inspection.
    query_limit: int = 200
    #: Largeur et hauteur du rendu en pixels.
    render_size: tuple[int, int] = (1600, 1200)
    #: Commandes AutoCAD autorisées par la liste blanche.
    allowed_commands: frozenset[str] = frozenset(
        {"OFFSET", "TRIM", "EXTEND", "FILLET", "CHAMFER", "ARRAY", "MIRROR", "BOUNDARY", "HATCH"}
    )

    @property
    def defaults(self) -> Defaults:
        return Defaults(self.unit)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            backend=_default_backend(),
            unit=parse_unit(os.environ.get("AUTOCAD_MCP_UNIT", "m")),
            dxf_path=os.environ.get("AUTOCAD_MCP_DXF"),
            query_limit=int(os.environ.get("AUTOCAD_MCP_QUERY_LIMIT", "200")),
        )
