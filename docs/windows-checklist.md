# Checklist de portage — backend AutoCAD COM

`src/autocad_mcp/backends/acad_com.py` a été écrit **sur macOS, sans AutoCAD**.
Rien de ce qui touche COM n'a pu être exécuté. Ce document liste, point par
point, tout ce qui reste à confirmer au premier essai sous Windows.

**Ce qui a réellement été vérifié sur Mac**, et n'a donc pas à être repris ici :
le module s'importe sans charger `win32com` ni `pythoncom` ; `connect()` lève
`BackendUnavailable` et non `ImportError` ; les neuf méthodes abstraites de
`CadBackend` sont honorées ; le thread STA sérialise bien huit appelants
concurrents, relève les exceptions à l'identique, ne s'interbloque pas en
réentrance et respecte son délai ; les tables de correspondance
`kind → type DXF` et `alignement de texte` couvrent tout `model/ops.py` ;
le décodage des HRESULT, y compris `DISP_E_EXCEPTION` enveloppé, rend le bon
code ; `ruff check`, `mypy --strict` et la suite de tests passent.

S'y ajoutent, depuis les chantiers « définition de bloc », « commandes natives »
et « mesures » :

- **L'assainissement des commandes est du Python pur, il a donc été exécuté.**
  `_validated_command` et `_validated_argument` ont été éprouvés sur une série
  d'injections : `OFFSET;ERASE`, `(command "_.erase" "all" "")`, `OFFSET\n_QUIT`,
  `_OFFSET(setq a 1)`, `OFFSET"x"`, `9OFFSET`, `._`, `ERASE` hors liste, ainsi
  que les tabulations et fins de ligne en tête ou en queue. Toutes refusées.
  `OFFSET`, `offset`, `._offset`, `.TRIM` et `  HATCH  ` rendent bien le nom
  canonique. La liste blanche du backend a été comparée à
  `Config.allowed_commands` : identiques.
- **La logique d'aiguillage a été éprouvée contre un faux ActiveX**, un objet
  Python imitant la surface d'appel supposée. Cela ne prouve **rien** du
  comportement d'AutoCAD — c'est précisément l'objet des entrées W-66 à W-73 —
  mais cela vérifie le code du backend : `DefineBlock` ne produit aucun handle
  et remplit `blocks`, une redéfinition conserve l'existant, un contenu qui
  échoue ne laisse pas de bloc entamé, `insert()` ne crée qu'une entité, le
  repère est redressé après la pose, les clés de mesure sortent pour les bons
  types et `ops.query` les additionne, `run_command` rend `completed: None` et
  un compte d'entités `None` plutôt que zéro quand il est illisible.

**Convention de lecture.** Chaque entrée porte un identifiant, l'emplacement
exact dans le code, l'hypothèse formulée, le geste de vérification, et la
correction à appliquer si l'hypothèse est fausse.

**89 entrées**, réparties en 11 phases à dérouler dans l'ordre. Les phases 0 à 3
conditionnent tout le reste : ne pas passer à la suivante tant que la
précédente n'est pas verte.

**Les deux entrées les plus incertaines du document sont W-66 et W-76** : ce que
rend réellement `Blocks.Add`, et ce que signifie réellement « asynchrone » pour
`SendCommand`. Tout le chantier des blocs dépend de la première, toute la
passerelle de commandes de la seconde.

**Le chantier des maillages ajoute une troisième incertitude du même ordre :
W-84**, la base — zéro ou un — des indices de facette. Elle diffère des deux
premières sur un point qui la rend plus dangereuse, pas moins : une hypothèse
fausse ici **ne lève aucune erreur**. Elle produit un volume déformé, visible
seulement au rendu, jamais dans `BatchResult.failures`.

---

## Préparation : environnement

Machine Windows avec **AutoCAD complet** — AutoCAD LT n'expose pas
l'automatisation ActiveX et rend cette checklist sans objet.

```bat
python -m pip install -e ".[windows]"
python -m pip show pywin32
```

Avant tout essai destructif : ouvrir un dessin **neuf et jetable**, jamais un
plan de production. Plusieurs entrées ci-dessous créent et suppriment des
entités, et la phase 6 en supprime volontairement.

Journal recommandé pendant toute la session, il rend chaque échec lisible :

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## Phase 0 — Chargement et connexion

### W-01 — L'import de `pywin32` aboutit sous Windows
- **Où** : `acad_com.py:603` `_ensure_modules`
- **Hypothèse** : `import pythoncom` et `import win32com.client` réussissent, et
  `pythoncom.com_error` existe bien sous ce nom.
- **Vérifier** : `AcadComBackend()._ensure_modules()` ne lève rien ;
  `backend._com_error is not _NeverRaised`.
- **Si faux** : `pywin32` mal installé — exécuter
  `python Scripts/pywin32_postinstall.py -install`.

### W-02 — Le garde-fou de plateforme ne bloque pas Windows
- **Où** : `acad_com.py:476` — `if sys.platform != "win32"`
- **Hypothèse** : `sys.platform` vaut exactement `"win32"`, y compris sur un
  Python 64 bits (c'est le cas, mais le code en dépend entièrement).
- **Vérifier** : `print(sys.platform)`.
- **Si faux** : le backend refuserait de démarrer sur la machine cible. Élargir
  le test à `sys.platform.startswith("win")`.

### W-03 — `CoInitialize` réussit sur le thread worker
- **Où** : `acad_com.py:300`
- **Hypothèse** : `pythoncom.CoInitialize()` (STA) suffit ; `CoInitializeEx`
  avec `COINIT_APARTMENTTHREADED` n'est pas nécessaire.
- **Vérifier** : `connect()` puis `backend.diagnostics()["worker_alive"] is True`.
- **Si faux** : `BackendUnavailable("CoInitialize a échoué")`. Cause probable :
  le thread hôte a déjà initialisé COM en MTA. Basculer sur
  `CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)`.

### W-04 — `GetActiveObject` rejoint l'instance ouverte
- **Où** : `acad_com.py:668`
- **Hypothèse** : `win32com.client.GetActiveObject("AutoCAD.Application")` rend
  l'instance déjà lancée. Recette reprise telle quelle du serveur historique.
- **Vérifier** : AutoCAD ouvert **avant** l'appel, puis `connect()` ; contrôler
  que `document_info()["name"]` est bien le dessin affiché à l'écran, et
  qu'**aucune deuxième instance** n'a démarré.
- **Si faux** : avec plusieurs instances ouvertes, la table des objets en cours
  n'en rend qu'une, pas forcément celle du premier plan. À documenter comme
  limite, ou à sélectionner par `Documents` sur le nom du fichier.

### W-05 — `Dispatch` démarre AutoCAD, et la scrutation suffit
- **Où** : `acad_com.py:836` `_launch`, appel ligne 698
- **Hypothèse** : quand aucune instance ne tourne, `Dispatch` en lance une, et
  interroger `app.Documents.Count` toutes les `startup_poll_interval` (0,5 s)
  pendant `startup_timeout` (120 s) détecte le moment où elle répond. Remplace
  le `time.sleep(2)` aveugle du code historique.
- **Vérifier** : AutoCAD **fermé**, chronométrer `connect()`. Doit rendre la main
  dès qu'AutoCAD est prêt, jamais après le délai maximal.
- **Si faux** : si `Documents.Count` répond avant qu'AutoCAD n'accepte de
  dessiner, sonder une propriété plus tardive, par exemple `app.ActiveDocument.Name`.

### W-06 — `app.Visible = True` n'échoue pas et n'est pas indispensable
- **Où** : `acad_com.py:681`
- **Hypothèse** : l'échec est tolérable, une instance invisible reste pilotable.
  L'erreur n'est que journalisée.
- **Vérifier** : AutoCAD passe au premier plan après `connect()`.
- **Si faux** : sans conséquence fonctionnelle, mais l'utilisateur ne voit rien
  se dessiner et croit à une panne. Remonter alors l'information dans
  `document_info()`.

### W-07 — Un bureau vide reçoit un nouveau document
- **Où** : `acad_com.py:871` `_acquire_document`
- **Hypothèse** : `Documents.Count == 0` ⇒ `Documents.Add()` ; sinon
  `app.ActiveDocument`.
- **Vérifier** : lancer AutoCAD sans dessin ouvert, puis `connect()`.
- **Si faux** : `Documents.Add()` peut ouvrir une boîte de dialogue de choix de
  gabarit selon `STARTUP`/`FILEDIA`, ce qui **gèle l'appel COM** jusqu'au délai
  de W-45. Passer alors un gabarit explicite : `Documents.Add("acadiso.dwt")`.

### W-08 — La sonde de vie du document est fiable
- **Où** : `acad_com.py:880` `_document_is_alive`
- **Hypothèse** : lire `doc.Name` échoue par `com_error` si le document a été
  fermé ou AutoCAD arrêté, ce qui produit un `NotConnected` clair.
- **Vérifier** : `connect()`, fermer le dessin **dans AutoCAD**, puis appeler
  `count()`. Attendu : `NotConnected`, pas un plantage ni un gel.
- **Si faux** : si la lecture rend une chaîne vide au lieu de lever, la sonde ne
  détecte rien. Tester alors aussi `app.Documents.Count`.

---

## Phase 1 — Constantes de l'API ActiveX

C'est le point le plus incertain de tout le portage. `win32com.client.constants`
n'est peuplé **que** si la bibliothèque de types a été générée par makepy. En
liaison tardive pure, elle est vide et `constants.acActiveViewport` lève
`AttributeError`. Le code historique enveloppait chaque `Regen` dans un
`try/except` muet : **son regen ne s'exécutait très probablement jamais**.

Générer la bibliothèque de types, une fois pour toutes :

```bat
python -m win32com.client.makepy -i "AutoCAD 2024 Type Library"
python -c "import win32com.client as w; w.gencache.EnsureDispatch('AutoCAD.Application')"
```

### W-09 — Contrôle groupé des constantes
- **Où** : `acad_com.py:641` `_const`, table de repli `acad_com.py:119-151`
- **Hypothèse** : à défaut de typelib, les littéraux de `_FALLBACK_CONSTANTS`
  sont justes.
- **Vérifier**, en une seule commande, **après** makepy :
  ```python
  import json; print(json.dumps(backend.diagnostics()["constants_differ_from_fallback"], indent=2))
  ```
  Toute valeur à `true` signale un littéral **faux** dans le code source.
- **Si faux** : corriger le littéral correspondant dans `_FALLBACK_CONSTANTS` et
  noter ici la valeur réelle, pour les machines sans typelib générée.

### W-10 — `acActiveViewport = 3`
- **Où** : `acad_com.py:122`, utilisé `acad_com.py:2001`
- **Hypothèse** : valeur de `AcRegenType`. **Autodesk ne publie pas les valeurs
  numériques de cette énumération** ; celle-ci vient de sources communautaires.
- **Vérifier** : `print(win32com.client.constants.acActiveViewport)` après makepy.
- **Si faux** : le `Regen` échouera silencieusement — seulement journalisé en
  avertissement, les entités restant valides. Symptôme : les entités créées
  n'apparaissent qu'après un zoom manuel.

### W-11 — `acSelectionSetAll = 5`
- **Où** : `acad_com.py:129`, utilisé `acad_com.py:1933` et `1945`
- **Hypothèse** : valeur de `AcSelect`. Cette énumération est donnée sans
  valeurs numériques dans la documentation Autodesk. L'ordre retenu place
  `acSelectionSetFence` en 2, ce qui décale `Previous`, `Last` et `All` d'un
  cran par rapport à l'autre ordre couramment cité. **Un décalage d'un rend un
  jeu de sélection vide, pas une erreur** : c'est le mode d'échec le plus
  sournois de ce fichier.
- **Vérifier** : dessiner 3 cercles, puis `backend.count(EntityFilter(kind="circle"))`.
  Attendu : `3`. Un `0` inexpliqué désigne cette entrée.
- **Si faux** : corriger `_FALLBACK_CONSTANTS["acSelectionSetAll"]`, valeur
  alternative la plus probable : `4`.

### W-12 — `acSelectionSetWindow = 0`
- **Où** : `acad_com.py:124`, utilisé `acad_com.py:1922-1928`
- **Hypothèse** : mode fenêtre, qui ne retient que les entités **entièrement
  contenues**, contrairement à `acSelectionSetCrossing`.
- **Vérifier** : un cercle dedans, un cercle à cheval sur le bord, puis
  `count(EntityFilter(window=(0, 0, 100, 100)))`. Attendu : `1`, pas `2`.
- **Si faux** : si le compte vaut 2, la constante désigne en réalité le mode
  capture. Échanger 0 et 1.

### W-13 — Les codes `AcAlignment`
- **Où** : `acad_com.py:157-169` `_TEXT_ALIGNMENT`
- **Hypothèse** : `acAlignmentLeft = 0`, puis `TopLeft = 6` … `BottomRight = 14`.
- **Vérifier** : voir W-27, contrôle visuel.

### W-14 — `acHatchPatternTypePreDefined = 1`
- **Où** : `acad_com.py:136`, utilisé `acad_com.py:1117`
- **Hypothèse** : les motifs nommés comme `SOLID` ou `ANSI31` sont des motifs
  prédéfinis, type 1.
- **Vérifier** : voir W-29.
- **Si faux** : `AddHatch` lève une `com_error` immédiate, donc l'échec est
  visible, pas silencieux.

---

## Phase 2 — VARIANT, la recette héritée

### W-15 — Le VARIANT de point
- **Où** : `acad_com.py:669` `_point`
- **Hypothèse** : `VARIANT(VT_ARRAY | VT_R8, [x, y, z])`. **Recette éprouvée en
  production** dans `server.py:62` ; conservée à l'identique, et c'est à ce
  titre le seul élément COM de ce fichier dont on ait une preuve d'usage réel.
- **Vérifier** : `execute_one(AddLine(start=(0,0,0), end=(100,100,0)))` rend
  `ok is True` et un handle.
- **Si faux** : rien ne se dessinerait du tout. Priorité absolue.

### W-16 — Le VARIANT de tableau plat
- **Où** : `acad_com.py:683` `_doubles`
- **Hypothèse** : même forme `VT_ARRAY | VT_R8`, longueur quelconque mais
  **paire** pour les sommets 2D. Dérivée de `server.py:66`, qui définissait le
  helper mais **ne l'appelait jamais** — donc jamais exercée en production.
- **Vérifier** : voir W-24.

### W-17 — Les VARIANT de filtre
- **Où** : `acad_com.py:692` `_shorts` et `acad_com.py:701` `_variants`
- **Hypothèse** : les codes de groupe DXF veulent `VT_ARRAY | VT_I2` (entiers
  courts) et les valeurs `VT_ARRAY | VT_VARIANT`. Une inversion, ou des codes
  en `VT_I4`, fait rejeter le filtre.
- **Vérifier** : voir W-11 puis W-35.
- **Si faux** : `com_error` sur `Select`, capturée et traduite en
  `OperationFailed("Sélection filtrée refusée par AutoCAD")`.

### W-18 — Le VARIANT d'objets COM
- **Où** : `acad_com.py:707` `_dispatches`
- **Hypothèse** : `VT_ARRAY | VT_DISPATCH` accepte directement les wrappers
  pywin32, sans passer par `._oleobj_`.
- **Vérifier** : voir W-30.
- **Si faux** : le code retombe automatiquement sur la liste Python nue, voir la
  cascade W-30.

---

## Phase 3 — Création des entités

Passage minimal, un lot par famille, en contrôlant à chaque fois que le handle
rendu est **un vrai handle hexadécimal AutoCAD** (`"2AF"`, pas `"line_created"`
comme dans le code historique) et qu'il est retrouvable :

```python
res = backend.execute(batch)
assert res.ok, res.failures
assert backend.query(EntityFilter(handles=tuple(res.to_dict()["handles"])))
```

### W-19 — `AddLine`
- **Où** : `acad_com.py:982`
- **Hypothèse** : `ModelSpace.AddLine(p1, p2)` avec deux VARIANT de points.
- **Vérifier** : le segment apparaît aux bonnes coordonnées.

### W-20 — `AddCircle`
- **Où** : `acad_com.py:1037`
- **Hypothèse** : `AddCircle(centre, rayon)`, rayon en double.

### W-21 — Les angles ActiveX sont en **radians**, contrairement au DXF
- **Où** : `acad_com.py:1047`, justification `acad_com.py:1041`
- **Contradiction à trancher, la plus importante du fichier.** L'en-tête de
  `model/ops.py:15-19` affirme que « les formats DXF **et ActiveX** attendent des
  degrés » et demande aux backends de convertir. C'est vrai du DXF, qui stocke
  bien des degrés, mais **faux d'ActiveX** : la documentation Autodesk de la
  méthode `AddArc` dit « the start angle **in radians** », et la propriété
  `Rotation` est elle aussi documentée en radians. Ce backend **ne convertit
  donc pas**. Convertir décalerait tous les arcs d'un facteur 180/π.
- **Vérifier** : `AddArc(center=(0,0,0), radius=50, start_angle=0, end_angle=math.pi/2)`
  doit tracer **exactement le quart supérieur droit**. Si l'arc obtenu est un
  filet quasi nul, ActiveX attendait des degrés et l'en-tête de `ops.py` a
  raison — ajouter alors `math.degrees()` sur les quatre grandeurs angulaires
  du fichier : `AddArc`, `Text.Rotation`, `MText.Rotation`, `Hatch.PatternAngle`.
- **Puis, séparément, le sens** : un arc couvrant les trois autres quarts
  signifie que le sens est horaire et non direct ; échanger alors les deux
  angles à l'appel.
- **Dans tous les cas** : corriger l'en-tête de `model/ops.py` pour qu'il ne
  dise plus « ActiveX attend des degrés », sous peine d'induire en erreur le
  prochain qui touchera ce fichier.

### W-22 — `Hatch.PatternAngle` suit-il la même convention ?
- **Où** : `acad_com.py:1143`
- **Hypothèse** : radians, par cohérence avec le reste d'ActiveX. **Non
  confirmé** : la documentation Autodesk consultée ne précise pas l'unité de
  cette propriété, contrairement à `AddArc` et `Rotation`.
- **Vérifier** : `AddHatch(pattern="ANSI31", angle=math.pi/4)`. Les hachures
  doivent tourner de 45°. Si elles paraissent inchangées, la propriété est en
  degrés et 0,785° est invisible : appliquer `math.degrees()` ici seulement.

### W-23 — La validation est mutualisée avec le backend DXF
- **Où** : `acad_com.py:884` — `validate(operation)` avant tout appel COM
- **Hypothèse** : c'est un choix d'architecture, pas une hypothèse COM. Le
  backend n'applique **aucune règle géométrique locale** : il délègue à
  `model.ops.validate`, pour qu'un rayon négatif échoue à l'identique sur
  AutoCAD et sur DXF. Conséquence assumée : un arc de 0 à 2π **passe**, puisque
  `validate` ne refuse que `start_angle == end_angle`.
- **Vérifier** : rien côté COM. Confirmer que l'arc de 0 à 2π donne le même
  résultat sur les deux backends — un cercle complet, ou une erreur, mais le
  même des deux côtés.

### W-24 — `AddLightWeightPolyline`, tableau plat
- **Où** : `acad_com.py:1027`
- **Hypothèse** : la méthode attend un tableau **plat de coordonnées 2D**
  `[x1, y1, x2, y2, …]` de longueur paire, et **non** une liste de points ni des
  triplets 3D. Remplace les quatre `AddLine` indépendants du rectangle
  historique (`server.py:1360`).
- **Vérifier** : polyligne à 4 sommets ; **cliquer dessus dans AutoCAD** : elle
  doit se sélectionner d'un seul clic, entièrement.
- **Si faux** : `com_error` immédiate, ou polyligne dégénérée si les
  coordonnées sont décalées d'un rang.

### W-25 — `.Closed = True`
- **Où** : `acad_com.py:1031`
- **Hypothèse** : affecter `Closed` après création relie le dernier sommet au
  premier, **sans** qu'il faille répéter le premier point en fin de tableau.
- **Vérifier** : sur la polyligne de W-24, `LIST` dans AutoCAD indique
  « fermée » et l'**aire** est calculable. Vérifier aussi qu'il n'y a **pas de
  sommet doublon** au point de départ.
- **Si faux** : si la polyligne reste ouverte, répéter le premier sommet. Si un
  doublon apparaît, c'est l'inverse.

### W-26 — `ConstantWidth`
- **Où** : `acad_com.py:992`
- **Hypothèse** : `ConstantWidth` applique une épaisseur uniforme à tous les
  segments, dans l'unité du document.
- **Vérifier** : `AddPolyline(..., width=2.0)` donne un trait visiblement épais.
- **Si faux** : l'échec est capturé et rapporté dans `failures`, sous
  `stage: "style"`. L'entité existe quand même.

### W-27 — `AddText` et le piège de l'alignement
- **Où** : `acad_com.py:1057`, alignement `acad_com.py:1072-1075`
- **Hypothèse, la plus importante de la phase** : dès que `Alignment` vaut autre
  chose que `acAlignmentLeft`, AutoCAD **ignore `InsertionPoint`** et positionne
  le texte selon `TextAlignmentPoint`. Le code affecte donc les deux, dans cet
  ordre : `Alignment` puis `TextAlignmentPoint`.
- **Vérifier** : trois textes au **même** point, `halign` valant `left`,
  `center`, `right`. Ils doivent se chevaucher autour de ce point commun. S'ils
  se retrouvent tous à la même position, à gauche, `TextAlignmentPoint` n'a pas
  été pris.
- **Si faux** : inverser l'ordre des deux affectations, ou affecter
  `TextAlignmentPoint` avant la création.

### W-28 — `AddMText` et la signification de `Height`
- **Où** : `acad_com.py:1084`, hauteur `acad_com.py:1089`
- **Hypothèse** : signature `AddMText(point, largeur, texte)` où `largeur` est
  la largeur du cartouche (0 = pas de retour à la ligne), et `Height` la
  **hauteur de caractère**, non la hauteur du bloc.
- **Vérifier** : `AddMText(height=2.5, width=50)` sur un texte long. Contrôler
  dans les propriétés AutoCAD que « Hauteur de texte » vaut 2,5 et que le
  retour à la ligne se fait bien à 50.
- **Si faux** : si `Height` est en lecture seule, l'échec est rapporté dans
  `failures`. Passer alors par `mtext.TextString` avec un code de formatage
  `\H2.5x`, ou par un style de texte dédié.

### W-29 — `AddHatch`, création et évaluation
- **Où** : `acad_com.py:1116`, `PatternScale` `1136`, `Evaluate` `1148`
- **Hypothèse** : `AddHatch(typeDeMotif, nomDuMotif, associativité)` puis ajout
  des contours, puis réglages, puis `Evaluate()` en **dernier**. Sans
  `Evaluate`, la hachure reste vide.
- **Vérifier** : hachure `SOLID` sur un carré, puis une hachure `ANSI31` avec
  `scale=2, angle=0.785`. Le remplissage doit être visible **immédiatement**.
- **Si faux** : si le motif apparaît mais pas à la bonne échelle, déplacer
  `PatternScale`/`PatternAngle` **après** `Evaluate()`.

### W-30 — `AppendOuterLoop` / `AppendInnerLoop`
- **Où** : `acad_com.py:1321` `_append_loop`
- **Hypothèse** : la méthode attend un SAFEARRAY d'IDispatch. Le code tente
  d'abord le VARIANT `VT_DISPATCH`, puis retombe sur une liste Python nue, et
  **mémorise la forme qui passe** pour la session.
- **Vérifier** : après une hachure réussie,
  `backend.diagnostics()["hatch_loop_strategy"]`. Attendu : `"variant"`.
- **Si faux** : la valeur `"list"` indique que le VARIANT est refusé par cette
  combinaison AutoCAD/pywin32 — le noter ici et inverser l'ordre de la cascade
  pour économiser un aller-retour par contour.

### W-31 — Contours de hachure et détection d'îlots
- **Où** : `acad_com.py:1126` — `HatchStyle` puis `AppendOuterLoop` pour **tous** les contours
  les contours
- **Hypothèse** : alignement délibéré sur le backend `ezdxf`, qui ajoute lui
  aussi chaque contour comme chemin extérieur et laisse la détection d'îlots
  décider de ce qui est un trou. Traiter le premier contour comme extérieur et
  les suivants comme des trous ferait diverger les deux moteurs dès qu'un lot
  porte **deux régions disjointes** : AutoCAD percerait la seconde au lieu de la
  hachurer. `HatchStyle` est posé explicitement (`acHatchStyleNormal`) pour que
  le résultat ne dépende pas des variables système du poste.
- **Vérifier**, deux cas, et **comparer au rendu du backend `ezdxf` sur le même
  lot** :
  1. carré + carré intérieur ⇒ un **anneau** hachuré, pas un carré plein ;
  2. deux carrés **disjoints** ⇒ les **deux** hachurés, aucun percé.
- **Si faux** : si le cas 2 donne un trou, `acHatchStyleNormal` n'a pas pris —
  vérifier sa valeur, puis seulement en dernier recours revenir à
  `AppendInnerLoop` pour les contours suivants, en alignant `ezdxf` de la même
  façon.

### W-32 — Les contours restent dans le dessin
- **Où** : `acad_com.py:1131-1132`
- **Hypothèse** : les polylignes de contour sont conservées et leurs handles
  rapportés comme créés, puisqu'elles existent réellement.
- **Vérifier** : une `AddHatch` à un contour rend **deux** handles. Confirmer que
  c'est le comportement voulu par la couche outils.
- **Si faux** : la hachure étant non associative, les contours peuvent être
  supprimés après `Evaluate()` — à valider visuellement, la hachure doit
  survivre.

### W-33 — `AddDimAligned`
- **Où** : `acad_com.py:1182`, `TextOverride` `1190`
- **Hypothèse** : `AddDimAligned(p1, p2, positionDuTexte)` en points 3D
  (z = 0), et la position du texte détermine le déport de la ligne de cote.
- **Vérifier** : coter deux points distants de 1000 ; le texte doit afficher la
  valeur réelle dans l'unité du document. Puis `text_override="1,00 m"`.

### W-34 — `InsertBlock` et les attributs
- **Où** : `acad_com.py:1613`, attributs `1225` `_fill_attributes`
- **Hypothèse** : `InsertBlock(point, nom, sx, sy, sz, rotation)` accepte aussi
  bien un nom de bloc défini dans le dessin qu'un chemin DWG ; `HasAttributes`
  précède utilement `GetAttributes()`, qui rend un tuple d'objets portant
  `TagString` et `TextString` ; la comparaison des étiquettes se fait en
  majuscules.
- **Vérifier** : créer un bloc avec deux attributs dans le dessin d'essai,
  l'insérer avec ses valeurs, contrôler à l'écran. Puis insérer un nom de bloc
  **inexistant** : attendu, un `OperationFailed` explicite, jamais un succès.
- **Si faux** : `GetAttributes()` peut lever au lieu de rendre un tuple vide sur
  un bloc sans attribut — c'est déjà capturé et rapporté comme problème de style.

---

## Phase 4 — Style

### W-35 — Affectation de calque et de couleur par entité
- **Où** : `acad_com.py:1891` `_apply_style`
- **Hypothèse** : affecter `entity.Layer` et `entity.Color` après création est
  fiable. Choix délibéré : ne **pas** manipuler `doc.ActiveLayer` comme le
  faisait le code historique (`server.py:125`), qui laissait le calque courant
  de l'utilisateur modifié en cas d'erreur.
- **Vérifier** : lot mêlant trois calques, contrôler que le calque courant
  d'AutoCAD n'a **pas bougé** après exécution.

### W-36 — La couleur est toujours posée explicitement
- **Où** : `acad_com.py:1283`
- **Hypothèse** : `entity.Color = 256` vaut ByLayer et est accepté. Le code
  l'affecte systématiquement, y compris pour la valeur par défaut, afin que le
  résultat ne dépende pas de la variable `CECOLOR` laissée par l'utilisateur.
- **Vérifier** : régler `CECOLOR` sur rouge dans AutoCAD, puis dessiner avec le
  style par défaut. L'entité doit être **ByLayer**, pas rouge.

### W-37 — `Lineweight` n'accepte que l'énumération
- **Où** : `acad_com.py:1294`
- **Hypothèse** : `AcLineWeight` est une liste **fermée** de centièmes de
  millimètre (0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90,
  100, 106, 120, 140, 158, 200, 211), plus −1 ByLayer, −2 ByBlock, −3 Default.
  Une valeur hors liste est refusée.
- **Vérifier** : `lineweight=25` passe ; `lineweight=26` doit produire une
  entrée dans `failures` avec `stage: "style"`, **l'entité restant créée**.
- **Si faux** : si 26 est accepté et arrondi silencieusement, supprimer le
  message d'aide qui évoque l'énumération.

### W-38 — Chargement des types de ligne
- **Où** : `acad_com.py:1929` `_apply_linetype`, chargement ligne 1315
- **Hypothèse** : `Linetypes.Load(nom, fichier)` charge depuis `acad.lin`
  (impérial) ou `acadiso.lin` (métrique) ; le code tente les deux dans cet
  ordre ; recharger un type déjà chargé lève, d'où le cache préalable.
- **Vérifier** : `Style(linetype="DASHED")` sur un dessin où il n'est pas
  chargé. Le trait doit devenir pointillé.
- **Si faux** : sur une installation localisée, les noms diffèrent
  (`TIRETS` en français). Un nom introuvable produit un problème de style
  explicite, pas un trait plein silencieux.

### W-39 — `Layer.Description`
- **Où** : `acad_com.py:1403`
- **Hypothèse** : propriété absente des versions anciennes d'AutoCAD, purement
  documentaire, son échec ne doit pas invalider le calque. Reprend le constat
  du code historique (`server.py:110`).
- **Vérifier** : `EnsureLayer(name="ESSAI", description="mon calque")` puis
  contrôler la colonne Description du gestionnaire de calques.

### W-65 — Les calques sont rapportés à part de `created`
- **Où** : `acad_com.py:862` et `acad_com.py:928`, champ `BatchResult.layers`
- **Hypothèse** : choix d'architecture imposé par le contrat — un calque n'est
  pas une entité de l'espace objet. Le compter dans `created` gonflerait le
  nombre d'objets dessinés et, surtout, ferait **supprimer des calques** à
  l'annulation, puisque `undo()` efface tout ce que porte `handles`.
  Sont rapportés dans `layers` : les calques nommés par un `EnsureLayer`, créés
  ou simplement confirmés, et ceux créés à la volée pour accueillir une entité
  dont le style désigne un calque absent.
- **Vérifier** : un lot `EnsureLayer(name="MURS")` + `AddLine(style=Style(layer="PORTES"))`
  sur un dessin neuf. Attendu : `created_count == 1`, un seul handle, et
  `layers == ["MURS", "PORTES"]`.
- **Si faux** : un `created_count` à 2 ou 3 signale une régression du contrat.

### W-40 — Un calque existant n'est jamais réécrit
- **Où** : `acad_com.py:1977` `_ensure_layer`
- **Hypothèse** : choix d'architecture — un calque déjà présent porte des
  réglages voulus par l'utilisateur.
- **Vérifier** : créer `MURS` en rouge à la main dans AutoCAD, puis exécuter
  `EnsureLayer(name="MURS", color=3)`. Le calque doit **rester rouge**.

---

## Phase 5 — Sélection, lecture, suppression

### W-41 — Cycle de vie des jeux de sélection
- **Où** : `acad_com.py:2922` `_fresh_selection_set`
- **Hypothèse** : les jeux de sélection **persistent dans le document** ;
  réutiliser le même nom sans supprimer l'ancien lève « déjà existant » au
  deuxième appel. D'où : supprimer, puis ajouter. Le nom est tronqué à 31
  caractères et les espaces remplacés.
- **Vérifier** : appeler `count(EntityFilter(layer="MURS"))` **trois fois de
  suite**. Les trois doivent réussir et rendre le même nombre. Puis contrôler
  qu'aucun jeu `MCP_FILTER` ne subsiste dans le document.

### W-42 — Omission de `Point1`/`Point2` en mode « tout »
- **Où** : `acad_com.py:2857` `_run_select`, cascade lignes 1936-1949
- **Hypothèse** : en `acSelectionSetAll`, les deux points doivent être omis.
  La bonne façon d'omettre un paramètre optionnel en liaison tardive pywin32
  n'est pas certaine, d'où une cascade de trois formes : `pythoncom.Missing`,
  puis `None`, puis les arguments nommés. La première qui passe est mémorisée.
- **Vérifier** : après un premier filtre réussi,
  `backend.diagnostics()["select_strategy"]`. **Noter la valeur ici** ; elle
  permettra de supprimer la cascade et de garder la seule forme utile.
- **Si faux** : si les trois échouent, l'erreur d'origine est relevée telle
  quelle. Piste : construire la sélection par
  `ss.Select(acSelectionSetWindow, trèsGrandRectangle, …)`.

### W-43 — Filtrage côté AutoCAD par codes DXF
- **Où** : `acad_com.py:2826` `_select`
- **Hypothèse** : groupe 0 pour le type, 8 pour le calque, 62 pour la couleur,
  combinés par ET implicite sans opérateur logique. Remplace les boucles Python
  sur tout le ModelSpace du code historique (`server.py:1567`).
- **Vérifier** : un dessin à 5 cercles sur `MURS` et 5 lignes sur `PORTES`, puis
  `count(EntityFilter(kind="circle", layer="MURS"))`. Attendu : `5`.
- **Si faux** : sans opérateur, AutoCAD combine par ET ; si le résultat
  ressemble à un OU, insérer les codes `-4` `"<AND"` / `"AND>"`.

### W-44 — Le groupe 0 veut le nom **DXF**
- **Où** : `acad_com.py:2817` `_dxf_type`, table `acad_com.py:205-220`
- **Hypothèse** : le filtre attend `"LWPOLYLINE"`, pas `"AcDbPolyline"` ;
  `"INSERT"` pour un bloc ; `"DIMENSION"` pour **toutes** les cotations, ce qui
  rend `kind="dim_aligned"` non discriminant entre types de cotes.
- **Vérifier** : une polyligne et un bloc dans le dessin, puis
  `count(EntityFilter(kind="polyline"))` et `count(EntityFilter(kind="block_ref"))`.
  Attendu : `1` chacun. Un `0` désigne cette table.

### W-45 — La couleur filtrée est la couleur **propre**
- **Où** : `acad_com.py:1890`
- **Hypothèse** : le groupe 62 voit la couleur de l'entité, pas celle héritée du
  calque. Une entité ByLayer porte 256.
- **Vérifier** : entité ByLayer sur un calque rouge, puis
  `count(EntityFilter(color=1))`. Attendu : `0`, et `count(EntityFilter(color=256))`
  doit la trouver. C'est contre-intuitif : à documenter côté outils.

### W-46 — `HandleToObject` est en temps constant
- **Où** : `acad_com.py:2745` `_by_handle`, `1798` et `1816`
- **Hypothèse** : la méthode interroge directement la table des handles. Le code
  historique parcourait tout le ModelSpace (`server.py:635`), soit un appel
  interprocessus par entité.
- **Vérifier** : sur un dessin de **plusieurs milliers** d'entités, chronométrer
  `set_color(handle_de_la_première, 1)`. Doit être en millisecondes, et **ne pas
  croître** avec la taille du dessin.
- **Si faux** : un handle inconnu lève `EntityNotFound`, ce qui est le
  comportement attendu et se vérifie avec `backend.set_color("ZZZZ", 1)`.

### W-47 — `GetBoundingBox` en paramètres de sortie
- **Où** : `acad_com.py:1686`, aussi `1850`
- **Hypothèse** : pywin32 transforme les deux paramètres `[out]` en un couple de
  tuples de trois flottants, d'où `min_point, max_point = entity.GetBoundingBox()`.
- **Vérifier** : `query(limit=1)[0].bbox` sur un cercle de rayon 50 centré en
  (0,0) doit rendre `(-50, -50, 50, 50)`.
- **Si faux** : `bbox` vaut `None` et un message de débogage le signale — dégradé
  propre, jamais une exception. Certaines entités (texte non régénéré) n'ont
  légitimement pas de boîte englobante.

### W-48 — `EXTMIN`/`EXTMAX` et le dessin vide
- **Où** : `acad_com.py:2441` `_extents_on_worker`, sentinelle `acad_com.py:226`
- **Hypothèse** : ces variables ne sont rafraîchies **qu'à la régénération** ; un
  dessin vide porte des sentinelles de l'ordre de 1e20, détectées par le seuil
  `1e19`, et `extents()` rend alors `None`.
- **Vérifier** : dessin neuf ⇒ `extents() is None`. Puis un cercle de rayon 50
  en (0,0) ⇒ environ `(-50, -50, 50, 50)`. Enfin, **supprimer** ce cercle et
  rappeler `extents()` : la valeur reste probablement l'ancienne, tant qu'aucun
  `ZOOM Étendue` n'a eu lieu. Confirmer cette limite et la documenter.

---

## Phase 6 — Annulation

### W-49 — `StartUndoMark` ne prend aucun argument
- **Où** : `acad_com.py:830-834`
- **Fait vérifié en documentation** : `AcadDocument.StartUndoMark` n'a pas de
  paramètre ; le libellé du lot **ne peut pas** lui être transmis. Il est
  conservé côté backend, dans `BatchResult.label` et dans la pile d'annulation.
- **Vérifier** : dessiner un lot de 10 entités, puis **un seul Ctrl+Z** dans
  AutoCAD. Les 10 doivent disparaître ensemble. Si elles partent une par une,
  la marque n'a pas fonctionné.

### W-50 — `EndUndoMark` est toujours refermée
- **Où** : `acad_com.py:843`, dans un `finally`
- **Hypothèse** : une marque laissée ouverte fausse toutes les annulations
  suivantes ; l'échec de fermeture est donc remonté dans `failures`.
- **Vérifier** : provoquer l'échec d'une opération au milieu d'un lot (par
  exemple un bloc inexistant), puis Ctrl+Z. Le lot partiel doit s'annuler d'un
  seul geste.

### W-51 — `undo()` supprime par handle, sans `SendCommand`
- **Où** : `acad_com.py:2126` `undo`, boucle `1542`
- **Fait vérifié en documentation** : l'API ActiveX **n'expose aucune méthode
  `Undo`** sur le document. Le code historique envoyait `SendCommand("._UNDO _1 ")`
  (`server.py:1656`), traité de façon asynchrone, sans garantie, et rendait
  « annulation envoyée » sans savoir si quoi que ce soit avait été annulé.
  Le backend supprime donc par handle, en ordre inverse, dans sa propre marque.
- **Vérifier** : `execute` de 5 entités, puis `undo()`. Les 5 disparaissent, et
  `count()` revient à sa valeur d'avant.
- **Limite à confirmer** : `undo()` ne couvre **que les créations**. Un
  `set_color` n'est pas défait. À confirmer comme acceptable par la couche outils.

### W-52 — Entité déjà disparue au moment de l'annulation
- **Où** : `acad_com.py:1544-1547`
- **Hypothèse** : si l'utilisateur a supprimé une entité entre-temps,
  `HandleToObject` lève et le handle est simplement compté comme « déjà absent »,
  sans faire échouer l'annulation.
- **Vérifier** : `execute` de 3 entités, en supprimer une **à la main** dans
  AutoCAD, puis `undo()`. Attendu : les deux autres partent, aucune exception,
  et le journal indique « 2 supprimée(s), 1 déjà absente(s) ».

### W-53 — Calque verrouillé
- **Où** : `acad_com.py:2035` `delete`, `1552` pour l'annulation
- **Hypothèse** : `Delete()` échoue sur une entité posée sur un calque verrouillé
  ou gelé, et cet échec est rapporté, jamais avalé.
- **Vérifier** : dessiner sur `MURS`, **verrouiller** `MURS` dans AutoCAD, puis
  `delete(EntityFilter(layer="MURS"))`. Attendu : `OperationFailed` détaillant
  les handles en échec — surtout pas un succès silencieux.

### W-54 — Refus de supprimer sans filtre
- **Où** : `acad_com.py:1415-1419`
- **Hypothèse** : choix d'architecture — un `EntityFilter` vide viserait tout le
  dessin, ce qui doit rester une décision explicite de l'utilisateur. Lève
  `ConfirmationRequired`. **Déjà vérifié sur Mac** : ce chemin ne touche pas COM.
- **À confirmer** : que le backend `ezdxf` adopte la même règle, sous peine de
  faire diverger les deux moteurs sur les tests de contrat communs.

---

## Phase 7 — Performance et régénération

### W-55 — Un seul `Regen` par lot
- **Où** : `acad_com.py:2950` `_regen`, appelé depuis `_execute_on_worker:865`
- **Hypothèse** : le regen est l'appel le plus coûteux de l'API. Le code
  historique en déclenchait un **par entité** (`server.py:1198`, `1223`, `1241`,
  `1251`…), doublé d'un `time.sleep(1)` (`server.py:71`), soit plus de trois
  minutes pour un plan de 200 traits.
- **Vérifier** : chronométrer un lot de **200 lignes**. Objectif : quelques
  secondes. Comparer si possible avec l'ancien serveur sur le même lot, c'est la
  mesure qui justifie toute la refonte.
- **Si faux** : si les entités n'apparaissent pas avant un zoom manuel, tester
  `acAllViewports` (2) au lieu de `acActiveViewport` (3), cf. W-10.

### W-56 — Reprise sur AutoCAD occupé
- **Où** : `acad_com.py:768` `_submit_read`
- **Hypothèse** : `RPC_E_CALL_REJECTED` (0x80010001) et
  `RPC_E_SERVERCALL_RETRYLATER` (0x8001010A) sont les HRESULT renvoyés quand
  l'utilisateur est au milieu d'une commande. La reprise est réservée aux
  **lectures**, idempotentes : rejouer une écriture créerait des doublons.
- **Vérifier** : lancer `LIGNE` dans AutoCAD et **laisser la commande en cours**,
  puis appeler `count()` depuis Python. Attendu : la lecture aboutit après
  quelques reprises, ou rend un `OperationFailed` mentionnant « AutoCAD est
  occupé ». Puis tenter un `execute()` dans la même situation : l'échec doit être
  immédiat et explicite, sans reprise.

### W-57 — Délai d'appel et boîtes de dialogue modales
- **Où** : `acad_com.py:433` `call_timeout=300.0`, garde `acad_com.py:347`
- **Hypothèse** : une boîte de dialogue modale ouverte dans AutoCAD bloque
  l'appel COM indéfiniment ; le délai de 5 minutes évite le gel définitif du
  serveur MCP.
- **Vérifier** : ouvrir une boîte de dialogue modale (par exemple `OPTIONS`)
  puis lancer un `execute()`. Attendu : `OperationFailed` mentionnant la boîte
  de dialogue modale, et **le serveur reste réactif ensuite**.
- **À régler** : 300 s est probablement trop long pour un usage conversationnel.
  Choisir la valeur définitive après cet essai.

### W-58 — L'API synchrone ne bloque pas la boucle asyncio
- **Où** : `acad_com.py:38-45` — contrat d'usage documenté
- **Hypothèse** : la couche MCP enveloppe chaque appel dans `asyncio.to_thread`.
  C'est précisément ce que l'ancien serveur ne faisait pas : ses `time.sleep(1)`
  synchrones gelaient la boucle d'événements et donc tout le serveur.
- **Vérifier** : pendant un lot long, le serveur MCP doit continuer à répondre.
- **Si faux** : c'est un défaut de la couche appelante, pas de ce fichier, mais
  il annulerait tout le bénéfice du thread STA.

---

## Phase 8 — Divers

### W-59 — `$INSUNITS` et les dessins sans unité
- **Où** : `acad_com.py:934` `unit`, `1596`
- **Hypothèse** : `GetVariable("INSUNITS")` rend un entier ; 4 = mm, 5 = cm,
  6 = m, 1 = pouce, 2 = pied. La valeur 0, « sans unité », est **fréquente** et
  fait retomber sur `default_unit` (millimètre par défaut).
- **Vérifier** : sur un gabarit `acadiso.dwt`, `backend.unit` doit rendre
  `Unit.MILLIMETER` ; la valeur brute reste lisible dans
  `document_info()["insunits"]`.
- **Décision à prendre** : le repli sur le millimètre est arbitraire. Confirmer
  qu'il convient, sachant que toutes les valeurs par défaut de `units.py` en
  dérivent — un mur de 0,20 m devient 200 unités de dessin.

### W-60 — `document_info` lit toutes ses propriétés
- **Où** : `acad_com.py:1594`
- **Hypothèse** : `Name`, `Path`, `FullName`, `Saved`, `ActiveLayer.Name`,
  `ModelSpace.Count`, `Layers.Item(i).Freeze` et `.Lock` sont tous lisibles.
  `Path` est vide sur un dessin jamais enregistré.
- **Vérifier** : `document_info()` sur un dessin **non enregistré**, puis sur un
  dessin enregistré. Aucune clé ne doit manquer.

### W-61 — `SaveAs` et les boîtes de dialogue
- **Où** : `acad_com.py:1751`
- **Hypothèse** : `doc.Save()` sans argument, `doc.SaveAs(chemin)` avec ; le type
  de fichier par défaut convient ; `FullName` rend ensuite le chemin résolu.
- **Vérifier** : `save(r"C:\temp\essai.dwg")`, puis `save()` sur un dessin jamais
  enregistré — attention, ce dernier cas peut **ouvrir une boîte de dialogue**
  et déclencher W-57.
- **Si faux** : forcer `FILEDIA = 0`, ou exiger un chemin explicite.

### W-62 — `ZoomExtents` vit sur l'application
- **Où** : `acad_com.py:1734`
- **Hypothèse** : `ZoomExtents` est une méthode de `AcadApplication`, pas du
  document. Repris de `server.py:1553`.
- **Vérifier** : `zoom_extents()` recadre bien la vue.

### W-63 — Aucun succès sans preuve
- **Où** : `acad_com.py:942-949` — le handle est lu sur l'entité réelle
- **Hypothèse** : c'est le bug historique du projet. `server.py:1205` fabriquait
  `"line_created"` ou `"line_main"` quand l'opération échouait, si bien que le
  modèle croyait avoir dessiné et ne pouvait pas se corriger.
- **Vérifier**, essai de non-régression : forcer un échec (bloc inexistant,
  motif de hachure invalide) et contrôler que `BatchResult.ok is False`, que
  `failures` est renseigné avec le HRESULT, et qu'**aucun handle inventé**
  n'apparaît dans `handles`.

### W-64 — Relâchement des pointeurs COM à la fermeture
- **Où** : `acad_com.py:888` `close`, `acad_com.py:903` `_release_on_worker`
- **Hypothèse** : les pointeurs doivent être relâchés **dans l'appartement qui
  les a obtenus**, donc sur le worker, avant son arrêt ; `close()` ne ferme
  jamais AutoCAD.
- **Vérifier** : `connect()` puis `close()` en boucle, dix fois. AutoCAD doit
  rester ouvert et réactif, sans fuite de processus ni accumulation de threads
  (`threading.enumerate()`).

---

## Phase 9 — Définitions de blocs, commandes natives, mesures

Trois capacités ajoutées après la première rédaction du fichier. Elles se
vérifient dans cet ordre : les blocs d'abord, parce que la bibliothèque entière
en dépend ; les commandes ensuite, parce qu'elles peuvent laisser AutoCAD bloqué
sur une invite ; les mesures en dernier, elles ne modifient rien.

Préparer un dessin neuf, puis :

```python
from autocad_mcp.backends.acad_com import AcadComBackend
from autocad_mcp.model.ops import OperationBatch
from autocad_mcp.ops import blocks
from autocad_mcp.units import Defaults, Unit

be = AcadComBackend(); be.connect()
d = Defaults(Unit.MILLIMETER)
res = be.execute(OperationBatch(tuple(blocks.define("door", d)), label="bloc"))
print(res.to_dict())
```

### W-66 — `Blocks.Add` rend un conteneur qui sait dessiner
- **Où** : `acad_com.py:1667`, aiguillage du contenu `acad_com.py:1624`
- **L'hypothèse la plus lourde du chantier des blocs.** `doc.Blocks.Add(point, nom)`
  rend un `AcadBlock`, et dans la hiérarchie ActiveX `AcadModelSpace` **dérive**
  de `AcadBlock` : le bloc expose donc exactement les mêmes méthodes de dessin
  — `AddLine`, `AddLightWeightPolyline`, `AddCircle`, `AddArc`, `AddText`,
  `InsertBlock`. Tout le contenu réemprunte sans distinction l'aiguillage de
  `_create`, celui de l'espace objet. Si cette hypothèse tombe, **rien** du
  chantier des blocs ne fonctionne.
- **Vérifier** : le lot ci-dessus doit rendre `ok: True`, `created_count: 0`,
  `blocks: ["MCP_DOOR"]`. Puis, dans AutoCAD, `INSERER` : `MCP_DOOR` doit
  figurer dans la liste des blocs, avec **seuil, battant et arc de débattement**,
  pas un symbole vide.
- **Si faux** : `com_error` sur le premier `AddLine` du contenu. Le repli est
  alors de dessiner la géométrie dans l'espace objet, de la sélectionner et
  d'appeler `AddBlockFromSelection`, ou `Blocks.Add` suivi de `CopyObjects` vers
  le bloc — deux chemins plus coûteux, à n'emprunter que contraint.
- **Contrôle de non-régression** : `created_count` doit valoir **0**. Un bloc
  n'est pas dessiné ; voir W-74.

### W-67 — `AddAttribute` et l'ordre de ses six arguments
- **Où** : `acad_com.py:1785`, méthode `acad_com.py:1736` `_add_attdef`
- **Hypothèse** : la signature est
  `AddAttribute(Height, Mode, Prompt, InsertionPoint, Tag, Value)`, dans cet
  ordre exact — la hauteur **avant** le mode, l'étiquette **avant** la valeur.
  `Mode` est un champ de bits `AcAttributeMode` : 0 normal, 1 invisible
  (`_FALLBACK_CONSTANTS`, `acad_com.py:150`).
- **Vérifier** : après le lot ci-dessus, `MODIFBLOC MCP_DOOR` dans AutoCAD.
  L'attribut doit porter l'**étiquette** `REPERE`, l'**invite** « Porte simple
  avec battant » et la **valeur par défaut** `P`. Si étiquette et valeur sont
  interverties, l'ordre des deux derniers arguments est faux ; si la hauteur du
  texte est aberrante, c'est celui des deux premiers.
- **Si faux** : réordonner l'appel. Une inversion étiquette/valeur est
  particulièrement sournoise : le bloc s'insère sans erreur et la nomenclature
  sort avec des colonnes nommées `P`.

### W-68 — `Blocks.Item` sur un nom inconnu lève, il ne rend pas `None`
- **Où** : `acad_com.py:1848` `_block_exists`
- **Hypothèse** : `doc.Blocks.Item("NOM_ABSENT")` lève une `com_error`. C'est la
  forme déjà utilisée pour `SelectionSets.Item` (W-41). Sonder un nom coûte un
  appel, énumérer la table en coûte un par bloc : un dessin de production en
  compte des centaines.
- **Vérifier** : `be._block_exists(doc, ctx, "NOM_ABSENT")` rend `False`, et
  `be._block_exists(doc, ctx, "MCP_DOOR")` rend `True` après W-66.
- **Si faux** : si `Item` rendait `None`, `_block_exists` répondrait `True` pour
  tout nom et **aucun bloc ne serait jamais défini**, en silence. Conséquence
  visible : `blocks` renseigné mais rien dans le gestionnaire de blocs. Tester
  alors la valeur de retour en plus de l'exception.

### W-69 — `AcadBlock.Comments` et la suppression d'une définition
- **Où** : description `acad_com.py:1624`, suppression `acad_com.py:1857`
  `_discard_block`
- **Deux hypothèses mineures mais réunies ici.**
  1. `AcadBlock.Comments` est bien la **description** du bloc, celle qu'affiche
     le gestionnaire de blocs, équivalent du code de groupe 4 du DXF. Purement
     documentaire : son refus est rapporté dans `failures` sous
     `stage: "style"` et n'invalide pas le bloc.
  2. `Blocks.Item(nom).Delete()` retire une définition de la table des blocs.
     Elle n'est appelée que sur un bloc créé par le lot en cours, donc jamais
     référencé par une occurrence — AutoCAD refuse de supprimer une définition
     encore utilisée.
- **Vérifier** : la description apparaît dans `INSERER` ; et pour la suppression,
  voir l'essai de W-75.
- **Si faux** : si `Comments` n'existe pas, le remplacer par une donnée étendue
  ou l'abandonner. Si `Delete` échoue, le backend le **dit** au lieu de le taire :
  il lève alors `OperationFailed` en annonçant qu'un bloc incomplet subsiste.

### W-70 — Les propriétés ActiveX qui portent les mesures
- **Où** : `acad_com.py:2332` `_measures`, table `acad_com.py:311`
- **Hypothèse** : ces cinq propriétés existent et sont en unités du dessin —
  `AcadLine.Length`, `AcadLWPolyline.Length` et `.Area`,
  `AcadCircle.Circumference` et `.Area`, `AcadArc.ArcLength`. Les deux premières
  sont les moins sûres : `Length` est documenté pour `AcadLine`, moins nettement
  pour la polyligne légère.
- **Vérifier** : dessiner un segment de 1000, un cercle de rayon 100, un arc de
  quart de cercle de rayon 100 et un rectangle 400×300 fermé, puis :
  ```python
  for e in be.query(limit=20):
      print(e.kind, {k: v for k, v in e.extra.items() if k in ("length", "area")})
  ```
  Attendu : segment `length=1000` ; cercle `length≈628.3`, `area≈31416` ;
  arc `length≈157.1` **et pas d'aire** ; rectangle `length=1400`, `area=120000`.
- **Si faux** : une propriété absente lève, l'exception est interceptée et la clé
  est **omise**. Conséquence visible : `measure` rapporte `missing` au lieu d'un
  total. C'est le comportement voulu — une mesure absente est honnête, une mesure
  fausse ne l'est pas — mais si `LWPolyline.Length` manque, ajouter le calcul
  par sommets, renflements compris, comme le fait `ezdxf_be._polyline_measures`.

### W-71 — Redresser un repère : `Rotation`, `UpsideDown`, `Backward`
- **Où** : `acad_com.py:1463` `_straighten`, appel `acad_com.py:1421`
- **Hypothèse** : `AcadAttributeReference` expose `Rotation`, `UpsideDown` et
  `Backward`, et **affecter `Rotation` ne déplace pas le point d'ancrage** : le
  texte tourne autour de lui. La direction d'extrusion n'est délibérément pas
  touchée, car en ActiveX les points sont documentés en coordonnées du dessin,
  là où le DXF brut les exprime dans le repère objet — ce qui oblige le backend
  `ezdxf` à une gymnastique que rien ici ne justifie a priori.
- **Vérifier**, l'essai décisif : insérer la même porte à **0, 90, 180 et 270
  degrés**. Les quatre repères doivent se lire **horizontalement**, chacun à sa
  place autour de son symbole. C'est exactement l'essai qui a validé le backend
  `ezdxf`, et il doit donner la même image ici.
- **Si faux** : si le repère saute de place, `Rotation` déplace l'ancrage —
  relire `TextAlignmentPoint` avant, le réécrire après. Si le repère reste
  incliné, l'attribut de l'occurrence n'est pas celui qu'on a modifié : vérifier
  que `GetAttributes()` rend bien des objets vivants et non des copies.

### W-72 — Parcourir le contenu d'un bloc pour y retrouver les attributs
- **Où** : `acad_com.py:1493` `_upright_tags`
- **Hypothèse** : le contenu d'un `AcadBlock` se parcourt par `Count` et
  `Item(i)`, et une définition d'attribut s'y reconnaît à son `ObjectName`
  valant exactement `AcDbAttributeDefinition`.
- **Vérifier** :
  ```python
  b = be._doc.Blocks.Item("MCP_DOOR")
  print([b.Item(i).ObjectName for i in range(b.Count)])
  ```
  La liste doit se terminer par `AcDbAttributeDefinition`.
- **Si faux** : le nom pourrait être `AcDbAttribute`. Corriger
  `_ATTDEF_OBJECT_NAME` (`acad_com.py:307`). Conséquence si on ne le fait pas :
  aucun repère n'est redressé, sauf ceux couverts par le repli de session.

### W-73 — Données étendues : `RegisterApplication`, `SetXData`, `GetXData`
- **Où** : écriture `acad_com.py:1578` `_mark_upright`, lecture
  `acad_com.py:1540` `_wants_upright`
- **Hypothèse, en trois morceaux.** `Document.RegisterApplication(nom)` déclare
  l'application ; `SetXData(codes, valeurs)` attend deux SAFEARRAY de même
  longueur dont le **premier couple est `(1001, nom_application)`** ;
  `GetXData(nom)` rend, en liaison tardive pywin32, le couple `(codes, valeurs)`
  de ses deux paramètres de sortie. Ce dernier point est le moins sûr : certaines
  versions exigent qu'on passe deux paramètres muets.
- **Pourquoi c'est là** : le format n'a aucun champ pour dire qu'un attribut doit
  rester horizontal. `ezdxf_be.py` inscrit la même marque sous le même nom
  d'application `AUTOCAD_MCP` et la même chaîne `KEEP_UPRIGHT`, afin qu'un dessin
  passe d'un moteur à l'autre sans perdre la consigne. **Les deux constantes
  doivent rester identiques des deux côtés** (`acad_com.py:294`,
  `ezdxf_be.py:155`).
- **Vérifier** : après W-66, dans AutoCAD, `XDLISTE` sur la définition
  d'attribut, ou :
  ```python
  b = be._doc.Blocks.Item("MCP_DOOR")
  a = b.Item(b.Count - 1)
  print(a.GetXData("AUTOCAD_MCP"))
  ```
  Attendu : deux tableaux, `(1000,)` et `("KEEP_UPRIGHT",)`.
- **Si faux** : l'écriture échoue → rapporté dans `failures`, le bloc reste
  valide, seuls les repères suivront la rotation. La lecture échoue → elle est
  interceptée et le **repli de session** prend le relais
  (`acad_com.py:1528` `_remember_upright`) : les blocs définis pendant la session
  restent redressés, ceux relus d'un fichier ne le sont plus. Essayer alors
  `GetXData(nom, None, None)` ou passer par `win32com.client.VARIANT` en
  paramètres par référence.

### W-74 — Une définition de bloc n'est pas une entité de l'espace objet
- **Où** : `acad_com.py:1004`, aiguillage `acad_com.py:1075`
- **Hypothèse** : choix d'architecture imposé par le contrat, pas une hypothèse
  COM. Une définition vit dans la table des blocs ; la compter dans `created`
  gonflerait le nombre d'objets dessinés et, surtout, la ferait **supprimer à
  l'annulation**, ce qui emporterait toutes les occurrences posées par d'autres
  lots. Elle se rapporte donc dans `BatchResult.blocks`, exactement comme un
  calque se rapporte dans `layers` (W-65).
- **Vérifier** : `blocks.insert("chair", (0, 0), d)` sur un dessin neuf.
  Attendu : `created_count == 1` — la seule occurrence —, `blocks == ["MCP_CHAIR"]`,
  `layers == ["FURNITURE"]`, et `backend.count() == 1`.
- **Si faux** : un `created_count` à 2 ou plus signale une régression du contrat,
  que la suite commune aux backends doit attraper.

### W-75 — Redéfinir un bloc existant : garantie de présence, jamais écrasement
- **Où** : `acad_com.py:1637` `_define_block`, décision alignée sur
  `ezdxf_be.py:_define_block`
- **Hypothèse** : décision d'architecture, alignée sur le backend `ezdxf` qui,
  lui, est vérifiable sur la machine de développement. Un nom déjà pris est
  **conservé tel quel** et le lot réussit. Deux raisons : un plan qui insère dix
  fois la même porte redéfinit dix fois le bloc, et redéfinir un bloc change
  silencieusement **toutes** les occurrences déjà posées, y compris celles
  dessinées à la main par l'utilisateur.
- **Vérifier** : définir `MCP_DOOR`, puis modifier le bloc à la main dans
  AutoCAD (`MODIFBLOC`, ajouter un cercle), puis relancer le même lot. Le cercle
  ajouté à la main doit **survivre**, et `blocks` contenir quand même `MCP_DOOR`.
- **Et le cas partiel** : forcer l'échec d'une opération du contenu — par
  exemple un `AddPolyline` à deux sommets déclaré fermé — et vérifier
  qu'**aucun bloc entamé ne subsiste** dans le gestionnaire de blocs, que
  `blocks` est vide et que `failures` porte la cause. Un bloc à moitié écrit
  s'insérerait sans erreur et produirait un symbole tronqué, ce qui ne se voit
  pas sur un plan.

---

## Phase 9 bis — La passerelle de commandes natives

**Avant tout essai de cette section : sauvegarder.** Une commande native agit
directement sur le document et `undo_last_batch` ne la défera pas.

### W-76 — `SendCommand` est asynchrone, et on ne sait donc pas
- **Où** : `acad_com.py:2587`, méthode publique `acad_com.py:2465` `run_command`
- **L'hypothèse la plus lourde de la passerelle.** `doc.SendCommand(texte)`
  dépose la chaîne dans la file de commandes d'AutoCAD et rend la main. Il ne
  rend **aucune valeur**, **aucun code d'erreur**, et rien ne dit que la commande
  a été exécutée, ni quand. Le backend ne prétend donc jamais qu'elle a abouti :
  `completed` vaut **toujours `None`**, et le compte-rendu ne porte que ce qui a
  pu être constaté — nombre d'entités avant et après, `CMDACTIVE` relu ensuite.
- **Vérifier**, l'essai qui tranche : tracer une ligne, puis
  ```python
  print(be.run_command("OFFSET", ["100", "@0,0", "@0,200", ""]))
  ```
  Comparer `entities_before` et `entities_after`. **Trois issues possibles**, et
  il faut savoir laquelle on a :
  1. `delta == 1` — AutoCAD a exécuté la commande **avant** de répondre à la
     lecture qui suit. L'appel est en pratique synchrone depuis un autre
     processus, et le compte-rendu est directement utile.
  2. `delta == 0` mais la ligne décalée apparaît à l'écran un instant plus tard —
     l'appel est bien asynchrone. Le compte-rendu est alors **structurellement
     incapable** de mesurer l'effet, et il faut le dire au modèle plutôt que
     d'ajouter une temporisation : consigner le constat ici, et envisager une
     confirmation par relecture explicite (`measure`) côté outil.
  3. Une `com_error` — voir W-77.
- **Noter le résultat dans le récapitulatif**, c'est lui qui décide si la
  passerelle est exploitable telle quelle.
- **Aucune marque d'annulation n'est posée** autour de l'envoi, et c'est
  délibéré : elle serait refermée avant que la commande ne s'exécute, donc elle
  ne couvrirait rien. AutoCAD groupe de lui-même chaque commande dans une étape
  d'annulation, si bien qu'un Ctrl+Z dans l'interface reste le bon geste.

### W-77 — Le texte transmis : préfixes `._` et espace finale
- **Où** : `acad_com.py:2602` `_run_command_on_worker`
- **Hypothèse** : le texte est **entièrement fabriqué par le backend**, jamais
  recopié de l'appelant. La forme est `"._NOM arg1 arg2 "`, où le point impose
  la commande intégrée même si elle a été redéfinie par `UNDEFINE`, le tiret bas
  impose le nom anglais quelle que soit la langue de l'installation, et
  **l'espace finale vaut ENTRÉE** et déclenche l'exécution.
- **Vérifier** : sur une installation **française**, `run_command("OFFSET", …)`
  doit bien lancer `DECALER`. Contrôler la ligne de commande d'AutoCAD : elle
  doit afficher la commande, pas « Commande inconnue ».
- **Si faux** : si l'espace finale ne suffit pas, terminer par `"\n"`. Si le
  préfixe `._` est rejeté, essayer `_` seul. Attention, la chaîne construite est
  visible dans le compte-rendu sous la clé `sent` : c'est elle qu'il faut
  recopier dans la ligne de commande pour comparer.

### W-78 — Une commande incomplète bloque AutoCAD, et `CMDACTIVE` le dit
- **Où** : `acad_com.py:2675` `_command_active`
- **Hypothèse** : `GetVariable("CMDACTIVE")` rend 0 quand aucune commande n'est
  en cours. Une valeur non nulle signifie qu'AutoCAD est **resté dans la
  commande** et attend une saisie qu'on ne lui a pas donnée. Le compte-rendu
  porte alors un `warning` explicite. `None` est lui-même un indice : la lecture
  a été refusée, ce qui arrive typiquement quand AutoCAD est occupé.
- **Vérifier**, essai volontairement fautif : `be.run_command("TRIM", [])`, sans
  aucun argument. Attendu : `command_active` valant 1, ou `None`. Regarder la
  ligne de commande d'AutoCAD : elle attend une sélection. **Appuyer sur ÉCHAP**
  avant de continuer la checklist, sinon tous les appels suivants seront rejetés
  et déclencheront W-56 puis W-57.
- **Si faux** : si `CMDACTIVE` rend toujours 0 alors qu'AutoCAD est visiblement
  bloqué, cette sonde ne sert à rien et il faut retirer la promesse : mieux vaut
  ne rien dire que dire faux. Envisager alors d'envoyer `"\x1b"` après chaque
  commande, au prix d'annuler aussi les commandes légitimement interactives.

### W-79 — La liste blanche du backend, seconde barrière
- **Où** : `acad_com.py:2529` `_validated_command`, liste `acad_com.py:266`
- **Ce n'est pas une hypothèse COM, c'est une règle de sécurité**, et elle a été
  **exécutée sur macOS** : transmettre une chaîne libre à AutoCAD revient à
  exécuter du code arbitraire, puisque son interpréteur accepte aussi bien une
  commande qu'une expression AutoLISP capable d'ouvrir un fichier ou de joindre
  le réseau. La couche outil filtre déjà contre `Config.allowed_commands` ; le
  backend refiltre, parce qu'un moteur qui fait confiance à son appelant n'est
  pas défendu, il est seulement défendu ailleurs.
- **À vérifier sous Windows** : uniquement que la barrière n'a pas été
  contournée par une évolution de la configuration.
  ```python
  from autocad_mcp.config import Config
  print(sorted(Config.from_env().allowed_commands))
  print(be.diagnostics()["allowed_commands"])
  ```
  Les deux listes doivent être **identiques**. Un écart signale que l'une a bougé
  sans l'autre.
- **Essai d'injection**, à refaire une fois sur la machine cible :
  `be.run_command('(command "_.erase" "all" "")')` doit lever `InvalidParameter`
  **sans rien envoyer à AutoCAD**, et le dessin doit être intact.

---

## Phase 10 — Parité des mesures avec le backend DXF

### W-80 — L'aire d'une polyligne ouverte n'est jamais publiée
- **Où** : `acad_com.py:2332` `_measures`
- **Hypothèse** : AutoCAD rend, pour une polyligne **ouverte**, l'aire du
  contour qu'on obtiendrait en la refermant. C'est une mesure de quelque chose
  qui n'est pas dessiné : la publier gonflerait tout total de surfaces. La clé
  `area` n'est donc posée que si `Closed` est vrai.
- **Vérifier** : une polyligne ouverte en L, trois sommets. `extra` doit porter
  `length` et `closed: false`, et **pas** `area`. Fermer la même polyligne dans
  AutoCAD, relancer `query` : `area` doit apparaître.
- **Si faux** : si `Area` levait sur une polyligne ouverte, tant mieux, la clé
  serait omise de toute façon.

### W-81 — Écart de mesure assumé avec le backend `ezdxf`
- **Où** : `acad_com.py:2319`, à comparer à `ezdxf_be.py:_polyline_measures`
- **Ce n'est pas une hypothèse, c'est une divergence connue à confirmer.** Sur
  une polyligne fermée **à renflements** — un contour à côtés arrondis —, le
  backend `ezdxf` **ne publie pas** d'aire : il refuse de retrancher ou d'ajouter
  les segments circulaires, calcul qu'il ne fait pas. AutoCAD, lui, connaît
  l'aire exacte et ce backend la publie.
- **Conséquence** : sur un tel dessin, `measure` rend un total de surfaces plus
  complet sous AutoCAD que sous DXF. L'écart va dans le sens du **plus** juste,
  c'est le backend `ezdxf` qui est incomplet, mais il faut le savoir avant de
  comparer deux relevés.
- **Vérifier** : tracer un rectangle, arrondir deux coins par `RACCORD`, puis
  comparer `measure` sous AutoCAD et sur le même dessin exporté en DXF.
- **Si l'écart gêne** : ce n'est pas ce backend qu'il faut brider, c'est
  `ezdxf_be` qu'il faut compléter.

### W-82 — Le coût des mesures en allers-retours
- **Où** : `acad_com.py:2303` `_describe`
- **Hypothèse** : publier les mesures coûte une à deux lectures de propriété par
  entité décrite, donc un à deux appels interprocessus de plus. `query` étant
  borné par `limit`, la dépense reste bornée.
- **Vérifier** : chronométrer `query(limit=200)` sur un dessin de 200 entités,
  avant et après. Un temps qui double est attendu ; un temps qui décuple ne
  l'est pas.
- **Si trop cher** : n'interroger les mesures que sur demande, par un paramètre
  de `query`, plutôt que de les retirer — mais ce serait un changement du
  contrat, à décider avec `ops/query.py`.

---

## Phase 11 — Maillages, la troisième dimension

Chantier ajouté après coup, pour combler un écart de contrat : le moteur DXF
sait exécuter `AddMesh` depuis son premier jour, ce backend ne le savait pas et
faisait échouer tout plan en volume. `AddPolyfaceMesh` a été retenue pour la
matérialiser — voir la justification complète dans la docstring de
`_add_mesh`. **Aucune des sept entrées qui suivent n'a pu être exercée devant
AutoCAD.**

Préparer un dessin neuf, puis un prisme simple, asymétrique pour qu'un décalage
d'indice se voie :

```python
from autocad_mcp.backends.acad_com import AcadComBackend
from autocad_mcp.model.ops import AddMesh, OperationBatch, Style
from autocad_mcp.ops.volume import extrude_ring

be = AcadComBackend(); be.connect()
mesh = extrude_ring([(0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)], 0.0, 3.0)
res = be.execute(OperationBatch((mesh,), label="maillage"))
print(res.to_dict())
```

Comparer systématiquement au rendu du backend `ezdxf` sur le **même** lot,
produit par la compétence `dxf-preview` : c'est la seule preuve indépendante
d'AutoCAD que le volume obtenu est le bon, silhouette et proportions comprises.

### W-83 — `AddPolyfaceMesh`, la route retenue face à `Add3DFace` et `AddMesh`
- **Où** : `acad_com.py:2986` `_add_mesh`
- **Décision d'architecture, pas seulement une hypothèse COM.** Trois routes
  ActiveX permettent de poser un volume : `AddPolyfaceMesh(VerticesList, FaceList)`
  correspond exactement à la forme du modèle (sommets + facettes par indices)
  et rend une **seule** entité ; `Add3DFace` pose une facette à la fois, donc
  un mur à trois tranches (allège, linteau, chaînage) produirait des dizaines
  d'entités indépendantes pour un seul volume ; `AddMesh` au sens ActiveX
  (`Add3DMesh`, grille M x N) décrit une nappe régulière, pas un prisme.
  **Hypothèse non vérifiée ici** : `AddPolyfaceMesh` existe bien sur
  `AcadModelSpace` et, comme le reste des méthodes de dessin, sur l'`AcadBlock`
  rendu par `Blocks.Add` — même hypothèse que W-66, à laquelle celle-ci
  s'ajoute plutôt qu'elle ne s'y substitue.
- **Vérifier** : le lot ci-dessus doit rendre `ok: True`, **une seule** entité
  créée (`created_count == 1`, pas six, une par facette). Dans AutoCAD,
  cliquer une fois sur le prisme : il doit se sélectionner en entier. `LISTE`
  doit annoncer un maillage de faces polygonales (« maillage 3D » ou
  « polyface mesh » selon la version et la langue).
- **Si faux** : `com_error` sur l'appel — voir le message renvoyé, il cite les
  contraintes d'`AddPolyfaceMesh` (au moins quatre sommets, au moins une
  facette). Si la méthode est absente de l'objet, replier sur `Add3DFace`,
  un appel par groupe de quatre indices déjà produit par
  `_polyface_indices`, en acceptant une entité par facette et en rapportant
  tous les handles obtenus.

### W-84 — Base un contre base zéro : le piège principal, silencieux
- **Où** : `acad_com.py:3056` `_polyface_indices`
- **L'hypothèse la plus lourde du chantier des maillages, à traiter en
  priorité.** Le modèle numérote ses sommets à partir de **zéro**
  (`vertices[0]` est le premier). La documentation ActiveX d'`AddPolyfaceMesh`
  numérote les siens à partir de **un**. Chaque indice de `op.faces` est donc
  incrémenté avant l'envoi. **Une hypothèse fausse ici ne fait lever aucune
  erreur à AutoCAD** : `FaceList` reste un tableau d'entiers valide, seulement
  décalé d'un cran, et chaque facette pointe sur le sommet voisin de celui
  voulu. Le volume obtenu serait déformé, pas absent — exactement le genre de
  défaut silencieux que ce projet s'interdit ailleurs par construction
  (`BatchResult.ok` resterait `True`), mais que cette hypothèse précise peut
  réintroduire si elle est fausse.
- **Vérifier**, l'essai décisif : le prisme asymétrique ci-dessus (base
  2 x 1, hauteur 3, dont aucune face n'est carrée). Comparer, sommet par
  sommet, avec le rendu PNG du **même lot** produit par le backend `ezdxf`
  (compétence `dxf-preview`). Les deux volumes doivent être identiques —
  même largeur dans le même sens, même hauteur, aucune face vrillée. Puis,
  dans AutoCAD, `LISTE` sur le maillage et contrôler que les coordonnées des
  sommets de chaque face correspondent aux quatre coins attendus de cette
  face, pas à ceux de la face voisine.
- **Si faux** : un volume qui apparaît « tordu », dont une face traverse les
  autres, ou dont la première et la dernière facette semblent avoir échangé un
  sommet, signale un décalage d'indice. Retirer le `+ 1` de
  `_polyface_indices` si le modèle s'avère déjà en base un, ce qui serait
  surprenant mais pas impossible sur une version ancienne d'ActiveX.

### W-85 — Groupes de quatre stricts, et le repli triangle / éventail
- **Où** : `acad_com.py:3094` `_polyface_indices`
- **Hypothèse** : la documentation ActiveX impose que `FaceList` soit un
  multiple de quatre — une facette occupe toujours quatre positions. Une
  facette triangulaire du modèle est donc fermée en répétant son dernier
  sommet, convention du format DXF POLYFACE MESH sous-jacent. Une facette à
  plus de quatre sommets — possible depuis `ops.volume.slab`, dont le contour
  est arbitraire, jamais depuis `wall_volume` ou `box`, dont les panneaux sont
  toujours des quadrilatères — est découpée en éventail depuis son premier
  sommet, chaque triangle obtenu étant refermé par la règle précédente.
- **Vérifier** : un prisme triangulaire (`extrude_ring` sur un contour à trois
  points) doit produire un maillage fermé sans trou visible aux deux
  capuchons. Puis un lot construit avec `slab()` sur un contour à cinq
  sommets (une dalle en L, par exemple) : les deux capuchons doivent rester
  pleins, sans facette manquante ni croisée.
- **Si faux** : si `AddPolyfaceMesh` refuse un tableau dont la taille n'est
  pas un multiple de quatre, l'erreur est immédiate et explicite — ce n'est
  pas un défaut silencieux, contrairement à W-84. **Limite assumée, non
  testable depuis une machine sans AutoCAD** : un contour non convexe
  produirait un éventail qui sort de la facette. Aucun contour construit par
  `ops/volume.py` n'est aujourd'hui non convexe (des rectangles), donc le cas
  ne s'est pas présenté à l'écriture ; un contour en L ou en U passé à
  `slab()` y expose potentiellement ce module, et resterait à traiter en
  amont, dans la couche modèle, par une triangulation qui ne suppose pas la
  convexité.

### W-86 — Le type VARIANT de `FaceList` : entier court, pas long
- **Où** : `acad_com.py:3028` `_add_mesh`, constante `acad_com.py:328`
- **Hypothèse, appuyée sur un exemple officiel plutôt que devinée.** La
  documentation Autodesk d'`AddPolyfaceMesh` dit seulement « FaceList: Variant
  (array of integers) », ambigu entre `VT_I2` et `VT_I4`. Mais l'exemple VBA
  publié par Autodesk déclare `Dim FaceList(0 To 7) As Integer` — `Integer`
  est le type **16 bits** de VBA, ce qui correspond à `VT_ARRAY | VT_I2`,
  déjà la recette retenue par `_shorts` ailleurs dans ce fichier pour « un
  tableau d'entiers ». C'est donc `_shorts`, pas `_variants` ni un nouveau
  tableau `VT_I4`, qui sert ici.
  **Défensif, pas une hypothèse COM** : `_POLYFACE_MAX_INDEX` (32767) borne le
  plus grand indice acceptable avant même l'appel, pour lever une erreur
  nommée plutôt que de découvrir un débordement silencieux sur un dessin réel.
- **Vérifier** : le prisme de W-84 se dessine sans `com_error` immédiate liée
  au type de `FaceList`. Puis, pour la borne, un maillage dont le nombre de
  sommets dépasse 32767 (un maillage généré, pas dessiné à la main) doit lever
  `InvalidGeometry` **avant** tout appel COM, jamais un `com_error` opaque.
- **Si faux** : un `com_error` immédiat sur `AddPolyfaceMesh` avec un message
  évoquant un type incompatible signale que `VT_I4` était attendu. Remplacer
  l'appel à `self._shorts(face_indices)` par un nouveau tableau
  `VT_ARRAY | VT_I4` dans `_add_mesh`, et desserrer `_POLYFACE_MAX_INDEX` à la
  borne d'un entier 32 bits.

### W-87 — L'`ObjectName` d'un maillage : `AcDbPolyFaceMesh`
- **Où** : table `acad_com.py:222`, lue par `_describe` `acad_com.py:2305`
- **Hypothèse** : le nom ObjectARX d'un objet rendu par `AddPolyfaceMesh` est
  exactement `AcDbPolyFaceMesh` — Face et Mesh avec une majuscule, comme
  l'intitulé de la référence Autodesk « AcDbPolyFaceMesh Methods ». Une
  différence de casse ou d'orthographe ferait retomber `_describe` sur le nom
  brut au lieu de `"mesh"` : dégradé propre, pas un plantage, mais un filtrage
  par `kind="mesh"` qui ne trouverait jamais rien par cette voie.
- **Vérifier** : après W-83, `backend.query(limit=1)[0].kind`. Attendu :
  `"mesh"`, pas `"AcDbPolyFaceMesh"`.
- **Si faux** : corriger la clé dans `_OBJECT_NAME_TO_KIND` avec le nom
  effectivement lu.

### W-88 — Le filtre par `kind="mesh"` recouvre aussi les polylignes historiques
- **Où** : table `acad_com.py:243`
- **Ce n'est pas une hypothèse, c'est une ambiguïté du format elle-même,
  documentée, à confirmer plutôt qu'à deviner.** Un maillage polyface hérite
  en DXF du type `POLYLINE` (groupe 0), le même que les polylignes 2D et 3D
  historiques (`AcDb2dPolyline`, `AcDb3dPolyline`) — la distinction se fait
  par un indicateur du groupe 70, qu'un filtre de sélection par code de
  groupe 0 ne peut pas exprimer sans un second test (`-4 "&"` sur le groupe
  70), que `_select` ne construit pas aujourd'hui. Ce backend ne crée jamais
  de `AcDb2dPolyline`/`AcDb3dPolyline` lui-même — `AddPolyline` du modèle
  produit toujours une `AcDbPolyline` légère, un type DXF différent
  (`LWPOLYLINE`) — donc le cas ne se présente pas sur un dessin **entièrement
  produit par ce projet**. Il se présente en revanche sur un dessin existant,
  dessiné à la main ou importé, qui contiendrait de telles polylignes
  historiques.
  **Ce que ça ne change pas** : la lecture par handle (`query(handles=...)`,
  `_matches_residual`) reste précise, elle passe par `ObjectName` (W-87), pas
  par ce filtre.
- **Vérifier** : sur un dessin neuf ne contenant que des entités de ce projet,
  `count(EntityFilter(kind="mesh"))` doit correspondre exactement au nombre de
  maillages créés. Puis, essai qui matérialise la limite : dessiner à la main
  une polyligne 3D classique (`_3DPOLY` dans AutoCAD, pas `LIGNEP`), puis
  relancer le même comptage : si le total augmente d'un, la limite décrite
  ici est réelle et touche ce dessin.
- **Si ça gêne** : ajouter le test `-4`/`"&"` sur le groupe 70 dans `_select`
  et `_dxf_type`, réservé au cas `kind == "mesh"` — changement plus
  invasif, à ne faire qu'une fois la gêne confirmée en pratique.

### W-89 — `NumberOfVertices` et `NumberOfFaces`, comptes publiés par `_measures`
- **Où** : `acad_com.py:2396` `_measures`
- **Hypothèse** : `AcadPolyfaceMesh` expose `NumberOfVertices` (propriété
  documentée par Autodesk) et `NumberOfFaces` (non trouvée dans la
  documentation consultée, supposée par symétrie). Publiées sous les clés
  `vertices` et `faces` de `extra`, **volontairement en dehors** de
  `base.MEASURE_KEYS` : un volume n'a ni longueur ni aire au sens de ce
  dictionnaire, et rien ne doit les additionner par erreur dans une
  nomenclature de surfaces.
- **Vérifier** : sur le prisme de W-84 (6 sommets, 6 facettes après capuchons),
  `query(limit=1)[0].extra` doit porter `vertices: 6` et `faces: 6`.
- **Si faux** : si `NumberOfFaces` n'existe pas, l'échec est intercepté par
  `_number_property` et la clé `faces` est simplement omise — pas de
  plantage, juste une information en moins.

---

## Récapitulatif à remplir pendant la session

| Point | Attendu | Constaté | Corrigé |
|---|---|---|---|
| W-10 `acActiveViewport` | 3 | | |
| W-11 `acSelectionSetAll` | 5 | | |
| W-12 `acSelectionSetWindow` | 0 | | |
| W-21 sens de rotation de l'arc | direct | | |
| W-27 `TextAlignmentPoint` | pris en compte | | |
| W-28 `MText.Height` | hauteur de caractère | | |
| W-30 `hatch_loop_strategy` | `variant` | | |
| W-42 `select_strategy` | inconnue | | |
| W-51 `undo()` par handle | complet | | |
| W-55 200 lignes | quelques secondes | | |
| W-59 unité par défaut | mm | | |
| W-66 `Blocks.Add` sait dessiner | oui | | |
| W-67 ordre de `AddAttribute` | H, Mode, Invite, Pt, Tag, Val | | |
| W-68 `Blocks.Item` inconnu | lève | | |
| W-70 `LWPolyline.Length` | existe | | |
| W-71 repères à 0/90/180/270° | tous horizontaux | | |
| W-73 `GetXData` en liaison tardive | `(codes, valeurs)` | | |
| W-74 `created_count` d'un `DefineBlock` | 0 | | |
| W-76 `SendCommand` mesurable ? | **à trancher** | | |
| W-77 `._NOM ` sur AutoCAD français | lance la commande | | |
| W-78 `CMDACTIVE` après commande incomplète | 1 | | |
| W-79 listes blanches identiques | oui | | |
| W-83 `AddPolyfaceMesh` disponible | oui, une seule entité | | |
| W-84 base des indices de facette | un (base un) | | |
| W-86 type VARIANT de `FaceList` | `VT_I2` (`_shorts`) | | |
| W-87 `ObjectName` d'un maillage | `AcDbPolyFaceMesh` | | |
