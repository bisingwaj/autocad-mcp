# AutoCAD MCP

**Auteur :** Jérémie BISINGWA

Serveur [Model Context Protocol](https://modelcontextprotocol.io) qui donne à un
assistant IA le contrôle conversationnel d'**Autodesk AutoCAD**, doublé d'un
moteur **DXF multiplateforme** qui fait tourner le projet sans AutoCAD.

---

## Ce qui distingue ce serveur

**L'assistant voit son propre dessin.** L'outil de rendu renvoie une image dans
la réponse. Le modèle constate qu'une porte est mal orientée et se corrige, au
lieu de dessiner en aveugle.

**Tout part en un seul lot.** Un plan complet s'envoie en un appel, encadré par
une seule marque d'annulation et suivi d'un seul rafraîchissement. Annuler une
maison est un geste, pas cinquante.

**Le serveur tourne sans AutoCAD.** Le moteur DXF fonctionne sur macOS et Linux,
produit de vrais fichiers, et permet de développer et tester le projet sans
licence Autodesk.

**Un échec est un échec.** Toute erreur remonte avec un code et un remède. Aucun
succès n'est annoncé sans preuve d'exécution.

---

## Installation

```bash
git clone https://github.com/bisingwaj/autocad-mcp
cd autocad-mcp

# Avec uv, recommandé
uv sync --extra render
uv sync --extra windows   # sous Windows, pour piloter AutoCAD

# Ou avec pip
pip install -e ".[render]"
pip install -e ".[render,windows]"   # sous Windows
```

Python 3.10 ou plus récent.

---

## Configuration du client MCP

> **Sous Windows avec Cursor ou VS Code**, suivez plutôt le guide pas à pas :
> [docs/demarrage-windows.md](docs/demarrage-windows.md). Il part d'une machine
> vierge, donne les deux formats de configuration, qui diffèrent, et commence
> par le mode DXF qui fonctionne sans AutoCAD.

Dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "uv",
      "args": ["--directory", "/chemin/vers/autocad-mcp", "run", "autocad-mcp"]
    }
  }
}
```

Le moteur se choisit tout seul : AutoCAD sous Windows, fichier DXF ailleurs.
Variables d'environnement disponibles :

| Variable | Effet |
|---|---|
| `AUTOCAD_MCP_BACKEND` | `autocad`, `ezdxf` ou `recording` |
| `AUTOCAD_MCP_UNIT` | `mm`, `cm`, `m`, `in`, `ft` |
| `AUTOCAD_MCP_DXF` | fichier DXF ouvert par le moteur DXF |
| `AUTOCAD_MCP_LOG` | niveau de journal, sur stderr |

Sous Windows, AutoCAD doit être ouvert avec un dessin actif.

---

## Les outils exposés

| Outil | Rôle |
|---|---|
| `get_drawing_info` | document, unité, nombre d'entités, calques, capacités |
| `query_entities` | inspection filtrée, réponse toujours bornée |
| `measure` | résumé, longueurs, surfaces, nomenclature, proximité, fenêtre |
| `check_plan` | contours ouverts, croisements, doublons, résidus, jonctions |
| `render_view` | **renvoie une image du dessin** |
| `draw` | lot de primitives: ligne, polyligne, rectangle, cercle, arc, texte, hachure |
| `build_structure` | lot d'éléments: réseau de murs et ses baies, pièce, étiquette |
| `place_blocks` | insertion de symboles: sanitaires, électricité, mobilier |
| `delete_entities` | suppression filtrée, confirmation exigée si le filtre est vide |
| `set_entity_color` | changement de couleur |
| `undo_last_batch` | annulation du dernier lot |
| `run_cad_command` | passerelle AutoCAD, strictement sur liste blanche |

Les murs se raccordent aux angles et les baies percent réellement la
maçonnerie, ce qui rend les surfaces calculables et les contours hachurables.

## Exemples de demandes

* « Dessine un studio de 7 sur 5 mètres avec une salle de bain à droite,
  une porte entre les deux et une fenêtre au nord. »
* « Montre-moi le plan. » puis « La porte s'ouvre du mauvais côté, inverse-la. »
* « Combien d'entités sur le calque WALLS ? »
* « Supprime tout ce qui est sur le calque ANNOTATION. »

---

## Développement

```bash
uv run pytest -q          # suite complète, passe sans AutoCAD
uv run ruff check src/ tests/
uv run mypy src/
uv run python -m autocad_mcp.devtools.contract_diff   # parité des moteurs
```

La suite tourne sur macOS, Linux et Windows. Les tests marqués `windows`
exigent AutoCAD et sont sautés ailleurs.

Voir [docs/architecture.md](docs/architecture.md) pour la conception, et
[docs/windows-checklist.md](docs/windows-checklist.md) pour la validation du
moteur AutoCAD sous Windows.

---

## Licence et avertissement

Ce logiciel utilise l'automatisation locale permise par Autodesk mais n'est pas
officiellement soutenu par Autodesk, Inc. Son usage suppose une installation
légitime d'AutoCAD sous licence valide.
