"""Cotation automatique: coter un plan sans placer chaque cote à la main.

Coter est le travail le plus fastidieux d'un plan et le plus mécanique: les
mesures existent déjà dans la géométrie, seul leur placement demande du soin.
Ce module reprend les points d'un réseau de murs et en déduit les lignes de
cote, leur déport et leur ordre.

## Ce que place un cotateur, et dans quel ordre

Un plan coté se lit par couches, de la plus fine à la plus générale:

* la **première ligne** porte les percements, tableau par tableau ;
* la **deuxième** porte les trumeaux et les refends, d'axe en axe ;
* la **troisième** porte la dimension hors tout de la façade.

Chaque couche s'éloigne un peu plus du bâtiment, sinon les chiffres se
chevauchent et le plan devient illisible. C'est cet échelonnement, plus que la
mesure elle-même, qui fait la différence entre des cotes posées et un plan coté.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import InvalidParameter
from ..geometry import angle_of, distance
from ..model.layers import STANDARD_LAYERS
from ..model.ops import AddDimAligned, EnsureLayer, Operation, Point2, Style
from ..units import Defaults
from .architecture import Opening

__all__ = ["Chain", "dimension_run", "dimension_walls"]

#: Écart entre deux lignes de cote successives, en hauteurs de texte. En deçà,
#: les chiffres d'une ligne touchent les traits d'attache de la suivante.
_ESPACEMENT_LIGNES = 2.6

#: Déport de la première ligne par rapport à la face du mur, en hauteurs de
#: texte. La cote doit respirer sans se détacher de ce qu'elle mesure.
_DEPORT_INITIAL = 3.0


@dataclass(frozen=True, slots=True)
class Chain:
    """Une ligne de cote et les points qu'elle enchaîne.

    ``rank`` est le rang d'éloignement: zéro pour la ligne la plus proche du
    bâtiment, un pour la suivante, et ainsi de suite.
    """

    stations: tuple[float, ...]
    rank: int
    label: str


def _stations_percements(
    longueur: float, baies: list[Opening], tol: float
) -> tuple[float, ...]:
    """Abscisses des tableaux, c'est-à-dire les bords de chaque baie."""
    points: list[float] = [0.0]
    for baie in sorted(baies, key=lambda b: b.position):
        centre = baie.position * longueur
        demi = baie.width / 2.0
        points.append(max(0.0, centre - demi))
        points.append(min(longueur, centre + demi))
    points.append(longueur)
    return _nettoyer(points, tol)


def _stations_axes(longueur: float, baies: list[Opening], tol: float) -> tuple[float, ...]:
    """Abscisses des axes de baies, la lecture d'implantation."""
    points = [0.0] + [b.position * longueur for b in sorted(baies, key=lambda b: b.position)]
    points.append(longueur)
    return _nettoyer(points, tol)


def _nettoyer(points: list[float], tol: float) -> tuple[float, ...]:
    """Trie, borne et supprime les doublons à la tolérance près."""
    retenus: list[float] = []
    for valeur in sorted(points):
        if not retenus or valeur - retenus[-1] > tol:
            retenus.append(valeur)
    return tuple(retenus)


def dimension_run(
    start: Point2,
    end: Point2,
    defaults: Defaults,
    *,
    openings: list[Opening] | None = None,
    side: int = 1,
    thickness: float | None = None,
    overall: bool = True,
    layer: str | None = None,
) -> list[Operation]:
    """Cote un mur: ses percements, ses axes, puis sa longueur hors tout.

    ``side`` vaut 1 pour coter à gauche du sens de parcours, -1 pour coter à
    droite. Sur un contour parcouru dans le sens direct, -1 place les cotes à
    l'extérieur du bâtiment, ce qui est la convention.
    """
    longueur = distance(start, end)
    if longueur <= defaults.tolerance:
        raise InvalidParameter("Mur de longueur nulle, rien à coter", length=longueur)

    baies = list(openings or [])
    chaines: list[Chain] = []

    if baies:
        chaines.append(Chain(_stations_percements(longueur, baies, defaults.tolerance), 0, "baies"))
        if len(baies) > 1:
            chaines.append(Chain(_stations_axes(longueur, baies, defaults.tolerance), 1, "axes"))
    if overall:
        chaines.append(Chain((0.0, longueur), len(chaines), "hors tout"))

    return _emettre(start, end, chaines, defaults, side, thickness, layer)


def _tete_en_bas(p1: Point2, p2: Point2, defaults: Defaults) -> bool:
    """Vrai si le texte de cote se lirait à l'envers dans ce sens.

    Une cote alignée oriente son texte sur la droite qui joint ses deux points.
    Prise dans le mauvais sens, elle s'écrit donc tête en bas: c'est ce qui
    arrive sur la façade nord et sur le pignon ouest d'un contour relevé dans
    le sens direct, soit la moitié d'un plan.

    Le remède est celui du dessin technique: une cote se lit de la gauche vers
    la droite, ou du bas vers le haut. Échanger les deux points ne change pas
    la mesure, seulement le sens de lecture.
    """
    angle = math.degrees(angle_of(p1, p2, tol=defaults.tolerance)) % 360.0
    # La plage ]90, 270] est celle où le texte bascule. La borne haute est
    # exclue pour qu'une verticale montante reste lue de bas en haut.
    return 90.0 < angle <= 270.0


def _emettre(
    start: Point2,
    end: Point2,
    chaines: list[Chain],
    defaults: Defaults,
    side: int,
    thickness: float | None,
    layer: str | None,
) -> list[Operation]:
    """Traduit des chaînes d'abscisses en cotes placées."""
    spec = STANDARD_LAYERS["dimensions"]
    nom_calque = layer or spec.name
    style = Style(layer=nom_calque)
    ops: list[Operation] = [
        EnsureLayer(name=nom_calque, color=spec.color, description=spec.description)
    ]

    longueur = distance(start, end)
    ux, uy = (end[0] - start[0]) / longueur, (end[1] - start[1]) / longueur
    # Normale gauche, puis choix du côté.
    nx, ny = -uy * side, ux * side

    demi_mur = (defaults.wall_thickness if thickness is None else float(thickness)) / 2.0
    hauteur = defaults.text_height

    for chaine in chaines:
        deport = demi_mur + hauteur * (_DEPORT_INITIAL + _ESPACEMENT_LIGNES * chaine.rank)
        for gauche, droite in zip(chaine.stations, chaine.stations[1:], strict=False):
            if droite - gauche <= defaults.tolerance:
                continue
            p1 = (start[0] + ux * gauche, start[1] + uy * gauche)
            p2 = (start[0] + ux * droite, start[1] + uy * droite)
            milieu = (gauche + droite) / 2.0
            ancre = (
                start[0] + ux * milieu + nx * deport,
                start[1] + uy * milieu + ny * deport,
            )
            if _tete_en_bas(p1, p2, defaults):
                p1, p2 = p2, p1
            ops.append(AddDimAligned(p1=p1, p2=p2, location=ancre, style=style))

    return ops


def dimension_walls(
    points: list[Point2],
    defaults: Defaults,
    *,
    openings: list[Opening] | None = None,
    closed: bool = False,
    thickness: float | None = None,
    segments: list[int] | None = None,
    outside: bool = True,
    layer: str | None = None,
) -> list[Operation]:
    """Cote un réseau de murs entier, façade par façade.

    Prend les mêmes points et les mêmes baies que ``wall_network``, dont c'est
    le pendant coté: les deux décrivent le même bâtiment, l'un en traits,
    l'autre en chiffres.

    ``segments`` restreint la cotation à certaines façades, ce qui évite
    d'encombrer un plan qui n'a besoin d'être coté que d'un côté. ``outside``
    place les lignes à l'extérieur du contour.
    """
    if len(points) < 2:
        raise InvalidParameter(
            "Un réseau de murs demande au moins deux points", count=len(points)
        )

    total = len(points) if closed else len(points) - 1
    voulus = list(range(total)) if segments is None else sorted(set(segments))
    for rang in voulus:
        if not 0 <= rang < total:
            raise InvalidParameter(
                "Façade hors de l'enfilade", segment=rang, segments=total
            )

    par_segment: dict[int, list[Opening]] = {}
    for baie in openings or ():
        par_segment.setdefault(baie.segment, []).append(baie)

    # Sur un contour fermé relevé dans le sens direct, la droite du parcours
    # regarde dehors. Coter dedans recouvrirait les pièces de chiffres.
    cote = -1 if outside else 1
    if closed and len(points) > 2:
        from ..geometry import polygon_is_clockwise

        if polygon_is_clockwise(points, tol=defaults.tolerance):
            cote = -cote

    ops: list[Operation] = []
    vu_calque = False
    for rang in voulus:
        a = points[rang]
        b = points[(rang + 1) % len(points)]
        if distance(a, b) <= defaults.tolerance:
            continue
        produites = dimension_run(
            a, b, defaults,
            openings=par_segment.get(rang, []),
            side=cote,
            thickness=thickness,
            layer=layer,
        )
        # Un seul EnsureLayer pour tout le tour.
        ops.extend(produites if not vu_calque else produites[1:])
        vu_calque = True

    return ops
