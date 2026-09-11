"""Point d'entrée du serveur.

``run`` est la fonction déclarée comme script console dans ``pyproject.toml``.
Elle installe la journalisation **sur stderr** avant toute chose, car stdout
porte le protocole MCP, puis lance la boucle asynchrone.

Aucune connexion au moteur de dessin n'est tentée ici: le serveur doit pouvoir
démarrer sans AutoCAD ouvert, sinon le client MCP échoue avant même d'avoir vu
le catalogue d'outils.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import anyio

from . import __version__
from .config import Config
from .errors import CadError
from .logging_setup import setup_logging
from .units import parse_unit

__all__ = ["build_parser", "main", "run"]


def build_parser() -> argparse.ArgumentParser:
    """Options de ligne de commande, toutes facultatives.

    Elles doublent les variables d'environnement pour les clients MCP qui
    déclarent une commande plutôt qu'un environnement.
    """
    parser = argparse.ArgumentParser(
        prog="autocad-mcp",
        description=(
            "Serveur MCP de pilotage AutoCAD, avec backend DXF multiplateforme. "
            "Communique sur stdin/stdout; le journal part sur stderr."
        ),
    )
    parser.add_argument("--version", action="version", version=f"autocad-mcp {__version__}")
    parser.add_argument(
        "--backend",
        choices=("autocad", "ezdxf", "recording"),
        help=(
            "Moteur de dessin. Par défaut autocad sous Windows, ezdxf ailleurs. "
            "Équivaut à AUTOCAD_MCP_BACKEND."
        ),
    )
    parser.add_argument(
        "--unit",
        help="Unité de longueur du document: mm, cm, m, in, ft. Équivaut à AUTOCAD_MCP_UNIT.",
    )
    parser.add_argument(
        "--dxf",
        metavar="CHEMIN",
        help=(
            "Fichier DXF ouvert par le backend ezdxf, et destination des "
            "enregistrements. Équivaut à AUTOCAD_MCP_DXF."
        ),
    )
    parser.add_argument(
        "--query-limit",
        type=int,
        metavar="N",
        help="Nombre d'entités décrites par défaut à l'inspection. Équivaut à AUTOCAD_MCP_QUERY_LIMIT.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="DEBUG, INFO, WARNING ou ERROR. Équivaut à AUTOCAD_MCP_LOG. Le journal va sur stderr.",
    )
    parser.add_argument(
        "--log-file",
        metavar="CHEMIN",
        help="Duplique le journal dans un fichier. Équivaut à AUTOCAD_MCP_LOGFILE.",
    )
    return parser


def _config_from(args: argparse.Namespace) -> Config:
    """Configuration d'environnement, surchargée par les options données."""
    config = Config.from_env()
    if args.backend:
        config.backend = args.backend
    if args.unit:
        config.unit = parse_unit(args.unit)
    if args.dxf:
        config.dxf_path = args.dxf
    if args.query_limit is not None:
        if args.query_limit < 1:
            from .errors import InvalidParameter

            raise InvalidParameter(
                "--query-limit doit être strictement positif", got=args.query_limit
            )
        config.query_limit = args.query_limit
    return config


def main(argv: Sequence[str] | None = None) -> int:
    """Analyse les arguments, lance le serveur, rend un code de sortie."""
    args = build_parser().parse_args(argv)
    logger = setup_logging(args.log_level, args.log_file)

    try:
        config = _config_from(args)
    except CadError as exc:
        logger.error("configuration invalide: %s (%s)", exc.message, exc.code)
        return 2

    # Import tardif: il tire `mcp`, inutile pour --version ou --help.
    from .server import serve

    try:
        anyio.run(serve, config)
    except KeyboardInterrupt:  # pragma: no cover - dépend du terminal
        logger.info("interruption clavier")
        return 130
    except CadError as exc:
        logger.error("arrêt sur erreur: %s (%s)", exc.message, exc.code)
        return 1
    return 0


def run() -> None:
    """Entrée du script console ``autocad-mcp``."""
    sys.exit(main())


if __name__ == "__main__":
    run()
