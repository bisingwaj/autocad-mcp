# AutoCAD MCP — consignes de travail

Serveur MCP qui pilote AutoCAD par automatisation COM, doublé d'un backend DXF
multiplateforme qui rend le projet développable et testable sans AutoCAD.

## La règle qui structure tout

**La logique métier décide, le backend exécute.**

Une fonction qui crée un mur retourne une liste d'opérations déclaratives. Elle
ne connaît ni AutoCAD, ni `ezdxf`, ni COM. Un backend les exécute ensuite.

Cette séparation n'est pas un raffinement, c'est ce qui rend le projet testable :
la machine de développement est un **Mac**, AutoCAD n'y tourne pas.

| Couche | Connaît | Ne connaît pas |
|---|---|---|
| `geometry`, `units` | rien du projet | tout le reste |
| `model/ops`, `model/layers` | `geometry`, `units` | backends, MCP |
| `ops/*` | `model`, `geometry`, `units` | backends, MCP |
| `backends/*` | `model/ops` | `ops/*`, MCP |
| `tools/*`, `server` | tout | — |

## Interdits absolus

1. **Importer `win32com` ou `pythoncom` hors de `backends/acad_com.py`.**
   Le paquet doit rester chargeable sur macOS. Import paresseux uniquement.
2. **Un `except` nu qui avale l'erreur.** Utiliser les exceptions de `errors.py`.
3. **Renvoyer un succès sans preuve d'exécution.** C'est le bug historique :
   l'ancien code renvoyait `success: true` même en échec, avec de faux handles
   comme `line_created`. Le modèle croyait avoir dessiné et ne pouvait pas
   se corriger.
4. **Un `time.sleep` de confort.** L'ancien code dormait une seconde par
   opération : un rectangle coûtait quatre secondes. On groupe en lots.
5. **Un regen par entité.** Un seul en fin de lot.
6. **Une longueur sans unité.** Tout passe par `units.Defaults`.
7. **Deux outils MCP du même nom.** Le catalogue historique déclarait
   `delete_entities_by_color` deux fois.

## Commandes

```bash
uv sync --extra render --all-groups   # installer
uv run pytest -q                      # suite complète, doit passer sur macOS
uv run ruff check src/ tests/         # style
uv run mypy src/                      # types
```

Les tests marqués `windows` sont sautés hors Windows. Leur disparition
signifierait la perte de la couverture AutoCAD, pas un progrès.

## Agents disponibles

| Agent | Pour quoi |
|---|---|
| `cad-geometry` | calculs géométriques purs, `geometry.py`, `units.py` |
| `acad-com` | backend COM Windows, pièges ActiveX, threading STA |
| `dxf-backend` | backend `ezdxf`, rendu PNG, preuve exécutable sur Mac |
| `mcp-tooling` | schémas d'outils, protocole, ergonomie pour le modèle |
| `cad-tester` | les quatre niveaux de test, suite de contrat |
| `cad-architect` | arbitrage des frontières, revue de placement |

## Compétences disponibles

| Compétence | Quand |
|---|---|
| `add-cad-tool` | ajouter une capacité de dessin, les six couches à traiter |
| `verify-cad` | vérification complète avant de conclure un travail |
| `dxf-preview` | produire un rendu et le regarder réellement |
| `windows-parity` | après toute modification du backend COM |

## Bugs historiques, chacun couvert par un test

- Succès mensonger sur échec, avec faux handles.
- `delete_entities_by_color` déclaré deux fois dans le catalogue d'outils.
- Couleur `black` associée à l'index 0, qui vaut ByBlock et non noir.
- Battant de porte toujours orienté vers l'est, quel que soit le mur porteur.
- Correspondance de calque par sous-chaîne : `skylight` attrapait `light`,
  `portable` attrapait `table`.
- Aucune notion d'unité : épaisseurs invisibles dans un dessin en millimètres.
- Rectangle fait de quatre segments indépendants, donc ni hachurable,
  ni mesurable, ni sélectionnable d'un clic.
