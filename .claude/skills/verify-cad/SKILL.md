---
name: verify-cad
description: Vérification complète du serveur AutoCAD MCP avant de considérer un travail terminé. Enchaîne le contrôle des frontières d'architecture, l'importabilité sur macOS, les types, la suite de tests et la validité des schémas d'outils. À lancer après toute modification du projet, et systématiquement avant un commit.
---

# Vérifier le projet

Lance les étapes dans l'ordre. La première qui échoue arrête tout: les suivantes
donneraient des résultats trompeurs. Montre la sortie réelle de chaque commande.

## 1. Importabilité sur macOS

La régression la plus coûteuse du projet serait de rendre le paquet inchargeable
hors de Windows. C'est donc le premier contrôle.

```bash
uv run python -c "import autocad_mcp; print('import OK')"
```

Puis vérifie qu'aucun module hors du backend COM ne dépend de `pywin32`:

```bash
grep -rn "win32com\|pythoncom" src/ --include=*.py | grep -v "backends/acad_com.py"
```

Cette commande doit ne rien retourner.

## 2. Frontières d'architecture

La logique métier ne connaît aucun backend:

```bash
grep -rn "from autocad_mcp.backends\|import backends" src/autocad_mcp/ops/ src/autocad_mcp/geometry.py src/autocad_mcp/model/
```

Cette commande doit ne rien retourner. En cas de doute sur un placement,
consulte l'agent `cad-architect`.

## 3. Types

```bash
uv run mypy src/
```

## 4. Suite de tests

```bash
uv run pytest -q
```

Les tests marqués `windows` sont normalement sautés sur cette machine.
Vérifie qu'ils sont bien sautés et non silencieusement absents:

```bash
uv run pytest -q -m windows --collect-only | tail -3
```

## 5. Schémas d'outils

Les noms doivent être uniques et les schémas valides. Le projet porte un test
dédié, car le catalogue historique déclarait un outil deux fois:

```bash
uv run pytest tests/test_tool_schemas.py -q
```

## 6. Preuve visuelle

Pour tout changement touchant la géométrie ou le dessin, produis un rendu et
regarde-le réellement. Utilise la compétence `dxf-preview`.

## Rapport

Conclus par un état net: ce qui passe, ce qui échoue avec la trace,
et ce qui reste à confirmer sous Windows. N'écris jamais qu'une étape passe
sans avoir montré sa sortie.
