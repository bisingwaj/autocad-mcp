"""Serveur MCP de pilotage AutoCAD, avec backend DXF multiplateforme.

Ce module d'entrée reste volontairement léger et **n'importe aucun backend**.
Importer `backends.acad_com` ici rendrait le paquet inchargeable hors Windows,
ce qui interdirait tout développement sur la machine actuelle.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .errors import (
    BackendUnavailable,
    CadError,
    ConfirmationRequired,
    EntityNotFound,
    InvalidGeometry,
    InvalidParameter,
    NotConnected,
    OperationFailed,
    UnsupportedOperation,
)
from .units import Defaults, Unit, convert, parse_unit

__all__ = [
    "BackendUnavailable",
    "CadError",
    "ConfirmationRequired",
    "Defaults",
    "EntityNotFound",
    "InvalidGeometry",
    "InvalidParameter",
    "NotConnected",
    "OperationFailed",
    "Unit",
    "UnsupportedOperation",
    "__version__",
    "convert",
    "parse_unit",
]


def get_backend(name: str | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
    """Instancie un backend par son nom, avec import paresseux.

    L'import tardif est ce qui permet à ce paquet de se charger sur macOS alors
    que le backend AutoCAD dépend de `pywin32`, absent de cette plateforme.
    """
    from .config import Config

    resolved = name or Config.from_env().backend

    if resolved == "autocad":
        from .backends.acad_com import AcadComBackend

        return AcadComBackend(**kwargs)  # type: ignore[arg-type]
    if resolved == "ezdxf":
        from .backends.ezdxf_be import EzdxfBackend

        return EzdxfBackend(**kwargs)  # type: ignore[arg-type]
    if resolved == "recording":
        from .backends.recording import RecordingBackend

        return RecordingBackend(**kwargs)  # type: ignore[arg-type]

    raise InvalidParameter(
        f"Backend inconnu: {resolved!r}", supported=["autocad", "ezdxf", "recording"]
    )
