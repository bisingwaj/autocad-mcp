# Ce qui reste à faire

Les six étapes du plan sont livrées. Ce document consigne ce qui reste ouvert,
par ordre d'importance.

## La validation Windows, seul vrai point ouvert

Le backend AutoCAD n'a **jamais été exécuté**. Il a été écrit sur macOS, où
AutoCAD n'existe pas. Sa correction repose sur trois appuis: la revue, la
symétrie avec le moteur DXF qui lui est testé, et une liste de confirmation.

Les quatre-vingt-deux entrées de `windows-checklist.md` sont à dérouler devant
AutoCAD, dans l'ordre des phases. Chacune porte son emplacement exact dans le
code, l'hypothèse formulée, le geste de vérification et la correction à
appliquer si l'hypothèse est fausse.

Les deux hypothèses les plus risquées y sont signalées:

**Ce que rend la création d'un bloc.** Tout le chantier des blocs suppose que
l'objet rendu sait dessiner comme l'espace objet. Si c'est faux, la
bibliothèque entière est à revoir.

**Ce que veut dire « asynchrone » pour le passage de commandes.** L'entrée
détaille les trois issues possibles de l'essai et ce que chacune implique.

Vient ensuite un piège plus sournois: les valeurs numériques des constantes de
sélection et de régénération, qu'Autodesk ne publie pas. Une constante fausse
rend un jeu de sélection **vide sans lever d'erreur**. La méthode de
diagnostic du backend compare les constantes résolues dans la bibliothèque de
types avec les valeurs de repli et valide les vingt-six d'un coup.

Un outil vérifie que la checklist ne pourrit pas: il contrôle que chaque
identifiant cité dans le code existe, que les identifiants sont uniques, et que
chaque référence de ligne désigne encore la bonne construction.

```bash
uv run python -m autocad_mcp.devtools.checklist_refs
```

## Défauts connus, mineurs

**Les jonctions en T laissent un trait.** Un refend qui bute contre un mur
porteur se superpose à lui proprement, mais la ligne de contact reste visible.
En dessin technique on la supprime par une fusion des contours. Les angles
d'une même enfilade, eux, sont correctement mitrés.

**Deux repères de blocs se superposent à leur symbole**, ceux de la chaise et
du WC. C'est une question de position d'étiquette dans la bibliothèque.

**La surface d'une hachure et celle d'une polyligne à arcs ne sont pas
mesurées.** Les deux exigeraient une approximation dont la dérive a été
mesurée. Une mesure absente est rapportée comme manquante, ce qui est honnête.

## Non livré

**Présentations et export PDF.** Rien n'existe côté espace papier. C'est la
seule partie de l'étape 5 qui n'a pas été traitée. Un plan se produit
aujourd'hui en espace objet et s'enregistre en DXF.

## Pistes pour la suite

**Cotation automatique.** Coter un plan à la main reste fastidieux. Un outil
qui cote une enfilade de murs d'un seul appel serait le prolongement naturel.

**Blocs dynamiques et xrefs.** Le format les gère, le projet ne les expose pas.

**Nomenclature mise en tableau.** Les quantités sont calculées mais rendues en
données. Les poser dans un tableau du dessin demanderait l'opération
correspondante.
