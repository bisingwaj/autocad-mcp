---
name: dxf-preview
description: Génère un dessin avec le backend ezdxf et le rend en image pour l'inspecter visuellement. À utiliser pour vérifier de ses yeux qu'une géométrie est correcte, pour comparer un avant et un après, ou pour produire une preuve visuelle après un changement de dessin. Remplace l'impossibilité d'ouvrir AutoCAD sur cette machine.
---

# Prévisualiser un dessin

Le projet cible AutoCAD sous Windows, absent de cette machine. Le backend
`ezdxf` et son rendu sont donc le seul moyen de voir ce que le code produit.
Un changement de géométrie non regardé est un changement non vérifié.

## Produire un rendu

Il n'existe pas de commande dédiée: le point d'entrée `autocad-mcp` est le
serveur MCP, qui parle sur l'entrée et la sortie standard. Le rendu se fait
donc par un court script.

```python
from autocad_mcp.backends.ezdxf_be import EzdxfBackend
from autocad_mcp.model.ops import OperationBatch
from autocad_mcp.ops.architecture import wall_network, Opening, label
from autocad_mcp.render import render_png
from autocad_mcp.units import Defaults, Unit

d = Defaults(Unit.METER)
plan = wall_network(
    [(0, 0), (6, 0), (6, 4), (0, 4)], d, thickness=0.2, closed=True,
    openings=[Opening(segment=0, position=0.5, width=0.9, kind="door")],
)

be = EzdxfBackend(unit=Unit.METER)
be.connect()
resultat = be.execute(OperationBatch(tuple(plan), label="essai"))
print(resultat.to_dict())
open("essai.png", "wb").write(render_png(be.document, 1400, 1000))
be.save("essai.dxf")
```

Lance-le par `uv run python <script>.py`. Place les fichiers dans le répertoire
de travail temporaire de la session, jamais dans le dépôt, sauf s'il s'agit
d'une image de référence destinée aux tests.

Pour vérifier qu'un dessin sortirait intact d'un autre logiciel, relis le DXF
et lance son audit: zéro erreur et zéro correctif est la seule preuve
disponible ici, faute d'AutoCAD.

## Regarder réellement

Ouvre le PNG produit avec l'outil de lecture d'image, et examine-le. Les défauts
que ce projet a produits par le passé et qu'un rendu révèle immédiatement:

- Un battant de porte orienté vers l'est quel que soit le mur porteur.
- Des murs qui ne se rejoignent pas aux angles.
- Une épaisseur invisible parce que le dessin est en millimètres et la valeur
  exprimée en mètres.
- Des segments séparés au lieu d'une polyligne fermée, visible au fait que
  le contour ne se hachure pas.
- Un texte de taille absurde par rapport au plan.

## Comparer un avant et un après

Rends la version de référence et la nouvelle avec des paramètres de rendu
identiques, puis regarde les deux images. Une différence de cadrage ou
d'échelle entre les deux rend la comparaison inutilisable.

## Scénarios utiles à garder sous la main

Un mur oblique, une pièce fermée, une porte sur chacune des quatre
orientations, une fenêtre, et un plan complet à plusieurs pièces. Ce dernier
est le meilleur détecteur de régression d'ensemble.

Deux contrôles valent d'être refaits à chaque changement de géométrie:

**Hachurer l'intérieur d'une pièce.** Si la hachure remplit le contour, la
polyligne est une vraie surface fermée. Si elle reste vide, le contour est
ouvert ou fait de segments indépendants.

**Refaire le même plan en millimètres.** Le dessin doit être identique en
proportion. Une épaisseur qui disparaît ou un texte hors d'échelle signale une
longueur codée en dur au lieu de passer par les valeurs par défaut.

## Rapport

Dis ce que tu as regardé et ce que tu as vu, pas seulement que le fichier
a été produit. Signale le chemin de l'image pour que l'utilisateur puisse
la regarder aussi.
