"""Fonctions métier appelables depuis une macro.

Registre de données, sur le patron de la bibliothèque de blocs: une entrée
ajoutée ici apparaît d'elle-même dans le contrat remis au modèle, sans que
personne n'ait à recopier un nom.

**Chaque entrée élargit le périmètre.** Une macro n'exécute aucun code du
modèle, mais elle appelle ces fonctions avec des arguments qu'il a choisis.
N'inscrire ici que des fonctions pures qui rendent des opérations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from ..ops import architecture, blocks, primitives


@dataclass(frozen=True, slots=True)
class MacroOp:
    """Une fonction offerte aux macros, avec ce qu'il faut pour la décrire."""

    name: str
    func: Callable[..., list[Any]]
    summary: str
    #: Arguments attendus, dans l'ordre, avant les mots-clés.
    positional: tuple[str, ...]
    #: Vrai si la fonction réclame les valeurs par défaut du document.
    wants_defaults: bool = True


_ENTREES: Final[tuple[MacroOp, ...]] = (
    MacroOp(
        "line",
        primitives.line,
        "Segment droit entre deux points.",
        ("start", "end"),
        wants_defaults=False,
    ),
    MacroOp(
        "polyline",
        primitives.polyline,
        "Polyligne ouverte ou fermée, passant par une suite de points.",
        ("points",),
        wants_defaults=False,
    ),
    MacroOp(
        "rectangle",
        primitives.rectangle,
        "Rectangle tracé comme une polyligne fermée, donc hachurable.",
        ("corner1", "corner2"),
    ),
    MacroOp(
        "circle",
        primitives.circle,
        "Cercle de centre et rayon donnés.",
        ("center", "radius"),
        wants_defaults=False,
    ),
    MacroOp(
        "arc",
        primitives.arc,
        "Arc de cercle, angles en degrés.",
        ("center", "radius", "start_deg", "end_deg"),
        wants_defaults=False,
    ),
    MacroOp(
        "text",
        primitives.text,
        "Texte sur une ligne, hauteur déduite de l'unité si absente.",
        ("position", "content"),
    ),
    MacroOp(
        "hatch",
        primitives.hatch,
        "Hachure délimitée par un ou plusieurs contours fermés.",
        ("boundaries",),
        wants_defaults=False,
    ),
    MacroOp(
        "wall_network",
        architecture.wall_network,
        "Réseau de murs raccordés aux angles, avec baies qui percent la maçonnerie.",
        ("points",),
    ),
    MacroOp(
        "room",
        architecture.room,
        "Pièce rectangulaire: murs raccordés plus une étiquette de surface.",
        ("corner1", "corner2"),
    ),
    MacroOp(
        "label",
        architecture.label,
        "Étiquette centrée, déposée sur le calque d'annotation.",
        ("position", "text"),
    ),
    MacroOp(
        "block",
        blocks.insert,
        "Insère un symbole de la bibliothèque, en le définissant au besoin.",
        ("key", "point"),
    ),
)

#: Indexé par nom, pour un aiguillage sans recherche dynamique d'attribut.
CALLABLES: Final[dict[str, MacroOp]] = {entree.name: entree for entree in _ENTREES}


def contract() -> list[dict[str, Any]]:
    """Décrit les fonctions disponibles, pour le contrat remis au modèle.

    Calculé depuis le registre: il ne peut pas diverger du code réel.
    """
    return [
        {
            "call": entree.name,
            "summary": entree.summary,
            "positional": list(entree.positional),
        }
        for entree in _ENTREES
    ]
