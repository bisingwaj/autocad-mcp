"""Journalisation du serveur.

Contrainte du transport MCP en mode standard: **stdout porte le protocole**.
Tout message écrit sur stdout corrompt la communication avec le client. Le
journal part donc sur stderr, et éventuellement dans un fichier.

Le code historique n'avait aucun journal et avalait ses erreurs dans des blocs
``except`` nus. Un échec était donc indiscernable d'un succès.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOGGER_NAME = "autocad_mcp"
_CONFIGURED = False


def setup_logging(level: str | int | None = None, logfile: str | Path | None = None) -> logging.Logger:
    """Installe la journalisation. Idempotent.

    Le niveau se règle par l'argument, sinon par la variable d'environnement
    ``AUTOCAD_MCP_LOG``, sinon à INFO.
    """
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)

    if _CONFIGURED:
        return logger

    if level is None:
        level = os.environ.get("AUTOCAD_MCP_LOG", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%H:%M:%S"
    )

    # Jamais sur stdout: le protocole MCP l'occupe.
    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    target = logfile or os.environ.get("AUTOCAD_MCP_LOGFILE")
    if target:
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    """Journal du module appelant."""
    name = f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME
    return logging.getLogger(name)
