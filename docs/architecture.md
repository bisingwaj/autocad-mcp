# Architecture

## Le problème d'origine

Le serveur tenait en un fichier de 1713 lignes où la logique métier et l'appel
COM étaient le même code. `create_wall` appelait directement `AddLine` sur
AutoCAD. Trois conséquences, toutes coûteuses.

**Rien n'était testable.** Vérifier qu'un mur se dessinait correctement exigeait
Windows, une licence AutoCAD et une instance ouverte. La machine de
développement est un Mac : l'auteur ne pouvait pas exécuter son propre projet.

**Rien n'était rapide.** Chaque primitive s'accompagnait d'une seconde de
sommeil et d'un rafraîchissement complet du document. Un rectangle coûtait
quatre secondes, une maison de cinq pièces dépassait la minute. Ces sommeils
bloquaient de surcroît la boucle asynchrone, donc le serveur entier était gelé
pendant qu'il dessinait.

**Rien n'était vérifiable par le modèle.** Les opérations échouées renvoyaient
`success: true` avec des handles fabriqués comme `line_created`. Le modèle
croyait avoir dessiné, ne voyait jamais le résultat, et ne pouvait donc pas
se corriger.

## La décision

La logique métier décide **quoi** dessiner et produit une liste d'opérations
déclaratives. Un backend exécute **comment**. Les deux mondes ne se rencontrent
qu'à la frontière du protocole `CadBackend`.

```
   ops/architecture.py          model/ops.py            backends/
   « fais-moi un mur »   ──►   AddPolyline(...)   ──►   AutoCAD ou DXF
   fonction pure               donnée immuable          exécution réelle
```

Concrètement, la fonction qui crée un mur retourne une `AddPolyline` fermée sur
le calque `WALLS`. Elle ne connaît ni AutoCAD, ni `ezdxf`, ni COM.

## Ce que la décision apporte

**Les tests s'exécutent sur macOS.** Le backend d'enregistrement reçoit les
opérations et un test affirme sur leur contenu, sans toucher au moindre
logiciel de CAO. Le backend DXF va plus loin et produit un fichier réel,
relu et audité.

**Le traitement par lots devient trivial.** La liste complète est connue avant
toute exécution, donc une seule marque d'annulation encadre un plan entier et
un seul rafraîchissement a lieu, à la fin. Annuler une maison redevient un geste.

**Le rendu devient possible partout.** Un dessin exprimé en opérations se rend
en image sans AutoCAD, ce qui referme la boucle de correction : le modèle voit
son propre plan.

**Un second moteur apparaît.** Le backend DXF n'est pas qu'un outil de test.
C'est un mode de production utilisable sur Mac et Linux, sans licence Autodesk.

## Les frontières

| Couche | Connaît | Ne connaît pas |
|---|---|---|
| `geometry`, `units` | rien du projet | tout le reste |
| `model/ops`, `model/layers` | `geometry`, `units` | backends, MCP |
| `ops/*` | `model`, `geometry`, `units` | backends, MCP |
| `backends/*` | `model/ops` | `ops/*`, MCP |
| `tools/*`, `server` | tout | — |

Ces frontières ne reposent pas sur la discipline. Elles sont vérifiées par
`tests/test_architecture.py`, qui analyse l'arbre syntaxique de chaque module.
Un import de backend depuis la logique métier fait échouer la suite.

## Le cas particulier du backend AutoCAD

Il ne peut pas être exécuté sur la machine de développement. Sa correction
repose donc sur trois appuis.

**L'import paresseux.** `win32com` n'est jamais importé au niveau module, ce qui
laisse le fichier chargeable sur macOS. Un test le vérifie sur chaque fichier
du paquet.

**La symétrie.** L'outil `contract_diff` compare les méthodes réellement
honorées par chaque backend. Une méthode présente d'un côté et absente de
l'autre est soit un oubli, soit une limitation qui doit lever
`UnsupportedOperation` explicitement.

**La liste de confirmation.** Tout comportement supposé mais non vérifiable ici
s'inscrit dans `windows-checklist.md`, avec le fichier, la ligne, l'hypothèse et
la manière de la tester. Cette liste permet de valider le portage en une seule
session devant AutoCAD, au lieu de découvrir les problèmes un par un.
