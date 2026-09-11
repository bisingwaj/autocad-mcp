---
name: add-cad-tool
description: Procédure complète pour ajouter une nouvelle capacité de dessin au serveur AutoCAD MCP, de l'opération jusqu'à l'outil exposé au modèle. À utiliser dès qu'on veut ajouter une primitive, une structure architecturale, une cote, un bloc, une hachure ou tout nouvel outil MCP. Garantit que les six couches sont traitées et qu'aucune ne soit oubliée.
---

# Ajouter une capacité de dessin

Une capacité traverse six couches. En sauter une produit soit un outil
inutilisable par le modèle, soit une régression silencieuse sous Windows.
Suis l'ordre, il est conçu pour que chaque étape soit vérifiable avant la suivante.

## 1. L'opération déclarative

Dans `src/autocad_mcp/model/ops.py`, ajoute une dataclass figée qui décrit
**ce qu'il faut dessiner**, jamais comment. Elle ne contient que des données:
coordonnées, calque, couleur, épaisseur. Aucune méthode qui dessine.

Si l'opération se ramène à des opérations existantes, ne crée rien. Un rectangle
est une polyligne fermée, pas une opération distincte.

## 2. La logique métier

Dans `src/autocad_mcp/ops/`, écris la fonction qui produit la liste d'opérations.
Elle est pure: mêmes entrées, mêmes sorties, aucun appel externe. Les calculs
géométriques sont délégués à `geometry.py`, jamais écrits sur place.

Choisis le bon module: `primitives` pour les formes nues, `architecture` pour
les éléments de bâtiment, `annotate` pour les cotes et textes, `query` pour
la lecture.

## 3. Le contrat des backends

Si l'opération est réellement nouvelle, ajoute la méthode au protocole
`backends/base.py`, puis implémente-la **dans tous les backends**:

- `ezdxf_be.py` la met en œuvre et devient la preuve exécutable sur macOS.
- `acad_com.py` la met en œuvre pour AutoCAD. Ce code n'est pas exécutable ici,
  donc il est défensif et ses hypothèses sont documentées.
- `recording.py` l'enregistre, ce qui est automatique si elle passe par le
  mécanisme générique.

Un backend qui ne peut pas honorer l'opération lève `UnsupportedOperation`.
Il ne l'ignore jamais en silence.

## 4. Les tests

Trois tests au minimum, écrits avant de brancher l'outil MCP:

- Un test d'opération contre le backend d'enregistrement, qui affirme la forme
  exacte de la liste produite.
- Un test de référence qui génère un DXF par `ezdxf`, le relit et vérifie
  la structure obtenue.
- Un test de cas dégénéré: longueur nulle, coordonnées identiques, valeur négative.

Lance la suite et regarde la sortie. Un test non exécuté n'existe pas.

## 5. L'outil MCP

Dans `src/autocad_mcp/tools/`, déclare le schéma et branche le routage.
Chaque paramètre porte une description et son unité. Les opérations
destructrices exigent une confirmation explicite.

Le test d'unicité des noms d'outils doit rester vert.

## 6. La documentation

Mets à jour le tableau des outils dans le README, et note dans
`docs/windows-checklist.md` tout comportement qui reste à confirmer
lors du premier essai réel sous AutoCAD.

## Vérification finale

Lance la compétence `verify-cad`. Elle enchaîne le contrôle d'architecture,
les types, la suite complète et le test d'importabilité sur macOS.
