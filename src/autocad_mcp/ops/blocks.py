"""Bibliothèque de blocs: porte, fenêtre, sanitaires, électricité, mobilier.

Fonctions **pures**. Elles ne dessinent pas, elles décident quoi dessiner et
rendent des opérations. Aucun backend n'est importé ici, c'est la règle qui rend
le projet testable sans AutoCAD, et `tests/test_architecture.py` la vérifie.

Ce qui sépare un dessin d'une maquette exploitable, c'est le bloc. Une porte
insérée cent fois reste **une** définition: on la corrige une fois, les cent
occurrences suivent. Et chaque occurrence porte un attribut de repère, donc les
cent portes se comptent, se listent et se chiffrent. C'est ce que le projet
n'avait pas: `AddBlockRef` savait insérer, rien ne savait définir.

**Tout est dimensionné en grandeurs réelles.** Les tailles sont écrites en
mètres dans ce module, seule échelle physiquement signifiante, puis converties
vers l'unité du document par la même mécanique que `units.Defaults`. Un lavabo
fait cinquante-cinq centimètres de large que le dessin soit en millimètres, en
mètres ou en pieds. Là où `Defaults` connaît déjà la grandeur — largeur de
porte, épaisseur de mur, hauteur d'attribut — c'est elle qui décide, jamais une
constante locale.

**Chaque bloc va sur le calque de sa nature**, obtenu par `model.layers`: un WC
sur `PLUMBING`, une prise sur `ELECTRICAL`, une chaise sur `FURNITURE`.
L'attribut de repère reste sur ce même calque, et non sur `ANNOTATION`: geler le
calque électrique doit faire disparaître le symbole **et** son repère, sans quoi
le plan garde des étiquettes orphelines.

**Repère local de chaque symbole.** Le point de base est le point que l'on vient
poser sur le plan, et l'axe X du bloc est celui qu'on aligne sur le support.
Pour ce qui se pose contre un mur — porte, fenêtre, WC, lavabo, prise — le mur
est l'axe X et la pièce est du côté des Y positifs. Une rotation à l'insertion
suffit donc à suivre n'importe quel mur.

**Les repères restent horizontaux.** Le format fait suivre à un attribut la
rotation du bloc qui le porte: un évier posé à cent quatre-vingts degrés
afficherait son repère à l'envers, un WC posé à quatre-vingt-dix degrés le
coucherait sur le flanc. Un plan dont les repères se lisent la tête en bas n'est
pas livrable, alors tous les repères de cette bibliothèque portent
``keep_upright``: le backend les redresse après la pose, sans les déplacer.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import InvalidParameter
from ..geometry import rectangle_points
from ..model.layers import STANDARD_LAYERS, LayerSpec, layer_for, spec_for_layer
from ..model.ops import (
    AddArc,
    AddBlockRef,
    AddCircle,
    AddLine,
    AddPolyline,
    AttributeDef,
    DefineBlock,
    EnsureLayer,
    Operation,
    Point2,
    Style,
)
from ..units import Defaults, meters_to

__all__ = [
    "LIBRARY",
    "MARK_TAG",
    "NAME_PREFIX",
    "block_name",
    "define",
    "define_library",
    "insert",
    "spec",
]

#: Préfixe des noms de blocs de la bibliothèque. Un plan reçu d'un tiers a
#: souvent déjà un bloc nommé ``PORTE`` ou ``WC``; le préfixe évite d'entrer en
#: collision avec lui, ce qui redéfinirait silencieusement son symbole.
NAME_PREFIX = "MCP_"

#: Étiquette de l'attribut de repère, porté par tous les blocs. C'est la clé
#: d'une nomenclature: sans elle, une occurrence ne dit que sa présence.
MARK_TAG = "REPERE"

#: Taille de lecture d'un symbole électrique, en mètres. Une prise mesure huit
#: centimètres dans le mur, mais on ne la dessine pas à cette taille: le symbole
#: électrique est une convention de lecture, dimensionnée pour rester visible à
#: côté de son repère plutôt que pour représenter l'objet.
SYMBOL_M = 0.16


def _m(value_m: float, defaults: Defaults) -> float:
    """Convertit une longueur écrite en mètres vers l'unité du document.

    Même mécanique que les propriétés de :class:`units.Defaults`, appliquée aux
    dimensions propres au catalogue: un évier de cent centimètres n'est pas une
    valeur par défaut du projet, c'est une donnée de cette bibliothèque.
    """
    return meters_to(value_m, defaults.unit)


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """Fiche d'un bloc du catalogue.

    ``nature`` est le mot passé à :func:`model.layers.layer_for`: c'est lui, et
    non un nom de calque en dur, qui décide où va le symbole. ``mark`` est la
    valeur de repère proposée, point de départ d'une nomenclature.
    """

    key: str
    nature: str
    mark: str
    description: str
    #: Trace la géométrie dans le repère local, dans l'unité du document.
    draw: Callable[[Defaults, Style], list[Operation]]
    #: Position du repère dans le repère local, en mètres.
    label_m: Point2

    @property
    def name(self) -> str:
        """Nom DXF du bloc."""
        return f"{NAME_PREFIX}{self.key.upper()}"

    def layer(self) -> LayerSpec:
        """Calque du symbole, déduit de sa nature."""
        name = layer_for(self.nature)
        found = spec_for_layer(name)
        if found is None:  # pragma: no cover - garde-fou, jamais atteint
            raise InvalidParameter(
                f"Nature sans calque normalisé: {self.nature!r}",
                supported=sorted(STANDARD_LAYERS),
            )
        return found


# ---------------------------------------------------------------------------
# Tracés élémentaires
# ---------------------------------------------------------------------------


def _rect(
    corner1: Point2, corner2: Point2, defaults: Defaults, style: Style
) -> AddPolyline:
    """Rectangle en polyligne fermée, jamais en quatre segments séparés.

    Quatre segments indépendants ne sont ni sélectionnables d'un clic, ni
    hachurables, ni mesurables: c'est le défaut historique du projet.
    """
    return AddPolyline(
        points=rectangle_points(corner1, corner2, tol=defaults.tolerance),
        closed=True,
        style=style,
    )


def _line(a: Point2, b: Point2, style: Style) -> AddLine:
    return AddLine(start=(*a, 0.0), end=(*b, 0.0), style=style)


def _circle(center: Point2, radius: float, style: Style) -> AddCircle:
    return AddCircle(center=(*center, 0.0), radius=radius, style=style)


# ---------------------------------------------------------------------------
# Les symboles
# ---------------------------------------------------------------------------


def _door(d: Defaults, style: Style) -> list[Operation]:
    """Porte simple: seuil, battant et arc de débattement.

    Charnière à l'origine, mur suivant l'axe X, ouverture vers les Y positifs.
    La largeur est celle d'un passage courant, donnée par ``Defaults``.
    """
    width = d.door_width
    leaf = _m(0.04, d)
    return [
        # Seuil: la largeur du passage réellement percé dans le mur.
        _line((0.0, 0.0), (width, 0.0), style),
        # Battant ouvert à angle droit.
        _rect((0.0, 0.0), (leaf, width), d, style),
        # Débattement, de la position fermée à la position ouverte.
        AddArc(
            center=(0.0, 0.0, 0.0),
            radius=width,
            start_angle=0.0,
            end_angle=math.pi / 2.0,
            style=style,
        ),
    ]


def _window(d: Defaults, style: Style) -> list[Operation]:
    """Fenêtre: dormant sur l'épaisseur du mur, et les deux traits de vitrage.

    Origine à l'extrémité gauche de la baie, sur l'axe du mur. L'épaisseur est
    celle d'un mur courant et l'écart du vitrage en dérive, jamais d'une
    constante sans unité comme le faisait l'ancien code.
    """
    length = _m(1.20, d)
    thickness = d.wall_thickness
    half = thickness / 2.0
    quarter = thickness / 4.0
    return [
        _rect((0.0, -half), (length, half), d, style),
        _line((0.0, quarter), (length, quarter), style),
        _line((0.0, -quarter), (length, -quarter), style),
    ]


def _toilet(d: Defaults, style: Style) -> list[Operation]:
    """WC: réservoir contre le mur, cuvette vers la pièce.

    Origine au milieu du réservoir, contre le mur ; encombrement de trente-six
    centimètres sur soixante-deux, ce qui est la cuvette suspendue courante.
    """
    return [
        _rect((_m(-0.18, d), 0.0), (_m(0.18, d), _m(0.16, d)), d, style),
        AddPolyline(
            points=tuple(
                (_m(x, d), _m(y, d))
                for x, y in (
                    (-0.13, 0.16),
                    (-0.17, 0.26),
                    (-0.18, 0.38),
                    (-0.15, 0.50),
                    (-0.08, 0.59),
                    (0.0, 0.62),
                    (0.08, 0.59),
                    (0.15, 0.50),
                    (0.18, 0.38),
                    (0.17, 0.26),
                    (0.13, 0.16),
                )
            ),
            closed=True,
            style=style,
        ),
    ]


def _basin(d: Defaults, style: Style) -> list[Operation]:
    """Lavabo: plan, vasque et robinet. Origine au milieu du bord mural.

    Le robinet se place **entre** le mur et la vasque, sans la chevaucher: deux
    cercles sécants se liraient comme une seule forme au tracé.
    """
    half = _m(0.275, d)
    return [
        _rect((-half, 0.0), (half, _m(0.42, d)), d, style),
        _circle((0.0, _m(0.245, d)), _m(0.145, d), style),
        _circle((0.0, _m(0.05, d)), _m(0.026, d), style),
    ]


def _shower(d: Defaults, style: Style) -> list[Operation]:
    """Douche: receveur carré, diagonales de pente et bonde.

    Origine au coin arrière gauche du receveur, quatre-vingt-dix centimètres de
    côté, dimension d'un bac courant.
    """
    side = _m(0.90, d)
    mid = side / 2.0
    return [
        _rect((0.0, 0.0), (side, side), d, style),
        _line((0.0, 0.0), (side, side), style),
        _line((0.0, side), (side, 0.0), style),
        _circle((mid, mid), _m(0.05, d), style),
    ]


def _sink(d: Defaults, style: Style) -> list[Operation]:
    """Évier: plan de travail, cuve, bonde, robinet et égouttoir.

    Origine au milieu du bord mural. Un mètre sur soixante, le module de
    cuisine standard.
    """
    return [
        _rect((_m(-0.50, d), 0.0), (_m(0.50, d), _m(0.60, d)), d, style),
        _rect((_m(-0.42, d), _m(0.10, d)), (_m(-0.02, d), _m(0.50, d)), d, style),
        _circle((_m(-0.22, d), _m(0.30, d)), _m(0.03, d), style),
        _circle((_m(-0.22, d), _m(0.05, d)), _m(0.028, d), style),
        _line((_m(0.06, d), _m(0.16, d)), (_m(0.44, d), _m(0.16, d)), style),
        _line((_m(0.06, d), _m(0.44, d)), (_m(0.44, d), _m(0.44, d)), style),
    ]


def _outlet(d: Defaults, style: Style) -> list[Operation]:
    """Prise de courant: demi-cercle appuyé sur le mur, tige vers la pièce.

    Origine sur la face du mur, symbole tourné vers les Y positifs, à la taille
    de lecture de :data:`SYMBOL_M`.
    """
    radius = _m(SYMBOL_M, d)
    return [
        AddArc(
            center=(0.0, 0.0, 0.0),
            radius=radius,
            start_angle=0.0,
            end_angle=math.pi,
            style=style,
        ),
        _line((-radius, 0.0), (radius, 0.0), style),
        _line((0.0, 0.0), (0.0, radius * 1.75), style),
    ]


def _switch(d: Defaults, style: Style) -> list[Operation]:
    """Interrupteur: point d'appui sur le mur et basculeur en oblique."""
    radius = _m(SYMBOL_M * 0.32, d)
    tip = _m(SYMBOL_M * 1.6, d) * math.sqrt(0.5)
    bar = _m(SYMBOL_M * 0.38, d)
    start = radius * math.sqrt(0.5)
    return [
        _circle((0.0, 0.0), radius, style),
        _line((start, start), (tip, tip), style),
        _line((tip - bar, tip + bar), (tip + bar, tip - bar), style),
    ]


def _light(d: Defaults, style: Style) -> list[Operation]:
    """Point lumineux: cercle barré d'une croix, symbole du luminaire au plafond."""
    radius = _m(SYMBOL_M * 0.85, d)
    reach = radius * 1.7
    return [
        _circle((0.0, 0.0), radius, style),
        _line((-reach, 0.0), (reach, 0.0), style),
        _line((0.0, -reach), (0.0, reach), style),
    ]


def _bed(width_m: float) -> Callable[[Defaults, Style], list[Operation]]:
    """Fabrique le tracé d'un lit d'une largeur donnée.

    Un lit simple et un lit double ne diffèrent que par leur largeur et par le
    nombre d'oreillers: le tracé est donc écrit une fois.
    """

    def draw(d: Defaults, style: Style) -> list[Operation]:
        width = _m(width_m, d)
        length = _m(2.00, d)
        margin = _m(0.08, d)
        pillow_depth = _m(0.38, d)
        pillow_top = _m(0.10, d)
        double = width_m >= 1.20
        ops: list[Operation] = [
            _rect((0.0, 0.0), (width, length), d, style),
            # Pli de la couette, à hauteur de poitrine.
            _line((0.0, _m(0.70, d)), (width, _m(0.70, d)), style),
        ]
        if double:
            gap = _m(0.04, d)
            half = (width - 2.0 * margin - gap) / 2.0
            ops.append(
                _rect(
                    (margin, pillow_top),
                    (margin + half, pillow_top + pillow_depth),
                    d,
                    style,
                )
            )
            ops.append(
                _rect(
                    (margin + half + gap, pillow_top),
                    (width - margin, pillow_top + pillow_depth),
                    d,
                    style,
                )
            )
        else:
            ops.append(
                _rect(
                    (margin, pillow_top),
                    (width - margin, pillow_top + pillow_depth),
                    d,
                    style,
                )
            )
        return ops

    return draw


def _table(d: Defaults, style: Style) -> list[Operation]:
    """Table rectangulaire de quatre à six places. Origine au centre du plateau."""
    return [
        _rect((_m(-0.70, d), _m(-0.40, d)), (_m(0.70, d), _m(0.40, d)), d, style)
    ]


def _chair(d: Defaults, style: Style) -> list[Operation]:
    """Chaise: assise et dossier. Origine au centre de l'assise, dossier en -Y."""
    half = _m(0.225, d)
    return [
        _rect((-half, _m(-0.20, d)), (half, _m(0.25, d)), d, style),
        _rect((-half, _m(-0.26, d)), (half, _m(-0.20, d)), d, style),
    ]


#: Le catalogue. Minimal et utilisable: de quoi meubler un logement, l'équiper
#: et le raccorder, sans prétendre couvrir un projet d'exécution.
_SPECS: tuple[BlockSpec, ...] = (
    BlockSpec("door", "porte", "P", "Porte simple avec battant", _door, (0.30, 0.30)),
    BlockSpec("window", "fenetre", "F", "Fenêtre à un vantail", _window, (0.60, 0.26)),
    BlockSpec("toilet", "wc", "WC", "Cuvette de WC", _toilet, (0.0, 0.80)),
    BlockSpec("basin", "lavabo", "LV", "Lavabo", _basin, (0.0, 0.58)),
    BlockSpec("shower", "douche", "DO", "Bac de douche", _shower, (0.45, 1.06)),
    BlockSpec("sink", "evier", "EV", "Évier de cuisine", _sink, (0.0, 0.76)),
    BlockSpec("outlet", "prise", "PC", "Prise de courant", _outlet, (0.0, 0.46)),
    BlockSpec("switch", "interrupteur", "IN", "Interrupteur", _switch, (0.0, 0.42)),
    BlockSpec("light", "luminaire", "PL", "Point lumineux", _light, (0.0, 0.42)),
    BlockSpec(
        "bed_single", "lit", "L1", "Lit une place", _bed(0.90), (0.45, 1.35)
    ),
    BlockSpec(
        "bed_double", "lit", "L2", "Lit deux places", _bed(1.40), (0.70, 1.35)
    ),
    BlockSpec("table", "table", "TA", "Table", _table, (0.0, 0.0)),
    BlockSpec("chair", "chaise", "CH", "Chaise", _chair, (0.0, 0.40)),
)

_BY_KEY: dict[str, BlockSpec] = {s.key: s for s in _SPECS}

#: Clés du catalogue, dans l'ordre d'insertion, qui est celui de la planche.
LIBRARY: tuple[str, ...] = tuple(s.key for s in _SPECS)


def spec(key: str) -> BlockSpec:
    """Fiche d'un bloc du catalogue. Lève ``InvalidParameter`` si le nom est faux.

    Une clé inconnue est une erreur immédiate, jamais un bloc vide inséré en
    silence: une occurrence sans définition ne se voit pas au dessin.
    """
    found = _BY_KEY.get(key.strip().lower())
    if found is None:
        raise InvalidParameter(
            f"Bloc inconnu de la bibliothèque: {key!r}", supported=list(LIBRARY)
        )
    return found


def block_name(key: str) -> str:
    """Nom DXF du bloc désigné par sa clé, par exemple ``MCP_DOOR``."""
    return spec(key).name


def define(key: str, defaults: Defaults) -> list[Operation]:
    """Opérations qui définissent un bloc du catalogue.

    Rend le calque à garantir puis la définition elle-même. La définition ne
    dessine rien: il faut encore l'insérer, ce que fait :func:`insert`.
    """
    item = spec(key)
    layer = item.layer()
    style = Style(layer=layer.name)
    return [
        EnsureLayer(name=layer.name, color=layer.color, description=layer.description),
        DefineBlock(
            name=item.name,
            base_point=(0.0, 0.0, 0.0),
            operations=tuple(item.draw(defaults, style)),
            attributes=(
                AttributeDef(
                    tag=MARK_TAG,
                    height=defaults.attribute_height,
                    default=item.mark,
                    prompt=item.description,
                    position=(
                        _m(item.label_m[0], defaults),
                        _m(item.label_m[1], defaults),
                    ),
                    halign="center",
                    valign="middle",
                    # Sur le calque du symbole, pas sur ANNOTATION: geler le
                    # calque doit emporter le repère avec ce qu'il repère.
                    style=style,
                ),
            ),
            description=item.description,
        ),
    ]


def define_library(
    defaults: Defaults, keys: list[str] | None = None
) -> list[Operation]:
    """Définit tout le catalogue, ou seulement les blocs demandés.

    Utile pour préparer un gabarit: les définitions sont posées une fois, les
    occurrences suivent. Définir un bloc déjà présent ne coûte rien et n'écrase
    rien, la définition existante est conservée.
    """
    wanted = list(LIBRARY) if keys is None else keys
    ops: list[Operation] = []
    for key in wanted:
        ops.extend(define(key, defaults))
    return ops


def insert(
    key: str,
    point: Point2,
    defaults: Defaults,
    *,
    rotation_deg: float = 0.0,
    scale: float = 1.0,
    mark: str | None = None,
) -> list[Operation]:
    """Définit le bloc au besoin puis en insère une occurrence.

    ``rotation_deg`` est reçu en degrés, unité naturelle pour un humain, et
    converti en radians à cette frontière publique comme partout ailleurs dans
    ``ops``.

    ``mark`` est la valeur du repère porté par cette occurrence. Sans valeur,
    le repère du catalogue est repris: deux chaises portent alors le même
    repère, ce qui reste juste pour un comptage mais pas pour un renvoi.
    """
    if scale <= 0.0:
        raise InvalidParameter(
            f"Échelle d'insertion invalide: {scale}", valid="strictement positive"
        )
    item = spec(key)
    layer = item.layer()
    ops = define(key, defaults)
    ops.append(
        AddBlockRef(
            name=item.name,
            insert=(*point, 0.0),
            scale=(scale, scale, scale),
            rotation=math.radians(rotation_deg),
            attributes=((MARK_TAG, item.mark if mark is None else mark),),
            style=Style(layer=layer.name),
        )
    )
    return ops
