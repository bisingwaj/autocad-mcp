"""Opérations de dessin déclaratives.

C'est la clé de voûte de l'architecture. La logique métier ne dessine pas, elle
produit des opérations. Un backend les exécute ensuite, sur AutoCAD ou dans un
fichier DXF. Trois conséquences directes:

* la logique est testable sans AutoCAD, donc sur la machine de développement ;
* le traitement par lots est immédiat, puisque la liste complète est connue
  avant toute exécution, ce qui permet une seule marque d'annulation et un seul
  regen au lieu d'un par entité ;
* un dessin peut être sérialisé, rejoué et comparé.

Ces objets ne contiennent que des données. Aucune méthode ne dessine.

**Convention d'angle: toute grandeur angulaire de ce module est en radians**,
sans exception. Cela vaut pour les angles d'arc, la rotation d'un texte, celle
d'un bloc et l'inclinaison d'une hachure.

Attention, les deux backends ne se comportent pas pareil et c'est normal:

* le **format DXF** stocke ses angles en degrés, donc le backend `ezdxf`
  convertit à l'écriture ;
* l'**API ActiveX d'AutoCAD** attend au contraire des radians, pour `AddArc`
  comme pour la rotation d'un texte, donc le backend COM écrit la valeur
  telle quelle. Y ajouter une conversion décalerait tous les arcs d'un
  facteur cent quatre-vingts sur pi.

La conversion depuis les degrés, unité naturelle pour un humain, se fait à la
frontière publique, dans `ops/primitives.py` et dans les schémas d'outils.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

Point2: TypeAlias = tuple[float, float]
Point3: TypeAlias = tuple[float, float, float]

#: Couleur ByLayer, valeur par défaut. Une entité suit alors son calque.
BYLAYER = 256
#: Couleur ByBlock.
BYBLOCK = 0


@dataclass(frozen=True, slots=True)
class Style:
    """Attributs graphiques communs à toutes les entités.

    ``color`` est un index ACI. La valeur par défaut ``BYLAYER`` est
    volontaire: une entité doit suivre son calque sauf intention contraire,
    ce qui est la convention du dessin technique.
    """

    layer: str = "0"
    color: int = BYLAYER
    linetype: str | None = None
    lineweight: int | None = None


@dataclass(frozen=True, slots=True)
class AddLine:
    """Segment droit entre deux points."""

    start: Point3
    end: Point3
    style: Style = field(default_factory=Style)
    kind: Literal["line"] = "line"


@dataclass(frozen=True, slots=True)
class AddPolyline:
    """Polyligne légère, ouverte ou fermée.

    Remplace les paquets de segments indépendants du code historique. Une
    polyligne fermée est une entité unique, sélectionnable d'un clic,
    hachurable, et dont l'aire est calculable.
    """

    points: tuple[Point2, ...]
    closed: bool = False
    #: Épaisseur constante appliquée à tous les segments, dans l'unité du document.
    width: float = 0.0
    style: Style = field(default_factory=Style)
    kind: Literal["polyline"] = "polyline"


@dataclass(frozen=True, slots=True)
class AddCircle:
    center: Point3
    radius: float
    style: Style = field(default_factory=Style)
    kind: Literal["circle"] = "circle"


@dataclass(frozen=True, slots=True)
class AddArc:
    """Arc de cercle. Les angles sont en radians et tournent dans le sens direct."""

    center: Point3
    radius: float
    start_angle: float
    end_angle: float
    style: Style = field(default_factory=Style)
    kind: Literal["arc"] = "arc"


@dataclass(frozen=True, slots=True)
class AddText:
    """Texte sur une ligne."""

    position: Point3
    text: str
    height: float
    rotation: float = 0.0
    halign: Literal["left", "center", "right"] = "left"
    valign: Literal["baseline", "bottom", "middle", "top"] = "baseline"
    style: Style = field(default_factory=Style)
    kind: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class AddMText:
    """Texte multiligne, avec retour à la ligne automatique sur ``width``."""

    position: Point3
    text: str
    height: float
    width: float = 0.0
    rotation: float = 0.0
    style: Style = field(default_factory=Style)
    kind: Literal["mtext"] = "mtext"


@dataclass(frozen=True, slots=True)
class AddHatch:
    """Hachure délimitée par un ou plusieurs contours fermés."""

    boundaries: tuple[tuple[Point2, ...], ...]
    pattern: str = "SOLID"
    scale: float = 1.0
    angle: float = 0.0
    style: Style = field(default_factory=Style)
    kind: Literal["hatch"] = "hatch"


@dataclass(frozen=True, slots=True)
class AddDimAligned:
    """Cote alignée sur la distance entre deux points."""

    p1: Point2
    p2: Point2
    #: Position de la ligne de cote, qui détermine le déport.
    location: Point2
    text_override: str | None = None
    style: Style = field(default_factory=Style)
    kind: Literal["dim_aligned"] = "dim_aligned"


@dataclass(frozen=True, slots=True)
class AddBlockRef:
    """Insertion d'une occurrence de bloc.

    Les blocs sont ce qui fait la différence entre un dessin et une maquette
    exploitable: une porte insérée cent fois reste une définition unique, et
    ses attributs alimentent les nomenclatures.
    """

    name: str
    insert: Point3
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotation: float = 0.0
    attributes: tuple[tuple[str, str], ...] = ()
    style: Style = field(default_factory=Style)
    kind: Literal["block_ref"] = "block_ref"


@dataclass(frozen=True, slots=True)
class AttributeDef:
    """Définition d'attribut portée par une définition de bloc.

    C'est ce qui rend une nomenclature possible: chaque occurrence insérée peut
    valoriser ces étiquettes, et l'extraction rend un tableau de quantités. Un
    bloc sans attribut n'est qu'un dessin répété.

    ``tag`` est l'identifiant technique, sans espace, écrit en majuscules par
    convention DXF. ``prompt`` est la question posée à l'utilisateur par
    l'interface d'AutoCAD, ``default`` la valeur proposée.
    """

    tag: str
    #: Hauteur du texte d'attribut, dans l'unité du document.
    height: float
    default: str = ""
    prompt: str = ""
    #: Position du texte, relative au point de base du bloc.
    position: Point2 = (0.0, 0.0)
    rotation: float = 0.0
    #: Un attribut invisible reste extractible mais n'encombre pas le dessin.
    invisible: bool = False
    #: Maintient le texte horizontal quelle que soit la rotation de l'occurrence.
    #:
    #: Le format fait suivre à un attribut la rotation du bloc qui le porte, si
    #: bien qu'un évier posé à cent quatre-vingts degrés affiche son repère à
    #: l'envers et un WC posé à quatre-vingt-dix degrés le couche sur le flanc.
    #: Un repère est une annotation: sa vocation est d'être lu, pas de suivre la
    #: géométrie. Le vrai par défaut est donc le bon choix ; le faux rend le
    #: comportement brut du format à qui le veut.
    keep_upright: bool = True
    halign: Literal["left", "center", "right"] = "left"
    valign: Literal["baseline", "bottom", "middle", "top"] = "baseline"
    style: Style = field(default_factory=Style)


@dataclass(frozen=True, slots=True)
class DefineBlock:
    """Définition d'un bloc: une géométrie paramétrée, nommée et réutilisable.

    ``AddBlockRef`` insère une occurrence, mais rien ne définissait le bloc: le
    modèle ne pouvait donc référencer que des blocs déjà présents dans le
    document. Cette opération comble le manque.

    Les opérations qui composent le bloc sont exprimées dans le **repère local**
    du bloc, ``base_point`` étant le point qui viendra se poser sur le point
    d'insertion. Elles ne peuvent être ni un calque ni une autre définition de
    bloc: la table des calques et celle des blocs appartiennent au document,
    pas à un bloc. Une occurrence ``AddBlockRef`` est en revanche admise, ce qui
    autorise les blocs imbriqués.

    **Sémantique de garantie, comme ``EnsureLayer``.** Si le nom est déjà pris,
    la définition existante est conservée telle quelle et le lot réussit. Un
    plan qui insère dix fois la même porte redéfinit dix fois le bloc sans
    coût ni surprise, et une bibliothèque n'écrase jamais le travail de
    quelqu'un d'autre.
    """

    name: str
    base_point: Point3 = (0.0, 0.0, 0.0)
    operations: tuple[Operation, ...] = ()
    attributes: tuple[AttributeDef, ...] = ()
    description: str = ""
    #: Style de l'**enregistrement de bloc**, pas de son contenu. Le format DXF
    #: ne donne au BLOCK qu'un calque, sans couleur ni type de trait: la
    #: convention veut que ce soit le calque ``0``. Le contenu porte son propre
    #: style, opération par opération. Ce champ existe aussi pour que toute
    #: opération du modèle en porte un, ce dont dépendent les backends.
    style: Style = field(default_factory=Style)
    kind: Literal["define_block"] = "define_block"


@dataclass(frozen=True, slots=True)
class AddMesh:
    """Maillage de facettes: la troisième dimension du projet.

    Un volume est décrit par ses sommets et par les facettes qui les relient,
    chaque facette étant la suite des indices de ses sommets. C'est la forme la
    plus simple qu'un logiciel de CAO relit sans ambiguïté, et la seule qui se
    convertisse telle quelle vers un visualiseur web.

    Le plan en deux dimensions reste la source de vérité: un volume s'obtient
    en donnant une hauteur à des contours déjà tracés, jamais en dessinant
    deux fois la même chose.
    """

    vertices: tuple[Point3, ...]
    #: Facettes, chacune donnant les indices de ses sommets dans ``vertices``.
    #: Au moins trois indices, sans plafond: les faces latérales d'un prisme en
    #: ont quatre, mais son dessus et son dessous en ont autant que le contour.
    #: Une dalle à cinq côtés porte donc des facettes à cinq sommets, et un
    #: format qui n'accepte que les quadrilatères doit les découper lui-même.
    faces: tuple[tuple[int, ...], ...]
    style: Style = field(default_factory=Style)
    kind: Literal["mesh"] = "mesh"


@dataclass(frozen=True, slots=True)
class EnsureLayer:
    """Crée le calque s'il n'existe pas, sans jamais écraser un calque existant."""

    name: str
    color: int = 7
    description: str = ""
    linetype: str | None = None
    kind: Literal["ensure_layer"] = "ensure_layer"


#: Union de toutes les opérations. Le mot-clé ``kind`` permet un aiguillage
#: exhaustif côté backend et une sérialisation sans perte.
Operation: TypeAlias = (
    AddLine
    | AddPolyline
    | AddCircle
    | AddArc
    | AddText
    | AddMText
    | AddHatch
    | AddDimAligned
    | AddBlockRef
    | AddMesh
    | DefineBlock
    | EnsureLayer
)


@dataclass(frozen=True, slots=True)
class OperationBatch:
    """Lot d'opérations exécuté comme une seule transaction.

    Le lot porte son propre nom, qui devient le libellé de la marque
    d'annulation dans AutoCAD. Annuler un plan entier redevient un seul geste.
    """

    operations: tuple[Operation, ...]
    label: str = "mcp"

    def __len__(self) -> int:
        return len(self.operations)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.operations)


# ---------------------------------------------------------------------------
# Validation commune
# ---------------------------------------------------------------------------
#
# Chaque backend valide avant d'écrire, mais la sémantique doit être la même
# partout: un rayon négatif doit échouer identiquement sur AutoCAD, sur DXF et
# en mémoire. Sans règle commune, la suite de contrat passerait sur un moteur
# et pas sur l'autre, ce qui est précisément l'écart qu'on veut éviter.


def validate(op: Operation) -> None:
    """Vérifie qu'une opération est réalisable. Lève ``InvalidGeometry``.

    Ne vérifie que ce qui est intrinsèquement impossible, jamais ce qui relève
    du goût. Un mur de dix kilomètres est absurde mais dessinable ; un cercle
    de rayon négatif ne l'est pas.
    """
    from ..errors import InvalidGeometry, InvalidParameter

    if isinstance(op, AddCircle | AddArc) and op.radius <= 0.0:
        raise InvalidGeometry("Rayon nul ou négatif", kind=op.kind, radius=op.radius)

    if isinstance(op, AddArc) and op.start_angle == op.end_angle:
        raise InvalidGeometry(
            "Arc d'ouverture nulle", start=op.start_angle, end=op.end_angle
        )

    if isinstance(op, AddLine) and op.start == op.end:
        raise InvalidGeometry("Segment de longueur nulle", point=op.start)

    if isinstance(op, AddText | AddMText):
        if op.height <= 0.0:
            raise InvalidGeometry("Hauteur de texte nulle ou négative", height=op.height)
        if not op.text.strip():
            raise InvalidParameter("Texte vide", kind=op.kind)

    if isinstance(op, AddPolyline):
        minimum = 3 if op.closed else 2
        if len(op.points) < minimum:
            raise InvalidGeometry(
                "Polyligne trop courte",
                count=len(op.points),
                minimum=minimum,
                closed=op.closed,
            )
        if op.width < 0.0:
            raise InvalidGeometry("Largeur négative", width=op.width)

    if isinstance(op, AddHatch):
        if not op.boundaries:
            raise InvalidGeometry("Hachure sans contour")
        for index, ring in enumerate(op.boundaries):
            if len(ring) < 3:
                raise InvalidGeometry(
                    "Contour de hachure à moins de trois sommets",
                    boundary=index,
                    count=len(ring),
                )

    if isinstance(op, AddDimAligned) and op.p1 == op.p2:
        raise InvalidGeometry("Cote entre deux points confondus", point=op.p1)

    if isinstance(op, AddBlockRef):
        if not op.name.strip():
            raise InvalidParameter("Nom de bloc vide")
        if any(s == 0.0 for s in op.scale):
            raise InvalidGeometry("Échelle de bloc nulle", scale=op.scale)

    if isinstance(op, DefineBlock):
        _validate_block_definition(op)

    if isinstance(op, AddMesh):
        if len(op.vertices) < 3:
            raise InvalidGeometry(
                "Un maillage demande au moins trois sommets", count=len(op.vertices)
            )
        if not op.faces:
            raise InvalidGeometry("Maillage sans facette")
        borne = len(op.vertices)
        for rang, facette in enumerate(op.faces):
            if len(facette) < 3:
                raise InvalidGeometry(
                    "Facette à moins de trois sommets", face=rang, count=len(facette)
                )
            for indice in facette:
                if not 0 <= indice < borne:
                    raise InvalidGeometry(
                        "Facette qui désigne un sommet inexistant",
                        face=rang,
                        index=indice,
                        vertices=borne,
                    )

    if isinstance(op, EnsureLayer):
        if not op.name.strip():
            raise InvalidParameter("Nom de calque vide")
        if not 0 <= op.color <= 256:
            raise InvalidParameter("Index ACI hors domaine", color=op.color)


def _validate_block_definition(op: DefineBlock) -> None:
    """Vérifie une définition de bloc, contenu compris.

    Un bloc mal formé ne se signale pas à l'insertion: il produit une occurrence
    vide, ou une nomenclature dont une colonne manque. La validation est donc
    faite ici, une fois, avant toute écriture.
    """
    from ..errors import InvalidGeometry, InvalidParameter

    if not op.name.strip():
        raise InvalidParameter("Nom de bloc vide")
    if not op.operations and not op.attributes:
        raise InvalidParameter("Définition de bloc sans contenu", name=op.name)
    if op.style.color != BYLAYER or op.style.linetype or op.style.lineweight:
        # Le BLOCK du format DXF n'a qu'un calque. Accepter une couleur
        # reviendrait à la perdre en silence, et le symbole serait dessiné
        # autrement qu'annoncé.
        raise InvalidParameter(
            "Une définition de bloc ne porte qu'un calque",
            name=op.name,
            remedy="mettre la couleur sur les opérations du contenu, ou sur l'occurrence",
        )

    for index, child in enumerate(op.operations):
        if isinstance(child, DefineBlock | EnsureLayer):
            raise InvalidParameter(
                "Une définition de bloc ne peut contenir ni calque ni autre bloc",
                name=op.name,
                at=index,
                found=child.kind,
                note="les tables de calques et de blocs appartiennent au document",
            )
        validate(child)

    seen: set[str] = set()
    for attribute in op.attributes:
        tag = attribute.tag.strip()
        if not tag:
            raise InvalidParameter("Étiquette d'attribut vide", block=op.name)
        if any(c.isspace() for c in tag):
            raise InvalidParameter(
                f"Étiquette d'attribut avec espace: {attribute.tag!r}",
                block=op.name,
                valid="un seul mot, sans espace",
            )
        upper = tag.upper()
        if upper in seen:
            # Deux étiquettes identiques rendraient la nomenclature ambiguë et
            # la valorisation à l'insertion silencieusement partielle.
            raise InvalidParameter(
                f"Étiquette d'attribut en double: {tag!r}", block=op.name
            )
        seen.add(upper)
        if attribute.height <= 0.0:
            raise InvalidGeometry(
                "Hauteur d'attribut nulle ou négative",
                block=op.name,
                tag=tag,
                height=attribute.height,
            )
