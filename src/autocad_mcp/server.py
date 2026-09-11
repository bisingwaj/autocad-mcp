"""Câblage MCP.

Ce module ne contient **aucune logique métier**. Il traduit le catalogue de
``tools.schemas`` en outils du protocole, passe les appels à
``tools.handlers``, et convertit le résultat en contenus MCP. Toute décision
sur ce qui est dessiné, lu ou supprimé appartient aux couches inférieures.

Trois contraintes du transport sont tenues ici.

* **stdout porte le protocole.** Rien n'y est écrit: le journal part sur stderr
  par ``logging_setup.setup_logging``. Un simple ``print`` de débogage
  corromprait la communication avec le client.
* **Aucune connexion au démarrage.** Le serveur se lance et annonce son
  catalogue sans toucher au moteur de dessin. La connexion se fait au premier
  appel d'outil. Sans cela, démarrer sans AutoCAD ouvert échouerait d'emblée et
  le modèle ne verrait jamais les outils.
* **Les erreurs restent des erreurs.** Un échec ressort avec ``isError`` à vrai
  et le dictionnaire d'erreur complet, code compris. Le serveur historique
  répondait ``success: true`` quoi qu'il arrive, ce qui privait le modèle de
  toute chance de se corriger.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import mcp.types as types
from mcp.server.lowlevel.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from . import __version__
from .config import Config
from .logging_setup import get_logger
from .tools.handlers import ToolHandlers, ToolResult
from .tools.schemas import ToolSpec

__all__ = ["INSTRUCTIONS", "SERVER_NAME", "build_server", "serve"]

SERVER_NAME = "autocad-mcp"

#: Mode d'emploi remis au modèle à l'initialisation. Il porte ce qu'aucun
#: schéma d'outil pris isolément ne peut dire: l'ordre de travail.
INSTRUCTIONS = """\
Serveur de dessin CAO. Ordre de travail recommandé:

1. get_drawing_info d'abord, pour connaître l'unité du document et ses calques.
   Toutes les longueurs que vous donnerez ensuite sont dans cette unité.
2. Dessinez par LOTS: build_structure pour le bâti, place_blocks pour les
   équipements et le mobilier, draw pour la géométrie nue. Un lot vaut mieux
   que vingt appels: il forme une transaction unique et une seule marque
   d'annulation.
   Pour les murs, utilisez l'élément wall_network et donnez-lui ses openings:
   c'est le seul qui raccorde les angles et qui perce réellement la maçonnerie.
3. render_view après chaque lot, pour VOIR ce qui a été tracé. Un compte rendu
   de succès ne dit rien de la position ni de l'échelle.
4. check_plan pour vous relire: contours non fermés, murs qui se croisent,
   doublons, résidus, trous. Chaque défaut porte sa localisation et son remède.
5. measure pour chiffrer sans énumérer: résumé, totaux, nomenclature, voisinage
   d'un point, contenu d'une fenêtre.
6. undo_last_batch si le lot est faux, plutôt que de corriger à la main.

Les angles sont en degrés. Les couleurs sont des noms ou des index ACI.
Les handles ne se devinent pas: ils viennent de query_entities ou de la réponse
d'un lot d'écriture. Une réponse marquée en erreur porte un code stable et
n'a rien créé au-delà de ce que son champ handles énumère. Une mesure rendue à
null est une mesure que le moteur ne publie pas, jamais un zéro.
"""

_LOG = get_logger("server")


def _to_tool(spec: ToolSpec) -> types.Tool:
    """Traduit une déclaration du catalogue en outil du protocole."""
    return types.Tool(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        input_schema=spec.input_schema,
        annotations=types.ToolAnnotations(
            title=spec.title,
            read_only_hint=spec.read_only,
            destructive_hint=spec.destructive,
            idempotent_hint=spec.idempotent,
            open_world_hint=False,
        ),
    )


def _to_content(result: ToolResult) -> types.CallToolResult:
    """Met un résultat d'outil en contenus MCP.

    Le texte vient en premier: il donne les comptes, les handles et, s'il y a
    lieu, le code d'erreur. L'image suit quand l'outil en produit une, transmise
    en ``ImageContent`` et non par un chemin de fichier que le modèle ne
    pourrait pas ouvrir.
    """
    content: list[types.ContentBlock] = [
        types.TextContent(
            type="text",
            text=json.dumps(result.payload, ensure_ascii=False, indent=2, default=str),
        )
    ]
    if result.image_png is not None:
        content.append(
            types.ImageContent(
                type="image",
                data=base64.b64encode(result.image_png).decode("ascii"),
                mime_type="image/png",
            )
        )
    return types.CallToolResult(content=content, is_error=result.is_error)


def build_server(
    config: Config | None = None, handlers: ToolHandlers | None = None
) -> Server[Any]:
    """Assemble le serveur MCP autour d'un jeu de handlers.

    Args:
        config: configuration d'exécution. Lue dans l'environnement si absente.
        handlers: exécuteur des outils. Construit à partir de ``config`` si
            absent. L'injection sert aux tests, qui branchent un backend en
            mémoire sans passer par l'environnement.
    """
    settings = config or Config.from_env()
    router = handlers or ToolHandlers(settings)
    catalog = tuple(_to_tool(spec) for spec in router.catalog)

    async def on_list_tools(
        ctx: ServerRequestContext[Any, Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(catalog))

    async def on_call_tool(
        ctx: ServerRequestContext[Any, Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        _LOG.info("appel %s", params.name)
        result = await router.call(params.name, params.arguments)
        return _to_content(result)

    return Server(
        SERVER_NAME,
        version=__version__,
        title="AutoCAD MCP",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve(config: Config | None = None) -> None:
    """Sert le protocole sur stdio jusqu'à la fermeture du flux d'entrée."""
    settings = config or Config.from_env()
    handlers = ToolHandlers(settings)
    server = build_server(settings, handlers)

    _LOG.info(
        "serveur %s %s prêt, backend %s en attente du premier appel",
        SERVER_NAME,
        __version__,
        settings.backend,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await handlers.aclose()
        _LOG.info("serveur arrêté")
