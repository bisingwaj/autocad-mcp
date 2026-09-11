"""Catalogue des outils MCP exposés au modèle.

Ce module ne contient que des **données**: il décrit ce que le modèle peut
demander, jamais comment l'exécuter. L'exécution est dans ``handlers``, le
câblage du protocole dans ``server``. Il n'importe donc ni ``mcp``, ni un
backend, ce qui le rend vérifiable par un simple test de structure.

Quatre règles gouvernent ce catalogue, et chacune répond à un défaut constaté
dans l'ancien serveur.

* **Peu d'outils, mais puissants.** L'ancien catalogue déclarait vingt-deux
  outils dont neuf variantes de suppression et une primitive par forme. Le
  modèle enchaînait vingt appels pour un plan, sans transaction commune. Ici
  ``draw`` et ``build_structure`` prennent une liste et exécutent un seul lot,
  ``delete_entities`` porte un filtre unifié.
* **Aucun nom en double.** L'ancien catalogue déclarait
  ``delete_entities_by_color`` deux fois: la seconde déclaration masquait la
  première en silence. L'unicité est vérifiée par un test, pas par relecture.
* **Chaque paramètre porte son unité et un exemple.** Une longueur sans unité
  est un bug: dans un dessin en millimètres, une épaisseur de ``0.1`` est
  invisible. Les longueurs sont dans l'unité du document, les angles en degrés.
* **``additionalProperties`` vaut ``false`` partout.** Un paramètre mal nommé
  doit être refusé bruyamment, pas ignoré.

**Convention d'angle.** Les schémas parlent en **degrés**, parce que c'est
l'unité qu'un humain et un modèle manipulent, et chaque nom de champ le dit
(``rotation_deg``, ``start_angle_deg``). La conversion en radians, qui est la
convention interne de ``model.ops``, se fait à l'entrée des handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..model.layers import ACI, STANDARD_LAYERS
from ..ops import blocks as block_library

__all__ = [
    "MAX_BATCH_ITEMS",
    "MAX_CHECK_ENTITIES",
    "MAX_CHECK_ITEMS",
    "MAX_MEASURE_ENTITIES",
    "MAX_PROBLEMS",
    "MAX_QUERY_LIMIT",
    "MAX_RENDER_PIXELS",
    "MEASURE_MODES",
    "MIN_RENDER_PIXELS",
    "RENDER_PROJECTIONS",
    "ToolSpec",
    "build_catalog",
    "tool_names",
]

#: Plafond dur du nombre d'entités décrites par ``query_entities``. Le défaut
#: vient de la configuration, mais aucun appel ne peut le dépasser: déverser
#: cinquante mille entités saturerait le contexte du modèle sans rien lui
#: apprendre.
MAX_QUERY_LIMIT = 1000

#: Plafond du nombre d'éléments d'un lot d'écriture. Au-delà, le modèle doit
#: découper, ce qui garde l'annulation exploitable et le rapport lisible.
MAX_BATCH_ITEMS = 500

#: Bornes de la taille d'image, en pixels. En dessous, un plan est illisible;
#: au-dessus, l'image coûte plus à transmettre qu'elle n'apprend.
MIN_RENDER_PIXELS = 200
MAX_RENDER_PIXELS = 4000

#: Nombre maximal d'entités réellement examinées par ``measure``. Au-delà, la
#: mesure porte sur un échantillon et le dit: un total silencieusement partiel
#: serait pire qu'absent, puisqu'il a l'air juste.
MAX_MEASURE_ENTITIES = 5000

#: Nombre maximal d'entités examinées par ``check_plan``. Plus bas que la
#: mesure, parce que la recherche de doublons compare les entités deux à deux:
#: le coût croît avec le carré du nombre examiné.
MAX_CHECK_ENTITIES = 2000

#: Nombre maximal de contours et d'axes soumis en une fois à ``check_plan``.
MAX_CHECK_ITEMS = 200

#: Nombre maximal de défauts décrits dans un rapport de validation. Le compte
#: total est rendu quoi qu'il arrive: c'est la liste qui est tronquée, pas le
#: constat.
MAX_PROBLEMS = 100

#: Modes de ``measure``. Un seul outil paramétré vaut mieux que cinq outils
#: étroits: le modèle apprend une signature et choisit ensuite sa question.
MEASURE_MODES = (
    "summary",
    "totals",
    "by_layer",
    "by_type",
    "quantities",
    "nearest",
    "in_window",
)

#: Projections de ``render_view``. "plan" est la vue de dessus déjà produite
#: depuis les débuts du projet; "iso" relit les volumes du document, ce qui
#: exige que build_structure en ait construit au moins un.
RENDER_PROJECTIONS = ("plan", "iso")

#: Noms des calques normalisés, cités dans les descriptions pour que le modèle
#: n'invente pas de nomenclature.
_STANDARD_LAYER_NAMES = sorted(spec.name for spec in STANDARD_LAYERS.values())

#: Types d'entités interrogeables. Ce sont les noms DXF, ceux que renvoie
#: ``query_entities`` dans le champ ``type``.
_ENTITY_TYPES = [
    "LINE",
    "LWPOLYLINE",
    "CIRCLE",
    "ARC",
    "TEXT",
    "MTEXT",
    "HATCH",
    "DIMENSION",
    "INSERT",
]

_UNIT_NOTE = (
    "Longueur exprimée dans l'unité du document, donnée par get_drawing_info.unit "
    "(m par défaut)."
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Déclaration d'un outil, indépendante du protocole.

    ``read_only``, ``destructive`` et ``idempotent`` alimentent les annotations
    MCP. Elles ne remplacent pas la description: un modèle lit la description,
    un client lit les annotations.
    """

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False


# ---------------------------------------------------------------------------
# Fragments de schéma réutilisés
# ---------------------------------------------------------------------------


def _point2(description: str, example: str = "[0, 0]") -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
        "description": f"{description} Couple [x, y] dans l'unité du document. Exemple: {example}.",
    }


def _points(description: str, minimum: int = 2) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": minimum,
        "items": _point2("Sommet."),
        "description": f"{description} Exemple: [[0, 0], [5, 0], [5, 4]].",
    }


def _color() -> dict[str, Any]:
    return {
        "type": ["string", "integer"],
        "minimum": 0,
        "maximum": 256,
        "description": (
            "Couleur: nom (" + ", ".join(sorted(ACI)) + ", ou les synonymes français "
            "rouge, vert, bleu, jaune, gris, blanc) ou index ACI entier de 0 à 256. "
            "Omise, l'entité suit son calque (ByLayer). Exemple: \"red\" ou 1. "
            "Attention: l'index 0 est ByBlock, pas noir; le noir du dessin technique "
            "est l'ACI 7, nommé \"white\"."
        ),
    }


def _layer(default: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "default": default,
        "description": (
            "Calque de destination, créé s'il n'existe pas. Calques normalisés: "
            + ", ".join(_STANDARD_LAYER_NAMES)
            + f". Défaut: \"{default}\". Exemple: \"WALLS\"."
        ),
    }


def _thickness(what: str) -> dict[str, Any]:
    return {
        "type": "number",
        "minimum": 0,
        "description": (
            f"Épaisseur {what}. {_UNIT_NOTE} Omise: l'épaisseur de maçonnerie courante, "
            "0,20 m convertie dans l'unité du document. Une épaisseur nulle produit un "
            "simple axe, ce qui reste utile pour esquisser. Exemple: 0.2."
        ),
    }


def _length(description: str, example: str) -> dict[str, Any]:
    return {
        "type": "number",
        "exclusiveMinimum": 0,
        "description": f"{description} {_UNIT_NOTE} Exemple: {example}.",
    }


def _angle_deg(description: str, example: str) -> dict[str, Any]:
    return {
        "type": "number",
        "description": (
            f"{description} En **degrés**, sens trigonométrique direct, zéro vers l'est. "
            f"Exemple: {example}."
        ),
    }


def _level(description: str, example: str = "0") -> dict[str, Any]:
    """Une altitude signée: pas de plancher à zéro, un sous-sol descend en dessous.

    Distincte d'une épaisseur ou d'une hauteur, qui sont toujours positives: un
    niveau peut légitimement être négatif, pour une dalle de sous-sol.
    """
    return {
        "type": "number",
        "default": 0,
        "description": (
            f"{description} {_UNIT_NOTE} Altitude signée: positive au-dessus du sol fini, "
            f"négative en sous-sol. Omise: zéro, le niveau du sol fini. Exemple: {example}."
        ),
    }


def _label_param(default: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 80,
        "default": default,
        "description": (
            "Nom du lot, qui devient le libellé de la marque d'annulation. Un plan "
            "entier annulé redevient un seul geste. Exemple: \"rez-de-chaussee\"."
        ),
    }


def _variant(
    discriminator: str,
    name: str,
    summary: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """Branche d'une union discriminée, close sur ses propres propriétés."""
    props: dict[str, Any] = {
        discriminator: {"type": "string", "const": name, "description": summary}
    }
    props.update(properties)
    return {
        "type": "object",
        "title": name,
        "description": summary,
        "properties": props,
        "required": [discriminator, *required],
        "additionalProperties": False,
    }


def _selector_properties() -> dict[str, Any]:
    """Critères de sélection, communs à la lecture et à la suppression.

    Ils se combinent par ET logique. Le filtre est unique et partagé: l'ancien
    catalogue déclarait une suppression par calque, une par couleur, une par
    type, une par handle, et une combinaison type+couleur. Cinq outils pour un
    seul concept, dont deux portaient le même nom.
    """
    return {
        "layer": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Nom exact du calque, comparaison par nom entier et non par sous-chaîne. "
                "Exemple: \"WALLS\"."
            ),
        },
        "type": {
            "type": "string",
            "enum": _ENTITY_TYPES,
            "description": (
                "Type DXF de l'entité, tel que renvoyé par query_entities dans le champ "
                "\"type\". Exemple: \"LWPOLYLINE\"."
            ),
        },
        "color": _color(),
        "handles": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "Handles d'entités. Un handle s'obtient d'abord par query_entities ou "
                "dans la réponse de draw / build_structure; il n'est jamais deviné. "
                "Exemple: [\"1F3\", \"1F4\"]."
            ),
        },
        "window": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
            "description": (
                "Fenêtre rectangulaire [xmin, ymin, xmax, ymax] dans l'unité du document. "
                "Ne retient que les entités entièrement contenues. Exemple: [0, 0, 10, 8]."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Opérations de dessin, argument de `draw`
# ---------------------------------------------------------------------------


def _draw_variants() -> list[dict[str, Any]]:
    common = {"layer": _layer("0"), "color": _color()}
    annotation = {"layer": _layer("ANNOTATION"), "color": _color()}

    return [
        _variant(
            "op",
            "line",
            "Segment droit entre deux points.",
            {
                "start": _point2("Origine du segment."),
                "end": _point2("Extrémité du segment.", "[5, 0]"),
                **common,
            },
            ["start", "end"],
        ),
        _variant(
            "op",
            "polyline",
            (
                "Polyligne, ouverte ou fermée. Une polyligne fermée est UNE entité: "
                "sélectionnable d'un clic, hachurable, et son aire est calculable. "
                "À préférer systématiquement à une suite de segments indépendants."
            ),
            {
                "points": _points("Sommets dans l'ordre du tracé."),
                "closed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Ferme le contour. Ne pas répéter le premier sommet en fin de "
                        "liste: la fermeture est automatique."
                    ),
                },
                "width": {
                    "type": "number",
                    "minimum": 0,
                    "default": 0,
                    "description": f"Largeur de trait constante. {_UNIT_NOTE} Exemple: 0.05.",
                },
                **common,
            },
            ["points"],
        ),
        _variant(
            "op",
            "rectangle",
            (
                "Rectangle tracé comme une polyligne fermée, donc une seule entité. "
                "Les deux coins peuvent être donnés dans n'importe quel ordre."
            ),
            {
                "corner1": _point2("Premier coin."),
                "corner2": _point2("Coin opposé.", "[4, 3]"),
                **common,
            },
            ["corner1", "corner2"],
        ),
        _variant(
            "op",
            "circle",
            "Cercle complet.",
            {
                "center": _point2("Centre."),
                "radius": _length("Rayon, strictement positif.", "1.5"),
                **common,
            },
            ["center", "radius"],
        ),
        _variant(
            "op",
            "arc",
            "Arc de cercle, parcouru du départ vers l'arrivée dans le sens direct.",
            {
                "center": _point2("Centre de l'arc."),
                "radius": _length("Rayon, strictement positif.", "1.0"),
                "start_angle_deg": _angle_deg("Angle de départ.", "0"),
                "end_angle_deg": _angle_deg("Angle d'arrivée.", "90"),
                **common,
            },
            ["center", "radius", "start_angle_deg", "end_angle_deg"],
        ),
        _variant(
            "op",
            "text",
            "Texte sur une seule ligne. Pour un paragraphe, utiliser mtext.",
            {
                "position": _point2("Point d'accroche du texte."),
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Texte à écrire, non vide. Exemple: \"SEJOUR\".",
                },
                "height": _length(
                    "Hauteur des capitales. Omise: la hauteur d'annotation du document, "
                    "0,25 m converti dans l'unité courante.",
                    "0.25",
                ),
                "rotation_deg": _angle_deg("Rotation du texte.", "0"),
                "halign": {
                    "type": "string",
                    "enum": ["left", "center", "right"],
                    "default": "left",
                    "description": "Alignement horizontal par rapport à position.",
                },
                **annotation,
            },
            ["position", "content"],
        ),
        _variant(
            "op",
            "mtext",
            "Texte multiligne, avec retour à la ligne automatique sur width.",
            {
                "position": _point2("Coin haut-gauche du paragraphe."),
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Texte, non vide. Le retour à la ligne est autorisé.",
                },
                "height": _length("Hauteur des capitales.", "0.25"),
                "width": {
                    "type": "number",
                    "minimum": 0,
                    "default": 0,
                    "description": (
                        f"Largeur du pavé avant retour à la ligne. {_UNIT_NOTE} "
                        "Zéro: pas de retour automatique. Exemple: 3."
                    ),
                },
                "rotation_deg": _angle_deg("Rotation du pavé.", "0"),
                **annotation,
            },
            ["position", "content"],
        ),
        _variant(
            "op",
            "hatch",
            (
                "Hachure délimitée par un ou plusieurs contours fermés. Chaque contour "
                "est refermé automatiquement si besoin: un contour ouvert produirait une "
                "hachure vide sans message d'erreur."
            ),
            {
                "boundaries": {
                    "type": "array",
                    "minItems": 1,
                    "items": _points("Contour fermé, au moins trois sommets.", minimum=3),
                    "description": (
                        "Liste de contours. Le premier est l'extérieur, les suivants sont "
                        "des trous. Exemple: [[[0,0],[4,0],[4,3],[0,3]]]."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "default": "SOLID",
                    "description": (
                        "Motif AutoCAD. SOLID remplit, ANSI31 hachure à 45 degrés, "
                        "ANSI32 et ANSI37 sont les autres motifs courants de maçonnerie. "
                        "Un motif inconnu fait échouer l'opération plutôt que de produire "
                        "une entité muette."
                    ),
                },
                "scale": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 1.0,
                    "description": (
                        "Échelle du motif. À adapter à l'unité du document: dans un "
                        "dessin en millimètres, l'échelle 1 donne un motif invisible. "
                        "Sans effet sur SOLID. Exemple: 0.05."
                    ),
                },
                "angle_deg": _angle_deg("Inclinaison du motif.", "0"),
                "layer": _layer("HATCH"),
                "color": _color(),
            },
            ["boundaries"],
        ),
    ]


# ---------------------------------------------------------------------------
# Éléments de bâtiment, argument de `build_structure`
# ---------------------------------------------------------------------------


def _hand() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["left", "right"],
        "default": "left",
        "description": (
            "Côté d'ouverture du battant, vu depuis le mur en allant de son début vers "
            "sa fin. Détermine le sens de l'arc de débattement."
        ),
    }


def _opening_deg() -> dict[str, Any]:
    return {
        "type": "number",
        "exclusiveMinimum": 0,
        "maximum": 180,
        "default": 90,
        "description": "Angle d'ouverture du battant, en **degrés**. Exemple: 90.",
    }


def _openings(context: str = "wall") -> dict[str, Any]:
    """Baies percées dans une enfilade de murs.

    Une baie n'est pas un symbole posé par-dessus le mur: elle le coupe en deux
    tronçons pleins. C'est pourquoi elle se déclare **avec** le réseau de murs
    et non après lui.

    ``context`` choisit la phrase de fin, seule partie qui change selon
    l'élément qui reçoit ce champ: percer un plan, élever un volume, ou coter
    ce qui existe déjà sans rien dessiner de nouveau.
    """
    item = {
        "type": "object",
        "title": "opening",
        "description": "Baie percée dans un mur de l'enfilade: porte, fenêtre ou passage.",
        "properties": {
            "segment": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Rang du mur percé dans l'enfilade. Le mur qui va de points[0] à "
                    "points[1] porte le rang 0, le suivant le rang 1, et ainsi de suite. "
                    "Une enfilade fermée a un mur de plus: le retour du dernier point "
                    "vers le premier. Un rang hors de l'enfilade est refusé. Exemple: 0."
                ),
            },
            "position": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Position du CENTRE de la baie le long de ce mur, en fraction de sa "
                    "longueur: 0 au début, 0.5 au milieu, 1 à la fin. Une baie qui "
                    "déborde du mur est rognée à ses bornes. Exemple: 0.5."
                ),
            },
            "width": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": (
                    f"Largeur du percement. {_UNIT_NOTE} Exemple: 0.9 pour une porte, "
                    "1.2 pour une fenêtre, 1.6 pour un passage large."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["door", "window", "passage"],
                "default": "door",
                "description": (
                    "Nature de la baie. \"door\" ajoute le vantail et son arc de "
                    "débattement sur le calque DOORS, \"window\" ajoute le vitrage sur "
                    "le calque WINDOWS, \"passage\" ne pose aucun symbole et laisse la "
                    "trémie nue. Dans les trois cas le mur est réellement percé."
                ),
            },
            "hand": _hand(),
            "opening_deg": _opening_deg(),
        },
        "required": ["segment", "position", "width"],
        "additionalProperties": False,
    }
    if context == "volume":
        trailer = (
            "Chacune ouvre réellement le volume: une fenêtre garde son allège et son "
            "linteau, une porte garde son linteau seul jusqu'au sol, un passage reste "
            "libre du sol au plafond. Omise ou vide: les murs restent pleins du sol au "
            "plafond."
        )
    elif context == "dimensions":
        trailer = (
            "Sert uniquement à situer les lignes de cote des percements et de leurs "
            "axes: rien n'est dessiné ni percé ici. Reprendre EXACTEMENT les mêmes baies "
            "que celles données à wall_network pour ce plan, sinon les cotes ne "
            "correspondront pas à la maçonnerie. Omise ou vide: seule la longueur hors "
            "tout de chaque mur est cotée."
        )
    else:  # wall
        trailer = (
            "Chacune coupe réellement le mur en deux tronçons au lieu d'y superposer un "
            "symbole. Omise ou vide: les murs restent pleins."
        )
    return {
        "type": "array",
        "maxItems": MAX_BATCH_ITEMS,
        "items": item,
        "description": (
            "Baies de cette enfilade: porte, fenêtre ou passage. "
            + trailer
            + " Exemple: "
            "[{\"segment\": 0, \"position\": 0.5, \"width\": 0.9, \"kind\": \"door\"}]."
        ),
    }


def _block_key() -> dict[str, Any]:
    """Clé d'un symbole de la bibliothèque, énumérée depuis ``ops.blocks``.

    La liste est lue dans ``blocks.LIBRARY``: recopier ces noms à la main ferait
    diverger le schéma du catalogue réel au premier ajout de symbole.
    """
    catalogue = ", ".join(
        f"{key} — {block_library.spec(key).description}" for key in block_library.LIBRARY
    )
    return {
        "type": "string",
        "enum": list(block_library.LIBRARY),
        "description": (
            "Symbole à insérer, choisi dans la bibliothèque du serveur: "
            + catalogue
            + ". La définition du bloc est créée automatiquement au premier emploi: "
            "il n'y a rien à définir au préalable. Exemple: \"toilet\"."
        ),
    }


def _structure_variants() -> list[dict[str, Any]]:
    hand = _hand()
    opening = _opening_deg()

    return [
        _variant(
            "element",
            "wall_network",
            (
                "Réseau de murs raccordés, percé de ses baies. C'EST LA FAÇON NORMALE "
                "DE DESSINER DES MURS, à préférer à wall et à wall_run dès qu'il y a "
                "plus d'un mur, et à door_in_wall ou window pour poser une ouverture.\n\n"
                "Trois choses qu'aucun autre élément ne sait faire. Les angles se "
                "RACCORDENT: deux murs perpendiculaires forment un angle net, au lieu "
                "de se recouvrir sur un carré de la taille de l'épaisseur. Une enfilade "
                "fermée rend DEUX anneaux, extérieur et intérieur, donc deux polylignes "
                "fermées mesurables et hachurables, au lieu de quatre rectangles qui se "
                "dépassent. Et une baie PERCE le mur: elle le coupe en deux tronçons et "
                "ferme la coupe par un jambage, au lieu de poser un symbole par-dessus "
                "un mur resté plein."
            ),
            {
                "points": _points("Points de passage de l'axe des murs, dans l'ordre."),
                "closed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Referme l'enfilade du dernier point vers le premier. Ne pas "
                        "répéter le premier point en fin de liste: la fermeture est "
                        "automatique, et le sommet dupliqué serait ignoré."
                    ),
                },
                "thickness": _thickness("des murs"),
                "openings": _openings(),
                "show_symbols": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Dessine le vantail des portes et le vitrage des fenêtres. À "
                        "mettre à false pour n'obtenir que la maçonnerie percée, par "
                        "exemple pour poser ensuite des blocs avec place_blocks."
                    ),
                },
                "layer": _layer("WALLS"),
                "color": _color(),
            },
            ["points"],
        ),
        _variant(
            "element",
            "wall",
            (
                "Mur ISOLÉ d'épaisseur constante, tracé comme UNE polyligne fermée sur "
                "le calque WALLS. Réservé au mur unique qui ne touche rien: dès qu'il "
                "y a deux murs, wall_network raccorde leurs angles et perce leurs "
                "baies, ce que celui-ci ne sait pas faire."
            ),
            {
                "start": _point2("Début de l'axe du mur."),
                "end": _point2("Fin de l'axe du mur.", "[5, 0]"),
                "thickness": _thickness("du mur"),
                "layer": _layer("WALLS"),
                "color": _color(),
            },
            ["start", "end"],
        ),
        _variant(
            "element",
            "wall_run",
            (
                "Enfilade de murs raccordés suivant une polyligne de points, sans "
                "aucune baie. C'est exactement wall_network sans son champ openings, "
                "gardé pour les cas où le mur est plein: dès qu'une porte ou une "
                "fenêtre doit y être percée, utiliser wall_network. Les points "
                "confondus sont ignorés au lieu de lever."
            ),
            {
                "points": _points("Points de passage de l'axe, dans l'ordre."),
                "closed": {
                    "type": "boolean",
                    "default": False,
                    "description": "Referme l'enfilade du dernier point vers le premier.",
                },
                "thickness": _thickness("des murs"),
                "color": _color(),
            },
            ["points"],
        ),
        _variant(
            "element",
            "door",
            (
                "Porte définie par sa charnière et l'extrémité de son battant. À utiliser "
                "quand la position exacte du vantail est connue; sinon door_in_wall "
                "positionne la porte le long d'un mur donné, ce qui est le cas courant."
            ),
            {
                "hinge": _point2("Point de charnière."),
                "leaf_end": _point2("Extrémité libre du battant.", "[0, 0.9]"),
                "opening_deg": opening,
                "hand": hand,
                "show_swing": {
                    "type": "boolean",
                    "default": True,
                    "description": "Trace l'arc de débattement en plus du vantail.",
                },
                "color": _color(),
            },
            ["hinge", "leaf_end"],
        ),
        _variant(
            "element",
            "door_in_wall",
            (
                "Porte posée le long d'un mur donné, positionnée par une fraction de sa "
                "longueur. Le vantail sort perpendiculairement au mur porteur, quelle que "
                "soit son orientation. Attention: cet élément dessine le SYMBOLE seul, "
                "il ne perce pas la maçonnerie. Pour une porte qui coupe réellement le "
                "mur, déclarer la baie dans les openings de wall_network."
            ),
            {
                "wall_start": _point2("Début de l'axe du mur porteur."),
                "wall_end": _point2("Fin de l'axe du mur porteur.", "[5, 0]"),
                "position": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.5,
                    "description": (
                        "Position de la charnière le long du mur, en fraction de sa "
                        "longueur: 0 au début, 1 à la fin, 0.5 au milieu."
                    ),
                },
                "width": _length(
                    "Largeur du passage. Omise: 0,90 m converti dans l'unité du document.",
                    "0.9",
                ),
                "opening_deg": opening,
                "hand": hand,
            },
            ["wall_start", "wall_end"],
        ),
        _variant(
            "element",
            "window",
            (
                "Fenêtre posée entre deux points: le dormant tracé comme une polyligne "
                "fermée sur le calque WINDOWS, plus les deux traits de vitrage. L'écart "
                "des traits dérive de l'épaisseur, jamais d'une constante en dur. "
                "Attention: comme door_in_wall, cet élément se superpose au mur sans le "
                "percer. Pour une baie réellement ouverte, la déclarer dans les "
                "openings de wall_network."
            ),
            {
                "start": _point2("Début de la baie, sur l'axe du mur."),
                "end": _point2("Fin de la baie.", "[3.2, 0]"),
                "thickness": _thickness("du mur percé"),
                "color": _color(),
            },
            ["start", "end"],
        ),
        _variant(
            "element",
            "room",
            (
                "Pièce rectangulaire: quatre murs jointifs plus une étiquette portant le "
                "nom et la surface. La surface écrite est une mesure que le modèle peut "
                "relire ensuite par query_entities."
            ),
            {
                "corner1": _point2("Premier coin de l'axe du rectangle."),
                "corner2": _point2("Coin opposé.", "[4, 3]"),
                "thickness": _thickness("des murs"),
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Nom porté par l'étiquette. Exemple: \"SEJOUR\".",
                },
                "show_area": {
                    "type": "boolean",
                    "default": True,
                    "description": "Ajoute la surface calculée à l'étiquette.",
                },
                "color": _color(),
            },
            ["corner1", "corner2"],
        ),
        _variant(
            "element",
            "label",
            (
                "Étiquette centrée, déposée d'office sur le calque ANNOTATION. Le calque "
                "est imposé: une annotation perdue au milieu des murs disparaîtrait avec "
                "eux quand on gèle le calque."
            ),
            {
                "position": _point2("Centre du texte."),
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Texte de l'étiquette, non vide.",
                },
                "height": _length("Hauteur des capitales.", "0.25"),
                "rotation_deg": _angle_deg("Rotation de l'étiquette.", "0"),
                "color": _color(),
            },
            ["position", "text"],
        ),
        _variant(
            "element",
            "wall_volume",
            (
                "Élève en VOLUME le réseau de murs que wall_network trace en plan: mêmes "
                "points, même fermeture, mêmes épaisseurs, mêmes baies, plus une hauteur. "
                "Appeler les deux avec EXACTEMENT les mêmes valeurs donne un plan et un "
                "volume qui coïncident trait pour trait, la même géométrie élevée dans le "
                "troisième axe. Chaque baie devient un percement réel: une fenêtre garde "
                "son allège et son linteau, une porte garde son linteau seul et son passage "
                "descend jusqu'au sol, un passage libre reste ouvert du sol au plafond.\n\n"
                "À utiliser pour donner une hauteur à un plan déjà tracé, avant d'appeler "
                "render_view en projection iso: sans volume construit, la vue en trois "
                "dimensions n'a rien à montrer."
            ),
            {
                "points": _points(
                    "Points de passage de l'axe des murs, identiques à ceux donnés à "
                    "wall_network pour que le volume coïncide avec le plan."
                ),
                "closed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Referme l'enfilade du dernier point vers le premier, exactement "
                        "comme le champ closed de wall_network."
                    ),
                },
                "thickness": _thickness("des murs"),
                "openings": _openings("volume"),
                "height": _length(
                    "Hauteur du volume, du sol au sommet des murs. Omise: la hauteur "
                    "sous plafond courante, 2,50 m convertie dans l'unité du document.",
                    "2.5",
                ),
                "layer": _layer("WALLS"),
                "color": _color(),
            },
            ["points"],
        ),
        _variant(
            "element",
            "slab",
            (
                "Dalle de plancher pleine: un prisme élevé sous un contour, dont le "
                "dessus affleure le niveau z, de sorte que des murs élevés depuis ce même "
                "niveau reposent dessus au lieu de le traverser.\n\n"
                "À utiliser pour donner un sol à un volume, avant wall_volume, avec le "
                "contour extérieur du même plan."
            ),
            {
                "contour": _points(
                    "Sommets du contour de la dalle, dans l'ordre, typiquement le "
                    "contour extérieur du plan.",
                    minimum=3,
                ),
                "thickness": _length(
                    "Épaisseur de la dalle. Omise: l'épaisseur courante de plancher, "
                    "0,20 m convertie dans l'unité du document.",
                    "0.2",
                ),
                "z": _level("Niveau du dessus de la dalle, là où les murs viendront reposer."),
                "layer": _layer("STRUCTURE"),
            },
            ["contour"],
        ),
        _variant(
            "element",
            "box",
            (
                "Boîte droite entre deux coins en plan, élevée du niveau z sur la hauteur "
                "donnée: une silhouette de meuble ou d'appareil, pas un modèle de "
                "fabricant. C'est ce qui permet de poser du mobilier en volume.\n\n"
                "À utiliser pour peupler une vue en trois dimensions une fois les murs et "
                "la dalle posés: un lit, une table ou un plan de travail rendent l'échelle "
                "du logement plus lisible qu'un volume vide."
            ),
            {
                "corner1": _point2("Premier coin en plan."),
                "corner2": _point2("Coin opposé en plan.", "[1.0, 0.6]"),
                "height": _length(
                    "Hauteur de la boîte, du niveau z jusqu'à son sommet.", "0.45"
                ),
                "z": _level("Niveau de la base de la boîte."),
                "layer": _layer("FURNITURE"),
                "color": _color(),
            },
            ["corner1", "corner2", "height"],
        ),
        _variant(
            "element",
            "dimensions",
            (
                "Cote un réseau de murs entier, façade par façade: d'abord les "
                "percements tableau par tableau, puis les axes des baies, puis la "
                "longueur hors tout, chaque ligne se déportant un peu plus loin que la "
                "précédente pour rester lisible. C'est le pendant coté de wall_network: "
                "donnez-lui les MÊMES points, la MÊME fermeture, les MÊMES baies et la "
                "MÊME épaisseur que le plan à coter, sans quoi les cotes ne correspondront "
                "pas à la maçonnerie.\n\n"
                "À utiliser une fois les murs tracés, pour produire les cotes qu'un plan "
                "d'exécution exige, au lieu de positionner chaque ligne de cote à la main."
            ),
            {
                "points": _points(
                    "Points de passage de l'axe des murs, identiques à ceux du plan à coter."
                ),
                "closed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Referme l'enfilade du dernier point vers le premier, exactement "
                        "comme le champ closed de wall_network."
                    ),
                },
                "thickness": {
                    "type": "number",
                    "minimum": 0,
                    "description": (
                        "Épaisseur des murs cotés, qui fixe le déport de la première "
                        f"ligne de cote hors de la maçonnerie. {_UNIT_NOTE} Doit "
                        "correspondre à l'épaisseur donnée à wall_network pour ce même "
                        "plan. Omise: l'épaisseur de maçonnerie courante, 0,20 m "
                        "convertie dans l'unité du document. Exemple: 0.2."
                    ),
                },
                "openings": _openings("dimensions"),
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Rang d'un mur de l'enfilade, dans la même numérotation que "
                            "openings.segment. Exemple: 0."
                        ),
                    },
                    "maxItems": MAX_BATCH_ITEMS,
                    "description": (
                        "Rangs des murs à coter. Omis: tous les murs de l'enfilade sont "
                        "cotés, ce qui est le cas courant. À restreindre pour ne coter "
                        "qu'une façade sans encombrer le reste du plan. Exemple: [0, 2]."
                    ),
                },
                "outside": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Place les lignes de cote à l'extérieur du contour plutôt qu'à "
                        "l'intérieur, où elles recouvriraient les pièces de chiffres. "
                        "Sans effet notable sur une enfilade ouverte, où l'extérieur est "
                        "affaire de convention plutôt que de géométrie."
                    ),
                },
                "layer": _layer("DIMENSIONS"),
            },
            ["points"],
        ),
    ]


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def build_catalog(config: Config) -> tuple[ToolSpec, ...]:
    """Construit le catalogue, les défauts venant de la configuration.

    ``config`` fournit la limite de requête et la taille de rendu par défaut,
    afin qu'un réglage d'environnement soit visible dans le schéma que lit le
    modèle plutôt que caché dans le code d'exécution.
    """
    default_limit = max(1, min(int(config.query_limit), MAX_QUERY_LIMIT))
    render_width, render_height = config.render_size

    return (
        ToolSpec(
            name="get_drawing_info",
            title="État du dessin",
            description=(
                "Décrit le document courant: moteur utilisé, unité de longueur, nombre "
                "d'entités, liste des calques avec leur couleur et leur état, limites du "
                "dessin, et valeurs par défaut converties dans l'unité du document.\n\n"
                "À appeler EN PREMIER, avant tout tracé: l'unité conditionne toutes les "
                "longueurs que vous allez donner ensuite, et la liste des calques évite "
                "d'en inventer un nouveau alors qu'il existe. À rappeler après avoir "
                "ouvert un autre document."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
        ),
        ToolSpec(
            name="query_entities",
            title="Inspecter les entités",
            description=(
                "Liste les entités du dessin qui satisfont un filtre, avec leur handle, "
                "leur type, leur calque, leur couleur et leur boîte englobante.\n\n"
                "À utiliser pour retrouver ce qui existe déjà avant de modifier ou de "
                "supprimer: les handles renvoyés ici sont le préalable obligatoire à "
                "set_entity_color et à une suppression par handles.\n\n"
                "La réponse est TOUJOURS bornée. Elle donne le compte total des entités "
                "correspondantes ET un échantillon limité, avec un drapeau indiquant si "
                "elle a été tronquée. Un dessin de cinquante mille entités ne sera jamais "
                "déversé: affinez le filtre plutôt que d'augmenter la limite.\n\n"
                "Pour voir le dessin plutôt que de l'énumérer, utilisez render_view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_selector_properties(),
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_QUERY_LIMIT,
                        "default": default_limit,
                        "description": (
                            f"Nombre maximal d'entités décrites. Défaut {default_limit}, "
                            f"plafond dur {MAX_QUERY_LIMIT}. Le compte total est rendu "
                            "quoi qu'il arrive."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
        ),
        ToolSpec(
            name="measure",
            title="Mesurer le dessin",
            description=(
                "Mesure et résume le dessin sans l'énumérer. UN SEUL outil, dont le "
                "champ mode choisit la question:\n"
                "- summary: état des lieux complet — combien d'entités, sur quels "
                "calques, de quels types, entre quelles limites.\n"
                "- totals: longueur et surface cumulées.\n"
                "- by_layer / by_type: les mêmes quantités, réparties par calque ou "
                "par type.\n"
                "- quantities: la nomenclature, une ligne par couple calque/type.\n"
                "- nearest: les entités les plus proches d'un point, avec leur "
                "distance. Répond à « qu'y a-t-il ici ».\n"
                "- in_window: les entités d'une zone rectangulaire, au choix "
                "entièrement contenues ou simplement traversées.\n\n"
                "À utiliser AVANT de modifier un dessin que vous n'avez pas produit, "
                "pour savoir ce qu'il contient sans rapatrier ses entités une à une, et "
                "APRÈS un lot pour vérifier des quantités. Pour voir le dessin plutôt "
                "que de le chiffrer, utilisez render_view; pour obtenir les handles "
                "d'entités précises, query_entities.\n\n"
                "HONNÊTETÉ DES MESURES: longueurs et surfaces ne sont comptées que pour "
                "les entités dont le moteur les publie. Chaque mesure porte donc "
                "counted, missing et complete, et son total vaut null — et non zéro — "
                "quand aucune entité n'a répondu. Un total de zéro signifierait « rien "
                "ne mesure », ce qui est faux; null signifie « le moteur ne le dit "
                "pas ». Les comptes d'entités, eux, sont toujours exacts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": list(MEASURE_MODES),
                        "default": "summary",
                        "description": (
                            "Question posée. \"nearest\" exige point, \"in_window\" "
                            "exige window; les autres modes les ignorent."
                        ),
                    },
                    "layer": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Restreint la mesure à ce calque, par nom exact. Omis: tout "
                            "le dessin. Exemple: \"WALLS\"."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": _ENTITY_TYPES,
                        "description": (
                            "Restreint la mesure à ce type DXF. Omis: tous les types. "
                            "Exemple: \"LWPOLYLINE\"."
                        ),
                    },
                    "point": _point2(
                        "Point de référence du mode nearest.", "[2.5, 1.5]"
                    ),
                    "window": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": (
                            "Fenêtre [xmin, ymin, xmax, ymax] du mode in_window, dans "
                            "l'unité du document. Les deux coins peuvent venir dans "
                            "n'importe quel ordre. Exemple: [0, 0, 10, 8]."
                        ),
                    },
                    "window_mode": {
                        "type": "string",
                        "enum": ["inside", "crossing"],
                        "default": "inside",
                        "description": (
                            "Mode de sélection par fenêtre, au sens d'AutoCAD. "
                            "\"inside\" ne garde que ce qui tient entièrement dans la "
                            "fenêtre, \"crossing\" garde aussi ce qui la traverse. Sur "
                            "un plan, le premier rend les pièces d'une zone, le second "
                            "rend en plus les murs qui la bordent."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_QUERY_LIMIT,
                        "default": default_limit,
                        "description": (
                            f"Nombre maximal d'entités décrites par nearest et "
                            f"in_window. Défaut {default_limit}, plafond dur "
                            f"{MAX_QUERY_LIMIT}. Sans effet sur les autres modes, dont "
                            "la réponse est déjà un résumé."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
        ),
        ToolSpec(
            name="check_plan",
            title="Vérifier le plan",
            description=(
                "Cherche les défauts qu'un dessin ne signale jamais de lui-même: "
                "contours qui ne se referment pas et refusent la hachure, murs qui se "
                "croisent en croix au lieu de se rejoindre, entités superposées en "
                "double, résidus de taille négligeable, extrémités qui se ratent d'un "
                "cheveu.\n\n"
                "À appeler APRÈS un lot de construction et AVANT de livrer un plan: "
                "c'est ce qui vous permet de vous corriger seul. Chaque défaut est "
                "rendu avec sa gravité, sa localisation en coordonnées, les objets "
                "concernés et un remède en une phrase, directement exécutable.\n\n"
                "CE QUI EST EXAMINÉ. Les entités du document, telles que le moteur les "
                "publie, donnent les doublons et les résidus. Les contours et les axes "
                "de murs, eux, doivent être FOURNIS dans contours et segments: le "
                "moteur ne publie que des boîtes englobantes, pas les sommets, et "
                "deviner une géométrie serait pire que l'avouer. Donnez-y les mêmes "
                "points que ceux passés à build_structure, c'est la vérification la "
                "plus utile.\n\n"
                "Les seuils suivent l'unité du document: à l'échelle du mètre un écart "
                "d'un dixième de millimètre est une jonction, à l'échelle du millimètre "
                "c'est un trou. La réponse est bornée: le compte des défauts est exact, "
                "la liste est tronquée au-delà de "
                f"{MAX_PROBLEMS} entrées."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "layer": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Restreint l'examen des entités du document à ce calque, "
                            "par nom exact. Omis: tout le dessin. Exemple: \"WALLS\"."
                        ),
                    },
                    "contours": {
                        "type": "array",
                        "maxItems": MAX_CHECK_ITEMS,
                        "description": (
                            "Contours censés délimiter une surface, à confronter à leur "
                            "fermeture. Exemple: "
                            "[{\"points\": [[0,0],[4,0],[4,3],[0,3]], \"ref\": \"sejour\"}]."
                        ),
                        "items": {
                            "type": "object",
                            "title": "contour",
                            "description": "Suite de sommets censée délimiter une surface.",
                            "properties": {
                                "points": _points("Sommets du contour.", minimum=2),
                                "closed": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": (
                                        "Fermeture portée par l'entité elle-même. Un "
                                        "contour déclaré fermé est correct par "
                                        "construction et n'est pas examiné."
                                    ),
                                },
                                "ref": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "Nom ou handle qui désigne ce contour dans le "
                                        "rapport. Sans lui, le défaut est rendu sous un "
                                        "rang, moins parlant. Exemple: \"sejour\"."
                                    ),
                                },
                                "layer": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Calque du contour, rappelé dans le rapport.",
                                },
                            },
                            "required": ["points"],
                            "additionalProperties": False,
                        },
                    },
                    "segments": {
                        "type": "array",
                        "maxItems": MAX_CHECK_ITEMS,
                        "description": (
                            "Axes de murs, ou tout segment droit à confronter aux "
                            "autres: croisements et trous entre extrémités. Exemple: "
                            "[{\"start\": [0,0], \"end\": [4,0], \"ref\": \"mur sud\"}]."
                        ),
                        "items": {
                            "type": "object",
                            "title": "segment",
                            "description": "Axe d'un mur, donné par ses deux extrémités.",
                            "properties": {
                                "start": _point2("Début de l'axe."),
                                "end": _point2("Fin de l'axe.", "[4, 0]"),
                                "ref": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "Nom ou handle qui désigne cet axe dans le "
                                        "rapport. Exemple: \"mur sud\"."
                                    ),
                                },
                                "layer": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Calque de l'axe, rappelé dans le rapport.",
                                },
                            },
                            "required": ["start", "end"],
                            "additionalProperties": False,
                        },
                    },
                    "tolerance": _length(
                        "Écart en deçà duquel deux points sont tenus pour confondus, et "
                        "rayon de recherche des trous entre extrémités. Omise: la "
                        "tolérance de l'unité du document, qui est le bon choix dans "
                        "presque tous les cas.",
                        "0.001",
                    ),
                    "minimum_size": _length(
                        "Taille en deçà de laquelle une entité est tenue pour un "
                        "résidu, mesurée sur la diagonale de sa boîte englobante. "
                        "Omise: la tolérance. À augmenter pour débusquer les traits "
                        "parasites d'un plan repris.",
                        "0.01",
                    ),
                },
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
        ),
        ToolSpec(
            name="render_view",
            title="Voir le dessin",
            description=(
                "Rend le dessin courant en image PNG et renvoie cette image dans la "
                "réponse, accompagnée du nombre d'entités et des limites du dessin.\n\n"
                "À appeler APRÈS CHAQUE LOT D'ÉCRITURE. C'est le seul moyen de vérifier "
                "ce qui a réellement été tracé au lieu de faire confiance à un compte "
                "rendu: un mur peut être créé avec succès et se trouver au mauvais "
                "endroit, à la mauvaise échelle ou superposé à un autre. Sans regarder, "
                "vous dessinez à l'aveugle.\n\n"
                "À appeler aussi avant de corriger un dessin existant, pour savoir ce "
                "qu'il contient.\n\n"
                "DEUX PROJECTIONS. \"plan\" (défaut) est la vue de dessus, celle qui "
                "convient à un dessin en deux dimensions. \"iso\" relit les VOLUMES du "
                "document et rend une vue en perspective depuis un point de vue "
                "réglable: à appeler après avoir élevé des murs avec build_structure "
                "(wall_volume, slab, box), pour juger de hauteurs, d'allèges ou de "
                "linteaux qu'un plan ne montre pas. Un document sans volume ou un moteur "
                "qui n'en garde pas la trace refuse la projection iso avec une erreur "
                "explicite plutôt qu'une image vide qu'on croirait correcte.\n\n"
                "Le cadrage est automatique et centré sur le contenu; un dessin vide rend "
                "une image vide, ce qui n'est pas une erreur. Exige un moteur capable de "
                "rendu: le backend DXF le fait, le backend AutoCAD non, puisque l'écran "
                "d'AutoCAD tient ce rôle."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "width": {
                        "type": "integer",
                        "minimum": MIN_RENDER_PIXELS,
                        "maximum": MAX_RENDER_PIXELS,
                        "default": render_width,
                        "description": f"Largeur de l'image en pixels. Défaut {render_width}.",
                    },
                    "height": {
                        "type": "integer",
                        "minimum": MIN_RENDER_PIXELS,
                        "maximum": MAX_RENDER_PIXELS,
                        "default": render_height,
                        "description": f"Hauteur de l'image en pixels. Défaut {render_height}.",
                    },
                    "projection": {
                        "type": "string",
                        "enum": list(RENDER_PROJECTIONS),
                        "default": "plan",
                        "description": (
                            "Point de vue du rendu. \"plan\" est la vue de dessus, celle "
                            "du dessin en deux dimensions. \"iso\" est une vue en volume, "
                            "en perspective, qui exige des volumes déjà construits par "
                            "build_structure (wall_volume, slab, box): sans eux, ou sur "
                            "un moteur qui n'expose pas de document, l'appel échoue avec "
                            "une erreur qui le dit plutôt que de rendre une image vide."
                        ),
                    },
                    "elevation_deg": {
                        "type": "number",
                        "description": (
                            "Hauteur du point de vue au-dessus de l'horizon, en "
                            "**degrés**, pour la projection iso seulement; sans effet en "
                            "projection plan. Zéro regarde à l'horizontale, 90 regarde le "
                            "dessin depuis le dessus. Omis: l'angle isométrique par "
                            "défaut du serveur, environ 26 degrés. Exemple: 26."
                        ),
                    },
                    "azimuth_deg": {
                        "type": "number",
                        "description": (
                            "Orientation du point de vue autour du bâtiment, en "
                            "**degrés**, pour la projection iso seulement; sans effet en "
                            "projection plan. Omis: l'azimut isométrique par défaut du "
                            "serveur, environ -56 degrés. Exemple: -56."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            read_only=True,
            idempotent=True,
        ),
        ToolSpec(
            name="draw",
            title="Dessiner un lot de primitives",
            description=(
                "Trace une LISTE de primitives géométriques en un seul lot: segments, "
                "polylignes, rectangles, cercles, arcs, textes, paragraphes et hachures.\n\n"
                "À utiliser dès qu'il y a plus d'une entité à tracer, ce qui est le cas "
                "presque toujours. Le lot forme une transaction unique: une seule marque "
                "d'annulation, un seul rafraîchissement d'affichage. Enchaîner des appels "
                "unitaires est plus lent et rend l'annulation impraticable.\n\n"
                "Pour des éléments de bâtiment (murs, portes, fenêtres, pièces), utilisez "
                "build_structure: il pose les calques normalisés et la géométrie attendue "
                "d'un plan.\n\n"
                "Les angles sont en DEGRÉS. Les longueurs sont dans l'unité du document, "
                "que get_drawing_info donne.\n\n"
                "La réponse porte les DEUX côtés: les handles réellement créés, et les "
                "échecs éventuels avec l'index de l'élément fautif dans la liste fournie. "
                "Un lot partiellement exécuté n'est jamais annoncé comme un succès. "
                "Vérifiez ensuite avec render_view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BATCH_ITEMS,
                        "description": (
                            "Opérations à exécuter, dans l'ordre. Chaque élément est "
                            "distingué par son champ \"op\"."
                        ),
                        "items": {"oneOf": _draw_variants()},
                    },
                    "label": _label_param("draw"),
                },
                "required": ["operations"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="build_structure",
            title="Construire un lot d'éléments de bâtiment",
            description=(
                "Construit une LISTE d'éléments de bâtiment en un seul lot: réseaux de "
                "murs percés de leurs baies, murs isolés, portes, fenêtres, pièces, "
                "étiquettes, volumes élevés en trois dimensions et cotation automatique.\n\n"
                "À utiliser pour tout ce qui relève du plan d'architecture, de préférence à "
                "draw, qui ne connaît que la géométrie nue. Chaque "
                "élément est posé sur son calque normalisé, un mur épais est une polyligne "
                "fermée donc une entité unique et hachurable, et le battant d'une porte "
                "suit l'orientation de son mur porteur.\n\n"
                "POUR LES MURS, UTILISEZ wall_network, et donnez-lui ses openings. C'est "
                "le seul élément qui raccorde les angles et qui perce réellement la "
                "maçonnerie; wall, wall_run, door_in_wall et window ne servent plus que "
                "pour un objet isolé ou un symbole posé sur un mur existant.\n\n"
                "POUR LA TROISIÈME DIMENSION, wall_volume élève le même plan en volume "
                "quand on lui donne les mêmes points, slab pose une dalle sous un contour, "
                "et box pose une silhouette de meuble: c'est ce que render_view en "
                "projection iso vient ensuite montrer.\n\n"
                "POUR LA COTATION, dimensions cote un réseau de murs entier, façade par "
                "façade, à partir des mêmes points et des mêmes baies que wall_network.\n\n"
                "Toutes les longueurs omises prennent une valeur par défaut convertie dans "
                "l'unité du document: un mur de vingt centimètres reste un mur de vingt "
                "centimètres que le dessin soit en mètres ou en millimètres.\n\n"
                "Les angles sont en DEGRÉS. La réponse porte les handles créés ET les "
                "échecs avec l'index de l'élément fautif. Vérifiez ensuite avec render_view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "elements": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BATCH_ITEMS,
                        "description": (
                            "Éléments à construire, dans l'ordre. Chaque élément est "
                            "distingué par son champ \"element\"."
                        ),
                        "items": {"oneOf": _structure_variants()},
                    },
                    "label": _label_param("build_structure"),
                },
                "required": ["elements"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="place_blocks",
            title="Placer des blocs de la bibliothèque",
            description=(
                "Insère une LISTE d'occurrences de symboles normalisés en un seul lot: "
                "porte, fenêtre, WC, lavabo, douche, évier, prise, interrupteur, point "
                "lumineux, lits, table, chaise.\n\n"
                "À utiliser pour équiper et meubler un plan, une fois les murs posés "
                "par build_structure. Un seul appel place tous les objets d'un "
                "logement: n'enchaînez pas un appel par objet.\n\n"
                "Ce que cet outil apporte sur un tracé équivalent fait avec draw: une "
                "occurrence de bloc reste liée à SA définition, donc corriger le "
                "symbole une fois corrige toutes ses occurrences; chaque occurrence "
                "porte un attribut de repère, donc les objets se comptent et se "
                "chiffrent; et chaque symbole va sur le calque de sa nature, un WC sur "
                "PLUMBING, une prise sur ELECTRICAL, une chaise sur FURNITURE.\n\n"
                "LES DÉFINITIONS MANQUANTES SONT CRÉÉES AUTOMATIQUEMENT, il n'y a rien "
                "à préparer. Une définition déjà présente est conservée telle quelle: "
                "le même appel répété n'écrase jamais un symbole existant.\n\n"
                "Chaque symbole a son repère local, décrit dans la description de sa "
                "clé. Ce qui se pose contre un mur — porte, fenêtre, WC, lavabo, "
                "prise — a le mur pour axe X et la pièce du côté des Y positifs: "
                "rotation_deg à 0 pose l'objet contre un mur horizontal, la pièce "
                "au-dessus. Les longueurs sont réelles et suivent l'unité du document. "
                "La réponse porte les handles créés ET les échecs avec l'index de "
                "l'élément fautif. Vérifiez ensuite avec render_view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "blocks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BATCH_ITEMS,
                        "description": (
                            "Occurrences à insérer, dans l'ordre. Exemple: "
                            "[{\"block\": \"toilet\", \"at\": [1.2, 0.1]}, "
                            "{\"block\": \"basin\", \"at\": [2.4, 0.1], \"mark\": \"LV1\"}]."
                        ),
                        "items": {
                            "type": "object",
                            "title": "block_instance",
                            "description": "Une occurrence de symbole, posée en un point.",
                            "properties": {
                                "block": _block_key(),
                                "at": _point2(
                                    "Point d'insertion, sur lequel vient se poser le "
                                    "point de base du symbole.",
                                    "[1.2, 0.1]",
                                ),
                                "rotation_deg": _angle_deg(
                                    "Rotation de l'occurrence autour de son point "
                                    "d'insertion. Pour un objet posé contre un mur, "
                                    "c'est l'angle de ce mur: 0 vers l'est, 90 vers le "
                                    "nord, 180 vers l'ouest.",
                                    "90",
                                ),
                                "scale": {
                                    "type": "number",
                                    "exclusiveMinimum": 0,
                                    "default": 1.0,
                                    "description": (
                                        "Facteur d'échelle uniforme de l'occurrence. "
                                        "Laisser 1: les symboles sont déjà à leur taille "
                                        "réelle dans l'unité du document. À ne changer "
                                        "que pour un objet réellement plus petit ou plus "
                                        "grand que le modèle courant. Exemple: 1."
                                    ),
                                },
                                "mark": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 32,
                                    "description": (
                                        "Valeur du repère porté par cette occurrence, "
                                        "qui sera lisible sur le plan et extractible en "
                                        "nomenclature. Omise: le repère du catalogue, "
                                        "identique pour toutes les occurrences du même "
                                        "symbole, ce qui suffit à compter mais pas à "
                                        "renvoyer. Exemple: \"P1\"."
                                    ),
                                },
                            },
                            "required": ["block", "at"],
                            "additionalProperties": False,
                        },
                    },
                    "label": _label_param("place_blocks"),
                },
                "required": ["blocks"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="delete_entities",
            title="Supprimer des entités",
            description=(
                "DESTRUCTIF ET IRRÉVERSIBLE. Supprime toutes les entités correspondant au "
                "filtre et renvoie leurs handles. undo_last_batch ne restaure PAS ce que "
                "cet outil efface: il n'annule que des créations.\n\n"
                "À utiliser pour retirer ce qui a été mal tracé quand undo_last_batch ne "
                "suffit plus, parce que d'autres lots sont venus par-dessus, ou pour nettoyer "
                "un calque entier.\n\n"
                "Le filtre est unifié et ses critères se combinent par ET logique: calque, "
                "type, couleur, liste de handles, fenêtre rectangulaire. Pour supprimer "
                "précisément, passez d'abord par query_entities et supprimez par handles.\n\n"
                "Un filtre VIDE désigne le dessin entier. Dans ce cas seulement, "
                "confirm_delete_all doit valoir true, sinon l'appel est refusé avec le code "
                "confirmation_required. Ne posez ce drapeau que si l'utilisateur a demandé "
                "d'effacer tout le dessin."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_selector_properties(),
                    "confirm_delete_all": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Obligatoire, et à true, pour effacer TOUT le dessin, c'est-à-dire "
                            "quand aucun autre critère n'est fourni. Sans effet si un critère "
                            "est donné. Suppression irréversible."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            destructive=True,
        ),
        ToolSpec(
            name="set_entity_color",
            title="Changer la couleur d'entités",
            description=(
                "Change la couleur d'une ou plusieurs entités désignées par leur handle.\n\n"
                "À utiliser pour corriger une entité posée sur le bon calque mais dans la "
                "mauvaise couleur, sans avoir à l'effacer puis à la redessiner.\n\n"
                "Les handles s'obtiennent d'abord par query_entities, ou dans la réponse de "
                "draw et build_structure: ils ne se devinent pas.\n\n"
                "Pour rendre une entité à la couleur de son calque, passez \"bylayer\", ce "
                "qui est la convention du dessin technique. L'index 0 est ByBlock et non "
                "noir; le noir est l'ACI 7, nommé \"white\".\n\n"
                "Chaque handle est traité séparément: la réponse indique lesquels ont été "
                "modifiés et lesquels ont échoué, avec la raison."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": MAX_BATCH_ITEMS,
                        "description": (
                            "Handles des entités à recolorer, obtenus par query_entities ou "
                            "par un lot d'écriture. Exemple: [\"1F3\", \"1F4\"]."
                        ),
                    },
                    "color": _color(),
                },
                "required": ["handles", "color"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="undo_last_batch",
            title="Annuler le dernier lot",
            description=(
                "Annule le dernier lot d'écriture, c'est-à-dire le dernier appel à draw ou "
                "à build_structure, en supprimant les entités qu'il a créées.\n\n"
                "À utiliser dès qu'un lot s'avère faux après l'avoir regardé avec "
                "render_view: c'est plus sûr que de supprimer à la main et cela ramène le "
                "dessin exactement à son état précédent.\n\n"
                "Portée limitée, à connaître avant d'y compter: seules les CRÉATIONS sont "
                "annulées. Une couleur changée par set_entity_color et une entité supprimée "
                "par delete_entities ne sont pas restaurées. Les appels successifs "
                "dépilent les lots du plus récent au plus ancien; sans lot à annuler, "
                "l'appel échoue au lieu de ne rien faire silencieusement."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            destructive=True,
        ),
        ToolSpec(
            name="run_cad_command",
            title="Exécuter une commande AutoCAD",
            description=(
                "DESTRUCTIF ET IRRÉVERSIBLE. Passerelle vers la ligne de commande "
                "d'AutoCAD: exécute une commande native du logiciel sur le dessin "
                "ouvert. undo_last_batch n'annule PAS ce que cette commande modifie, "
                "puisqu'elle n'est pas un lot de créations du serveur; seul l'annuler "
                "dans AutoCAD le peut.\n\n"
                "À utiliser pour les transformations qu'AutoCAD sait déjà faire mieux "
                "qu'une réimplémentation: décaler un contour, ajuster ou prolonger des "
                "murs, raccorder deux traits, tracer un contour fermé autour d'un "
                "point. Commandes autorisées, et elles seules: "
                + ", ".join(sorted(config.allowed_commands))
                + ". Toute autre commande est refusée avec le code invalid_parameter et "
                "la liste ci-dessus: la liste blanche existe parce que transmettre une "
                "chaîne libre à un logiciel de CAO revient à exécuter du code "
                "arbitraire sur la machine de l'utilisateur.\n\n"
                "INDISPONIBLE SUR LE BACKEND DXF, qui n'a pas d'interpréteur de "
                "commandes: l'appel échoue alors avec le code unsupported_operation, et "
                "c'est la situation normale hors d'un poste Windows où AutoCAD tourne. "
                "get_drawing_info dit dans can_run_commands si le moteur courant sait "
                "les exécuter: vérifiez-le avant de bâtir un plan là-dessus.\n\n"
                "La commande est envoyée telle quelle: le dessin n'est pas relu pour "
                "vérifier ce qu'elle a fait. Regardez le résultat avec render_view."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": sorted(config.allowed_commands),
                        "description": (
                            "Nom de la commande AutoCAD, sans espace ni préfixe. La "
                            "casse est libre, la comparaison à la liste blanche se fait "
                            "en majuscules. Exemple: \"OFFSET\"."
                        ),
                    },
                    "arguments": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {
                            "type": "string",
                            "description": (
                                "Une réponse à une invite de la commande, dans l'ordre "
                                "où AutoCAD les demande. Les coordonnées s'écrivent "
                                "\"x,y\" sans espace, dans l'unité du document."
                            ),
                        },
                        "description": (
                            "Réponses aux invites de la commande, dans l'ordre. Une "
                            "commande laissée en attente d'une invite bloquerait "
                            "AutoCAD: donnez toutes les réponses. Exemple pour un "
                            "décalage de 0,2 : [\"0.2\", \"2,1\", \"3,1\", \"\"]."
                        ),
                    },
                    "confirm_command": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Obligatoire, et à true. Sans lui, l'appel est refusé avec "
                            "le code confirmation_required. Ce drapeau existe parce que "
                            "la commande agit directement sur le document de "
                            "l'utilisateur et que le serveur ne sait pas la défaire."
                        ),
                    },
                },
                "required": ["command", "confirm_command"],
                "additionalProperties": False,
            },
            destructive=True,
        ),
    )


def tool_names(config: Config | None = None) -> tuple[str, ...]:
    """Noms du catalogue, dans l'ordre de déclaration."""
    catalogue = build_catalog(config or Config.from_env())
    return tuple(spec.name for spec in catalogue)
