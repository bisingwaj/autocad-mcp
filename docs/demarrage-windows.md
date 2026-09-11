# Démarrage sous Windows avec Cursor ou VS Code

Ce guide vous mène d'une machine Windows vierge à un assistant qui dessine dans
AutoCAD. Comptez vingt minutes.

Il commence par le **mode DXF**, qui fonctionne tout de suite et sans AutoCAD.
Le pilotage direct d'AutoCAD vient ensuite, parce qu'il demande une mise au
point que le mode DXF n'exige pas.

---

## Ce dont vous avez besoin

| | Pour le mode DXF | Pour le pilotage direct |
|---|---|---|
| Windows 10 ou 11 | oui | oui |
| Cursor ou VS Code | oui | oui |
| AutoCAD complet, avec licence | non | **oui** |
| Python | installé par `uv` | installé par `uv` |

AutoCAD **LT** ne convient pas pour le pilotage direct : il n'expose pas
l'automatisation ActiveX. Le mode DXF, lui, fonctionne avec n'importe quelle
version, et même sans AutoCAD du tout.

---

## Étape 1 — Installer uv

`uv` installe Python et les dépendances pour vous. Ouvrez PowerShell :

```powershell
winget install --id=astral-sh.uv -e
```

Si `winget` n'est pas disponible :

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Fermez et rouvrez PowerShell**, puis vérifiez :

```powershell
uv --version
```

Si la commande n'est pas reconnue, c'est que le chemin n'a pas été rechargé :
fermez toutes les fenêtres PowerShell et rouvrez-en une.

---

## Étape 2 — Installer le projet

```powershell
cd $HOME\Documents
git clone https://github.com/bisingwaj/autocad-mcp
cd autocad-mcp

uv sync --extra render
```

Sous Windows, ajoutez le pont vers AutoCAD :

```powershell
uv sync --extra render --extra windows
```

Notez le chemin complet du dossier, vous en aurez besoin à l'étape 4 :

```powershell
(Get-Location).Path
```

Vous obtenez quelque chose comme `C:\Users\VotreNom\Documents\autocad-mcp`.

---

## Étape 3 — Vérifier avant de brancher quoi que ce soit

Cette étape évite de chercher une panne dans le mauvais endroit. Elle ne
dépend d'aucun éditeur.

```powershell
uv run python -c "import autocad_mcp; print(autocad_mcp.__version__)"
uv run autocad-mcp --help
```

La première commande doit afficher un numéro de version, la seconde la liste
des options. Si les deux passent, le serveur est installé correctement.

Produisez maintenant un vrai dessin, toujours sans éditeur ni AutoCAD :

```powershell
uv run python examples/premier_plan.py
```

Vous devez lire :

```
12 entités créées sur 4 calques
Fichier écrit: ...\essai.dxf
Aperçu écrit:  ...\essai.png
```

Ouvrez `essai.png` pour voir tout de suite le résultat, et `essai.dxf` dans
AutoCAD : une pièce de huit mètres sur cinq, murs raccordés aux angles, une
porte percée dans le mur du bas et une fenêtre dans celui du haut.

**Si cette étape passe, le cœur du serveur fonctionne sur votre machine.** Tout
problème ultérieur viendra de la configuration de l'éditeur, pas du serveur.

---

## Étape 4 — Brancher votre éditeur

Les deux éditeurs utilisent un fichier `mcp.json`, mais **leur format diffère**.
Ne copiez pas l'un dans l'autre.

Dans les deux cas, remplacez `C:/Users/VotreNom/Documents/autocad-mcp`
par le chemin relevé à l'étape 2. **Utilisez des barres obliques normales**, ou
doublez les antislashs : `C:\\Users\\...`. Un antislash simple dans du JSON est
une séquence d'échappement et casse le fichier.

### Cursor

Créez `%USERPROFILE%\.cursor\mcp.json` pour une configuration valable partout,
ou `.cursor\mcp.json` dans le dossier du projet pour la limiter à celui-ci. En
cas de doublon, la configuration du projet l'emporte.

```json
{
  "mcpServers": {
    "autocad": {
      "command": "uv",
      "args": [
        "--directory",
        "C:/Users/VotreNom/Documents/autocad-mcp",
        "run",
        "autocad-mcp"
      ],
      "env": {
        "AUTOCAD_MCP_BACKEND": "ezdxf",
        "AUTOCAD_MCP_UNIT": "m",
        "AUTOCAD_MCP_DXF": "C:/Users/VotreNom/Documents/plans/courant.dxf"
      }
    }
  }
}
```

**Quittez complètement Cursor et rouvrez-le.** Les serveurs ne sont chargés
qu'au démarrage. Vérifiez ensuite dans Settings, section MCP, que le serveur
apparaît avec ses douze outils.

### VS Code

Créez `.vscode\mcp.json` à la racine de votre dossier de travail. Vous pouvez
aussi passer par la palette de commandes et lancer `MCP: Add Server`, qui vous
guide.

Notez les deux différences avec Cursor : la clé racine est `servers` et non
`mcpServers`, et le champ `type` est obligatoire.

```json
{
  "servers": {
    "autocad": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory",
        "C:/Users/VotreNom/Documents/autocad-mcp",
        "run",
        "autocad-mcp"
      ],
      "env": {
        "AUTOCAD_MCP_BACKEND": "ezdxf",
        "AUTOCAD_MCP_UNIT": "m",
        "AUTOCAD_MCP_DXF": "C:/Users/VotreNom/Documents/plans/courant.dxf"
      }
    }
  }
}
```

Ce fichier peut être versionné pour partager la configuration avec votre équipe.

---

## Étape 5 — Votre premier plan

Dans le chat de votre éditeur, en mode agent, demandez simplement :

> Dessine un studio de sept mètres sur cinq, avec une salle de bain à droite
> séparée par une cloison, une porte entre les deux et une fenêtre au nord.
> Puis montre-moi le plan.

L'assistant appelle `build_structure` puis `render_view`, et **vous voyez
l'image du plan dans le chat**. C'est ce retour visuel qui lui permet de se
corriger : vous pouvez enchaîner par « la porte s'ouvre du mauvais côté,
inverse-la » et il comprendra ce que vous voyez.

Quelques demandes qui montrent l'étendue du serveur :

- « Place un lit double, une table et deux chaises. »
- « Cote la façade sud et le mur de refend. »
- « Combien de mètres carrés dans le séjour ? »
- « Vérifie le plan et dis-moi s'il y a des défauts. »

Le fichier désigné par `AUTOCAD_MCP_DXF` est réécrit après chaque lot. Dans
AutoCAD, rechargez-le pour voir le résultat.

---

## Étape 6 — Passer au pilotage direct d'AutoCAD

**Avertissement franc : cette partie n'a jamais été exécutée.** Le pont vers
AutoCAD a été écrit sur une machine sans AutoCAD. Le code est complet et
relu, mais il n'a pas encore tourné une seule fois face au logiciel réel.
Attendez-vous à une séance de mise au point, et gardez le mode DXF comme repli.

Voir `windows-checklist.md` pour la liste des quatre-vingt-deux points à
confirmer, et l'ordre dans lequel les dérouler.

Quand vous vous y attaquez :

1. Ouvrez AutoCAD avec un **dessin neuf et jetable**, jamais un plan de
   production.
2. Remplacez votre `mcp.json` par la version ci-dessous. Elle bascule le
   moteur sur AutoCAD, retire le fichier DXF qui n'a plus d'objet, et ajoute
   un journal détaillé. Exemple pour Cursor ; pour VS Code, gardez la clé
   `servers` et le champ `type` de l'étape 4.

```json
{
  "mcpServers": {
    "autocad": {
      "command": "uv",
      "args": [
        "--directory",
        "C:/Users/VotreNom/Documents/autocad-mcp",
        "run",
        "autocad-mcp"
      ],
      "env": {
        "AUTOCAD_MCP_BACKEND": "autocad",
        "AUTOCAD_MCP_UNIT": "m",
        "AUTOCAD_MCP_LOG": "DEBUG",
        "AUTOCAD_MCP_LOGFILE": "C:/Users/VotreNom/autocad-mcp.log"
      }
    }
  }
}
```

3. Redémarrez l'éditeur, puis demandez d'abord « décris-moi le dessin
   courant ». C'est l'appel le moins risqué : il lit, il n'écrit rien.
4. Au moindre doute, lisez `autocad-mcp.log`. Le serveur n'écrit jamais sur la
   sortie standard, qui porte le protocole, donc ce fichier est le seul endroit
   où voir ce qui s'est passé.

Avant même de passer par l'éditeur, faites le premier contact en ligne de
commande. Ce script lit le dessin sans jamais y écrire :

```powershell
uv run python examples/diagnostic_autocad.py
```

Il valide d'un coup les constantes ActiveX supposées et nomme celles qui
diffèrent. Autodesk ne publie pas leurs valeurs, et une constante fausse rend
un jeu de sélection **vide sans lever d'erreur** : c'est le défaut le plus
difficile à repérer, d'où ce contrôle en tout premier.

Trois issues possibles :

- **Aucun écart, lecture réussie.** Le meilleur cas. Passez aux phases
  suivantes de la checklist, celles qui écrivent.
- **Des constantes diffèrent.** Le script les nomme. Reportez les vraies
  valeurs dans le backend, à l'endroit qu'il indique.
- **Échec de connexion.** Le message porte un code et la marche à suivre.

---

## Dépannage

**Le serveur n'apparaît pas dans l'éditeur.** Vous n'avez pas complètement
quitté l'application. Fermez toutes les fenêtres, vérifiez dans le gestionnaire
des tâches, puis rouvrez.

**« uv n'est pas reconnu ».** L'éditeur ne voit pas la même variable de chemin
que votre terminal. Remplacez `"command": "uv"` par le chemin complet, que vous
obtenez avec `(Get-Command uv).Source`.

**Le fichier JSON est refusé.** Presque toujours un antislash simple dans un
chemin. Utilisez `C:/Users/...` ou `C:\\Users\\...`.

**Les outils apparaissent mais tout appel échoue.** Lisez le journal en posant
`AUTOCAD_MCP_LOGFILE`. Le serveur n'écrit jamais sur la sortie standard, qui
porte le protocole, donc le journal est le seul endroit où regarder.

**« BackendUnavailable » sous Windows.** Le pont AutoCAD n'est pas installé :
relancez `uv sync --extra render --extra windows`. Si le message persiste,
AutoCAD n'est pas lancé, ou c'est une version LT.

**Les murs sont invisibles ou minuscules.** Votre dessin est en millimètres
alors que le serveur travaille en mètres. Posez `"AUTOCAD_MCP_UNIT": "mm"` et
donnez vos dimensions en millimètres.

**Le dessin n'a pas changé dans AutoCAD.** En mode DXF, le fichier est réécrit
mais AutoCAD ne le recharge pas tout seul. Fermez et rouvrez le dessin.

---

## Les douze outils, en un coup d'œil

| Outil | Ce qu'il fait |
|---|---|
| `get_drawing_info` | état du document, unité, calques |
| `query_entities` | inspection filtrée du dessin |
| `measure` | surfaces, longueurs, nomenclature |
| `check_plan` | défauts : contours ouverts, croisements, doublons |
| `render_view` | **renvoie l'image du plan** |
| `draw` | primitives : ligne, polyligne, cercle, arc, texte, hachure |
| `build_structure` | murs et baies, pièces, étiquettes |
| `place_blocks` | sanitaires, électricité, mobilier |
| `delete_entities` | suppression filtrée |
| `set_entity_color` | changement de couleur |
| `undo_last_batch` | annulation du dernier lot |
| `run_cad_command` | passerelle AutoCAD, sur liste blanche |
