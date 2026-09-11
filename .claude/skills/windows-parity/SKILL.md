---
name: windows-parity
description: Vérifie que le backend AutoCAD COM tient la même promesse que le backend ezdxf, et tient à jour la liste des points à confirmer lors du premier essai sous Windows. À utiliser après toute modification du backend COM, et avant de livrer une version destinée à tourner sur une machine AutoCAD.
---

# Parité entre les backends

Le backend COM ne peut pas être exécuté sur cette machine. Sa correction
repose donc entièrement sur la revue, sur la symétrie avec le backend `ezdxf`
qui lui est testé, et sur une liste explicite de ce qui reste à confirmer.

## 1. Symétrie du contrat

Compare les méthodes réellement implémentées de part et d'autre:

```bash
uv run python -m autocad_mcp.devtools.contract_diff
```

Toute méthode présente dans un backend et absente de l'autre est soit un oubli,
soit une limitation qui doit lever `UnsupportedOperation` de façon explicite.
Il n'y a pas de troisième cas acceptable.

## 2. Revue des pièges COM

Relis le code modifié en cherchant ces défauts précis:

- Un point passé autrement que par un `VARIANT` de tableau de réels.
- Un appel COM effectué hors du thread STA dédié.
- Un regen déclenché par entité au lieu d'un seul en fin de lot.
- Un parcours du ModelSpace là où `HandleToObject` ou un jeu de sélection
  filtré ferait le travail.
- Une annulation pilotée par `SendCommand` au lieu des marques natives.
- Une commande transmise à AutoCAD sans passer par la liste blanche.
- Un `except` qui avale l'erreur, ou un retour de succès sans preuve.

L'agent `acad-com` connaît ces pièges en détail.

## 3. Tenue de la liste de confirmation

Tout comportement supposé mais non vérifiable ici s'inscrit dans
`docs/windows-checklist.md`, avec le fichier et la ligne concernés, ce qu'on
attend, et comment le vérifier une fois devant AutoCAD.

Cette liste est le livrable qui permettra de valider le portage en une seule
session sur une machine Windows, au lieu de découvrir les problèmes un par un.

## 4. Tests marqués

Les tests de contrat destinés au backend COM existent et portent le marqueur
`windows`. Ils sont sautés ici, mais leur présence est vérifiée:

```bash
uv run pytest -m windows --collect-only -q | tail -3
```

Un marqueur qui ne collecte aucun test signifie que la couverture Windows
a disparu.

## Rapport

Donne l'écart de contrat, les défauts trouvés à la revue, et le nombre
d'entrées ouvertes dans la liste de confirmation.
