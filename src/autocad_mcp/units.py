"""Unités de dessin et échelles.

Le code historique codait en dur une épaisseur de mur de 0.1 et un décalage de
fenêtre de 0.05, sans jamais dire dans quelle unité. Dans un dessin en
millimètres ces éléments sont invisibles. Toute longueur du projet est désormais
exprimée dans l'unité du document, et les valeurs par défaut en dérivent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import InvalidParameter


class Unit(str, Enum):
    """Unité de longueur du document."""

    MILLIMETER = "mm"
    CENTIMETER = "cm"
    METER = "m"
    INCH = "in"
    FOOT = "ft"


#: Longueur d'une unité exprimée en mètres.
_IN_METERS: dict[Unit, float] = {
    Unit.MILLIMETER: 0.001,
    Unit.CENTIMETER: 0.01,
    Unit.METER: 1.0,
    Unit.INCH: 0.0254,
    Unit.FOOT: 0.3048,
}

#: Code $INSUNITS du DXF, utilisé par les backends pour marquer le document.
INSUNITS: dict[Unit, int] = {
    Unit.MILLIMETER: 4,
    Unit.CENTIMETER: 5,
    Unit.METER: 6,
    Unit.INCH: 1,
    Unit.FOOT: 2,
}


def parse_unit(value: str | Unit) -> Unit:
    """Convertit un libellé d'unité en ``Unit``.

    Accepte les formes courtes et les noms complets, en français comme en anglais.
    """
    if isinstance(value, Unit):
        return value
    key = value.strip().lower()
    aliases = {
        "mm": Unit.MILLIMETER,
        "millimeter": Unit.MILLIMETER,
        "millimetre": Unit.MILLIMETER,
        "cm": Unit.CENTIMETER,
        "centimeter": Unit.CENTIMETER,
        "centimetre": Unit.CENTIMETER,
        "m": Unit.METER,
        "meter": Unit.METER,
        "metre": Unit.METER,
        "in": Unit.INCH,
        "inch": Unit.INCH,
        "pouce": Unit.INCH,
        "ft": Unit.FOOT,
        "foot": Unit.FOOT,
        "feet": Unit.FOOT,
        "pied": Unit.FOOT,
    }
    try:
        return aliases[key]
    except KeyError:
        raise InvalidParameter(
            f"Unité inconnue: {value!r}",
            supported=sorted({u.value for u in Unit}),
        ) from None


def convert(value: float, source: Unit, target: Unit) -> float:
    """Convertit une longueur d'une unité vers une autre."""
    if source is target:
        return float(value)
    return float(value) * _IN_METERS[source] / _IN_METERS[target]


def meters_to(value_m: float, target: Unit) -> float:
    """Convertit une longueur exprimée en mètres vers l'unité du document."""
    return float(value_m) / _IN_METERS[target]


@dataclass(frozen=True, slots=True)
class Defaults:
    """Valeurs par défaut exprimées dans l'unité du document.

    Les constantes de référence sont définies en mètres, seule échelle
    physiquement signifiante, puis converties. Un mur de vingt centimètres
    reste un mur de vingt centimètres que le dessin soit en mm ou en pieds.
    """

    unit: Unit

    #: Épaisseur de mur courante en maçonnerie.
    WALL_THICKNESS_M = 0.20
    #: Épaisseur d'une cloison légère.
    PARTITION_THICKNESS_M = 0.07
    #: Largeur de passage d'une porte intérieure.
    DOOR_WIDTH_M = 0.90
    #: Hauteur d'allège, utilisée pour le tracé symbolique des fenêtres.
    WINDOW_FRAME_M = 0.05
    #: Hauteur de texte d'annotation lisible à l'échelle du centième.
    TEXT_HEIGHT_M = 0.25
    #: Hauteur d'un attribut de bloc. Plus petite qu'une annotation courante:
    #: un repère posé sur une chaise de quarante-cinq centimètres doit tenir
    #: dans le symbole au lieu de le recouvrir.
    ATTRIBUTE_HEIGHT_M = 0.15
    #: Taille du symbole d'extrémité d'une cote, le « tiret d'architecte ».
    DIM_ARROW_M = 0.15
    #: Dépassement de la ligne d'attache au-delà de la ligne de cote.
    DIM_EXTENSION_M = 0.12
    #: Retrait de la ligne d'attache par rapport au point mesuré, pour que la
    #: cote ne vienne pas toucher la géométrie qu'elle mesure.
    DIM_OFFSET_M = 0.10
    #: Écart entre le texte de cote et sa ligne.
    DIM_TEXT_GAP_M = 0.08
    #: Nombre de décimales affichées par une cote. Sans dimension: c'est une
    #: convention de lecture, pas une longueur.
    DIM_DECIMALS = 2

    @property
    def wall_thickness(self) -> float:
        return meters_to(self.WALL_THICKNESS_M, self.unit)

    @property
    def partition_thickness(self) -> float:
        return meters_to(self.PARTITION_THICKNESS_M, self.unit)

    @property
    def door_width(self) -> float:
        return meters_to(self.DOOR_WIDTH_M, self.unit)

    @property
    def window_frame(self) -> float:
        return meters_to(self.WINDOW_FRAME_M, self.unit)

    @property
    def text_height(self) -> float:
        return meters_to(self.TEXT_HEIGHT_M, self.unit)

    @property
    def attribute_height(self) -> float:
        return meters_to(self.ATTRIBUTE_HEIGHT_M, self.unit)

    @property
    def dim_arrow_size(self) -> float:
        return meters_to(self.DIM_ARROW_M, self.unit)

    @property
    def dim_extension(self) -> float:
        return meters_to(self.DIM_EXTENSION_M, self.unit)

    @property
    def dim_offset(self) -> float:
        return meters_to(self.DIM_OFFSET_M, self.unit)

    @property
    def dim_text_gap(self) -> float:
        return meters_to(self.DIM_TEXT_GAP_M, self.unit)

    @property
    def tolerance(self) -> float:
        """Tolérance de comparaison géométrique, à l'échelle du document.

        Un dixième de millimètre, exprimé dans l'unité courante.
        """
        return meters_to(0.0001, self.unit)
