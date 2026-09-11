"""Calques normalisés et correspondance des types de structure.

Le code historique devinait le calque par test de sous-chaîne sur un
dictionnaire non ordonné: ``light`` attrapait aussi ``skylight``, et ``table``
attrapait ``portable``. La correspondance est désormais explicite, par mot
entier, avec des synonymes déclarés en français et en anglais.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Index ACI par nom, utilisé par les outils et la configuration.
ACI: dict[str, int] = {
    "red": 1,
    "yellow": 2,
    "green": 3,
    "cyan": 4,
    "blue": 5,
    "magenta": 6,
    "white": 7,
    "gray": 8,
    "light_gray": 9,
    "bylayer": 256,
    "byblock": 0,
}

#: Synonymes français des couleurs.
_COLOR_ALIASES: dict[str, str] = {
    "rouge": "red",
    "jaune": "yellow",
    "vert": "green",
    "bleu": "blue",
    "gris": "gray",
    "blanc": "white",
    "noir": "white",  # l'ACI 7 s'affiche noir sur fond clair et blanc sur fond sombre
    "black": "white",
}


def color_index(value: str | int) -> int:
    """Convertit un nom de couleur ou un index en index ACI valide.

    L'index ACI 7 se nomme « blanc » mais s'affiche noir sur fond clair. Le code
    historique associait ``black`` à 0, qui vaut ByBlock, donc les entités
    demandées en noir héritaient d'une couleur arbitraire.
    """
    if isinstance(value, int):
        if not 0 <= value <= 256:
            from ..errors import InvalidParameter

            raise InvalidParameter(f"Index ACI hors domaine: {value}", valid="0 à 256")
        return value
    key = value.strip().lower()
    key = _COLOR_ALIASES.get(key, key)
    if key in ACI:
        return ACI[key]
    if key.isdigit():
        return color_index(int(key))
    from ..errors import InvalidParameter

    raise InvalidParameter(
        f"Couleur inconnue: {value!r}", supported=sorted(ACI) + sorted(_COLOR_ALIASES)
    )


@dataclass(frozen=True, slots=True)
class LayerSpec:
    name: str
    color: int
    description: str


#: Calques standard du projet, inspirés des conventions de dessin de bâtiment.
STANDARD_LAYERS: dict[str, LayerSpec] = {
    "walls": LayerSpec("WALLS", ACI["white"], "Murs porteurs et cloisons"),
    "doors": LayerSpec("DOORS", ACI["green"], "Portes et battants"),
    "windows": LayerSpec("WINDOWS", ACI["cyan"], "Fenêtres et baies"),
    "furniture": LayerSpec("FURNITURE", ACI["yellow"], "Mobilier et agencement"),
    "electrical": LayerSpec("ELECTRICAL", ACI["red"], "Appareillage électrique"),
    "plumbing": LayerSpec("PLUMBING", ACI["blue"], "Appareils sanitaires et réseaux"),
    "hvac": LayerSpec("HVAC", ACI["magenta"], "Ventilation et climatisation"),
    "structure": LayerSpec("STRUCTURE", ACI["gray"], "Poteaux, poutres, dalles"),
    "annotation": LayerSpec("ANNOTATION", ACI["white"], "Textes et repères"),
    "dimensions": LayerSpec("DIMENSIONS", ACI["light_gray"], "Cotation"),
    "hatch": LayerSpec("HATCH", ACI["gray"], "Remplissages et matériaux"),
    "site": LayerSpec("SITE", ACI["green"], "Terrain et aménagements extérieurs"),
    "utilities": LayerSpec("UTILITIES", ACI["red"], "Réseaux divers"),
}

#: Mot entier vers famille de calque. La recherche se fait par jeton exact,
#: jamais par sous-chaîne.
_KEYWORDS: dict[str, str] = {
    # murs
    "wall": "walls", "mur": "walls", "partition": "walls", "cloison": "walls",
    # ouvertures
    "door": "doors", "porte": "doors", "opening": "doors",
    "window": "windows", "fenetre": "windows", "fenêtre": "windows", "baie": "windows",
    # mobilier
    "furniture": "furniture", "mobilier": "furniture", "meuble": "furniture",
    "chair": "furniture", "chaise": "furniture", "table": "furniture",
    "bed": "furniture", "lit": "furniture", "sofa": "furniture", "canape": "furniture",
    # électricité
    "electrical": "electrical", "electrique": "electrical", "électrique": "electrical",
    "outlet": "electrical", "prise": "electrical", "switch": "electrical",
    "interrupteur": "electrical", "light": "electrical", "luminaire": "electrical",
    # plomberie
    "plumbing": "plumbing", "plomberie": "plumbing", "toilet": "plumbing",
    "wc": "plumbing", "sink": "plumbing", "evier": "plumbing", "évier": "plumbing",
    "lavabo": "plumbing", "douche": "plumbing", "shower": "plumbing",
    # ventilation
    "hvac": "hvac", "vent": "hvac", "duct": "hvac", "gaine": "hvac",
    "ventilation": "hvac", "clim": "hvac",
    # structure
    "structure": "structure", "beam": "structure", "poutre": "structure",
    "column": "structure", "poteau": "structure", "slab": "structure", "dalle": "structure",
    # annotation
    "text": "annotation", "texte": "annotation", "label": "annotation",
    "annotation": "annotation", "etiquette": "annotation",
    "dimension": "dimensions", "cote": "dimensions", "cotation": "dimensions",
    # divers
    "hatch": "hatch", "hachure": "hatch",
    "room": "walls", "piece": "walls", "pièce": "walls", "salle": "walls",
    "site": "site", "tree": "site", "arbre": "site", "terrain": "site",
    "utility": "utilities", "reseau": "utilities", "réseau": "utilities",
}


def _tokens(text: str) -> list[str]:
    """Découpe en mots, en traitant tirets et soulignés comme des séparateurs."""
    cleaned = "".join(c if c.isalnum() else " " for c in text.lower())
    return cleaned.split()


def layer_for(structure_type: str, *, default: str = "0") -> str:
    """Nom de calque correspondant à un type de structure.

    La correspondance porte sur des mots entiers, ce qui évite les faux
    positifs par sous-chaîne. Le premier mot reconnu gagne, ce qui rend
    ``porte de placard`` cohérent avec ``porte``.
    """
    for token in _tokens(structure_type):
        family = _KEYWORDS.get(token)
        if family is not None:
            return STANDARD_LAYERS[family].name
    return default


def spec_for_layer(layer_name: str) -> LayerSpec | None:
    """Retrouve la définition standard d'un calque par son nom."""
    upper = layer_name.upper()
    for spec in STANDARD_LAYERS.values():
        if spec.name == upper:
            return spec
    return None
