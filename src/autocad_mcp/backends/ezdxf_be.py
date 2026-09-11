"""Backend de dessin fondé sur ``ezdxf``.

C'est le backend qui porte la preuve du projet. Il fonctionne partout, sans
AutoCAD, donc c'est lui qui est exercé par les tests et par la boucle de
correction visuelle. Il produit un fichier DXF R2018, relisible par AutoCAD,
BricsCAD, LibreCAD ou QCAD.

Trois partis pris structurent ce fichier.

* **Aucun handle inventé.** Chaque ``EntityRef`` porte le handle attribué par
  ``ezdxf`` à l'entité réellement écrite dans le document. Le code historique
  renvoyait des chaînes comme ``line_created`` même quand rien n'était dessiné.
* **Aucun échec silencieux.** Une opération qui échoue apparaît dans
  ``BatchResult.failures`` avec son index et sa cause, et ``ok`` vaut alors
  ``False``. Une opération sans équivalent ``ezdxf`` lève
  ``UnsupportedOperation``.
* **Aucune écriture approximative.** Un calque inconnu, un type de ligne absent,
  un motif de hachures inexistant ou un bloc non défini font échouer l'opération
  au lieu de produire une entité muette. ``ezdxf`` accepterait pourtant chacun
  de ces cas sans broncher, et son propre audit ne les signale pas.

**Deux tables du document, jamais des entités.** ``EnsureLayer`` alimente la
table des calques, ``DefineBlock`` celle des blocs. Ni l'une ni l'autre ne
dessine, donc aucune ne rend de handle: elles se rapportent dans
``BatchResult.layers`` et ``BatchResult.blocks``. Les deux ont la même
sémantique de garantie de présence, sans écrasement de l'existant.

**Le style de cote est fabriqué ici.** Aucun des deux styles disponibles ne
convient: ``Standard`` est dimensionné en unités brutes et ``EZDXF`` porte
``dimlfac = 100``, si bien qu'une cote de six mètres s'affiche « 600 ». Voir
:meth:`EzdxfBackend._dimstyle`.

**Convention d'angle.** ``model.ops`` déclare les angles d'arc en radians. Par
cohérence, toutes les grandeurs angulaires du modèle sont traitées ici comme des
radians, y compris la rotation d'un texte, d'un bloc et l'angle d'une hachure.
Le DXF les stocke en degrés: la conversion est faite à l'écriture, une seule
fois, dans ce module.
"""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic
from ezdxf.enums import TextEntityAlignment
from ezdxf.layouts import BaseLayout, Modelspace
from ezdxf.lldxf import const as dxf_const
from ezdxf.lldxf import validator as dxf_validator
from ezdxf.math import area as ezdxf_area
from ezdxf.render.arrows import ARROWS

from ..errors import (
    ConfirmationRequired,
    EntityNotFound,
    InvalidGeometry,
    InvalidParameter,
    NotConnected,
    OperationFailed,
    UnsupportedOperation,
)
from ..model.layers import color_index
from ..model.ops import (
    BYLAYER,
    AddArc,
    AddBlockRef,
    AddCircle,
    AddDimAligned,
    AddHatch,
    AddLine,
    AddMText,
    AddPolyline,
    AddText,
    AttributeDef,
    DefineBlock,
    EnsureLayer,
    Operation,
    OperationBatch,
    Style,
)
from ..units import INSUNITS, Defaults, Unit
from .base import BatchResult, CadBackend, EntityFilter, EntityInfo, EntityRef

__all__ = ["EzdxfBackend"]

#: Version DXF cible. R2018 est le minimum imposé par le projet: c'est la
#: dernière version du format et elle est relue par tous les outils courants.
DXF_VERSION = "R2018"

#: Unité appliquée à un document neuf quand l'appelant n'en impose aucune.
DEFAULT_UNIT = Unit.MILLIMETER

#: Couleur d'un calque créé implicitement parce qu'une entité s'y réfère sans
#: qu'aucune opération ``EnsureLayer`` ne l'ait déclaré. L'ACI 7 s'affiche noir
#: sur fond clair: c'est le défaut neutre du dessin technique.
DEFAULT_LAYER_COLOR = 7

#: Correspondance entre le ``kind`` déclaratif de ``model.ops`` et le type DXF
#: réellement écrit. Sert aussi bien à l'exécution qu'au filtrage des requêtes.
KIND_TO_DXFTYPE: dict[str, str] = {
    "line": "LINE",
    "polyline": "LWPOLYLINE",
    "circle": "CIRCLE",
    "arc": "ARC",
    "text": "TEXT",
    "mtext": "MTEXT",
    "hatch": "HATCH",
    "dim_aligned": "DIMENSION",
    "block_ref": "INSERT",
    "define_block": "BLOCK",
    "ensure_layer": "LAYER",
}

#: Nom du style de cote créé par le projet. Le style ``EZDXF`` livré par la
#: bibliothèque ne convient pas, voir :meth:`EzdxfBackend._dimstyle`.
PROJECT_DIMSTYLE = "MCP"

#: Styles de texte acceptables pour la cotation, du plus lisible au repli
#: universel. ``Standard`` existe dans tout document DXF ; les autres ne sont
#: présents que si le document a été créé avec ``setup=True``.
DIM_TEXT_STYLES = (
    "OpenSansCondensed-Light",
    "OpenSans",
    "LiberationSans",
    "Standard",
)

#: Séparateur décimal des cotes, en code de caractère comme l'exige DIMDSEP.
#: Le point est la convention du format, quelle que soit la langue de l'usager.
DIM_DECIMAL_SEPARATOR = ord(".")

#: DIMZIN, suppression des zéros. Zéro signifie « on n'en supprime aucun »:
#: c'est ce qui fait écrire « 6.00 » au lieu de « 6 », donc lire la précision
#: du relevé sur la cote elle-même. Le style ``EZDXF`` vaut 12, soit zéro de
#: tête et zéros de queue supprimés.
DIM_KEEP_ALL_ZEROS = 0

#: DIMTAD, position du texte: au-dessus de la ligne de cote, convention du
#: dessin de bâtiment.
DIM_TEXT_ABOVE_LINE = 1

#: DIMLUNIT, unités décimales.
DIM_DECIMAL_UNITS = 2

#: Application propriétaire sous laquelle le projet inscrit ses données dans le
#: DXF. Le format n'a pas de champ pour dire qu'un attribut doit rester
#: horizontal: l'intention est donc portée par une donnée étendue de la
#: définition d'attribut, ce qui la fait survivre à l'enregistrement. Sans cela,
#: une occurrence insérée après relecture du fichier perdrait la consigne.
XDATA_APPID = "AUTOCAD_MCP"

#: Marqueur d'un attribut à maintenir horizontal, voir ``AttributeDef``.
XDATA_KEEP_UPRIGHT = "KEEP_UPRIGHT"

#: Alignements DXF par couple (horizontal, vertical) du modèle.
_TEXT_ALIGNMENT: dict[tuple[str, str], TextEntityAlignment] = {
    ("left", "baseline"): TextEntityAlignment.LEFT,
    ("center", "baseline"): TextEntityAlignment.CENTER,
    ("right", "baseline"): TextEntityAlignment.RIGHT,
    ("left", "bottom"): TextEntityAlignment.BOTTOM_LEFT,
    ("center", "bottom"): TextEntityAlignment.BOTTOM_CENTER,
    ("right", "bottom"): TextEntityAlignment.BOTTOM_RIGHT,
    ("left", "middle"): TextEntityAlignment.MIDDLE_LEFT,
    ("center", "middle"): TextEntityAlignment.MIDDLE_CENTER,
    ("right", "middle"): TextEntityAlignment.MIDDLE_RIGHT,
    ("left", "top"): TextEntityAlignment.TOP_LEFT,
    ("center", "top"): TextEntityAlignment.TOP_CENTER,
    ("right", "top"): TextEntityAlignment.TOP_RIGHT,
}

#: Point d'accroche MTEXT équivalent à un alignement haut-gauche, qui est le
#: comportement attendu d'un paragraphe posé à une position donnée.
_MTEXT_TOP_LEFT = 1


def _degrees(radians: float) -> float:
    """Convertit un angle du modèle vers l'unité du DXF."""
    return math.degrees(float(radians))


class EzdxfBackend(CadBackend):
    """Moteur de dessin écrivant dans un document ``ezdxf`` en mémoire.

    Le document n'est pas écrit sur disque tant que :meth:`save` n'est pas
    appelée, ce qui permet d'enchaîner plusieurs lots puis de n'enregistrer
    qu'une fois.

    Args:
        path: chemin d'un DXF existant à ouvrir. Si ``None``, un document neuf
            est créé. Le chemin sert aussi de destination par défaut à
            :meth:`save`.
        unit: unité de longueur à inscrire dans ``$INSUNITS``. Si ``None`` et
            qu'un fichier est ouvert, l'unité du fichier est conservée: écraser
            l'unité d'un document existant fausserait silencieusement toutes ses
            cotes.
        dxfversion: version du format pour un document neuf.
        setup: charge les types de ligne, styles de texte et styles de cote
            normalisés dans un document neuf. Sans eux, ni ``DASHED`` ni la
            cotation ne sont utilisables.
    """

    name = "ezdxf"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        unit: Unit | None = None,
        dxfversion: str = DXF_VERSION,
        setup: bool = True,
    ) -> None:
        self._path: Path | None = Path(path).expanduser() if path is not None else None
        self._requested_unit = unit
        self._dxfversion = dxfversion
        self._setup = setup
        self._doc: Drawing | None = None
        self._unit: Unit = unit if unit is not None else DEFAULT_UNIT
        # Une pile de lots, chacun décrit par les handles des entités qu'il a
        # réellement créées. C'est le substitut d'une marque d'annulation
        # AutoCAD. Les calques n'y figurent pas: voir :meth:`undo`.
        self._undo_stack: list[list[str]] = []

    # ---- cycle de vie -------------------------------------------------

    def connect(self) -> None:
        """Ouvre le fichier demandé ou crée un document neuf. Idempotent."""
        if self._doc is not None:
            return
        if self._path is not None and self._path.exists():
            try:
                doc = ezdxf.readfile(str(self._path))
            except (OSError, dxf_const.DXFError) as exc:
                raise OperationFailed(
                    f"Lecture impossible de {self._path}: {exc}", path=str(self._path)
                ) from exc
            self._unit = self._resolve_unit(doc)
        else:
            doc = ezdxf.new(dxfversion=self._dxfversion, setup=self._setup)
            self._unit = self._requested_unit or DEFAULT_UNIT
        if self._requested_unit is not None or self._path is None or not self._path.exists():
            doc.units = INSUNITS[self._unit]
            doc.header["$INSUNITS"] = INSUNITS[self._unit]
        self._doc = doc

    def _resolve_unit(self, doc: Drawing) -> Unit:
        """Déduit l'unité d'un document lu, sans jamais inventer de valeur."""
        if self._requested_unit is not None:
            return self._requested_unit
        code = int(doc.header.get("$INSUNITS", 0))
        for unit, insunits in INSUNITS.items():
            if insunits == code:
                return unit
        # $INSUNITS = 0 signifie « sans unité ». On le rapporte comme le défaut
        # du projet plutôt que d'échouer, mais sans toucher à l'en-tête.
        return DEFAULT_UNIT

    def close(self) -> None:
        """Libère le document. Le fichier déjà enregistré n'est pas touché."""
        self._doc = None
        self._undo_stack.clear()

    @property
    def unit(self) -> Unit:
        """Unité de longueur du document courant."""
        if self._doc is None:
            raise NotConnected("Aucun document ezdxf ouvert", remedy="appeler connect()")
        return self._unit

    @property
    def document(self) -> Drawing:
        """Document ``ezdxf`` sous-jacent, pour le rendu et les tests."""
        return self._require_doc()

    def _require_doc(self) -> Drawing:
        if self._doc is None:
            raise NotConnected("Aucun document ezdxf ouvert", remedy="appeler connect()")
        return self._doc

    # ---- écriture -----------------------------------------------------

    def execute(self, batch: OperationBatch) -> BatchResult:
        """Exécute un lot et rend les handles réellement créés.

        Le lot n'est pas atomique: une opération qui échoue est consignée dans
        ``failures`` avec son index, et les suivantes sont tout de même tentées.
        Un plan de cent murs ne doit pas être perdu parce que la cote numéro
        quarante-deux était dégénérée. En revanche ``ok`` vaut ``False`` dès
        qu'un échec existe, et l'entité partiellement écrite par l'opération
        fautive est retirée du document.

        ``created`` ne porte que des entités de l'espace objet. ``EnsureLayer``
        agit sur la table des calques, pas sur le dessin: elle n'ajoute donc
        aucun handle et se rapporte dans ``layers``. Compter un calque parmi les
        objets dessinés fausserait le nombre d'entités et ferait désigner une
        entrée de table là où l'appelant attend quelque chose d'effaçable.

        ``ezdxf`` n'a pas d'affichage: il n'y a donc ni marque d'annulation
        native ni rafraîchissement à grouper. La pile interne d'annulation joue
        le rôle de la marque, voir :meth:`undo`.
        """
        doc = self._require_doc()
        msp = doc.modelspace()
        result = BatchResult(label=batch.label)
        undo_record: list[str] = []

        for index, operation in enumerate(batch.operations):
            entity_count = len(msp)
            try:
                ref = self._apply(doc, msp, operation, result)
            except Exception as exc:  # consigné, jamais avalé
                self._rollback_partial(msp, entity_count)
                result.failures.append(self._failure(index, operation, exc))
            else:
                if ref is not None:
                    result.created.append(ref)
                    undo_record.append(ref.handle)

        self._undo_stack.append(undo_record)
        return result

    @staticmethod
    def _failure(index: int, operation: Operation, exc: Exception) -> dict[str, Any]:
        """Décrit un échec d'opération de façon exploitable par le modèle."""
        from ..errors import CadError

        payload: dict[str, Any] = {
            "index": index,
            "operation": getattr(operation, "kind", type(operation).__name__),
        }
        if isinstance(exc, CadError):
            payload["code"] = exc.code
            payload["error"] = exc.message
            if exc.details:
                payload["details"] = exc.details
        else:
            payload["code"] = OperationFailed.code
            payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    @staticmethod
    def _rollback_partial(msp: Modelspace, entity_count: int) -> None:
        """Retire les entités écrites par une opération qui a ensuite échoué.

        Sans ce retrait, le document contiendrait des entités dont aucun handle
        n'a été rendu à l'appelant: invisibles au modèle, bien réelles dans le
        fichier.
        """
        extra = list(msp)[entity_count:]
        for entity in extra:
            msp.delete_entity(entity)

    def _apply(
        self, doc: Drawing, msp: BaseLayout, operation: Operation, result: BatchResult
    ) -> EntityRef | None:
        """Aiguille une opération vers son écriture ``ezdxf``.

        Rend l'entité créée, ou ``None`` pour une opération qui ne dessine rien
        et n'agit que sur les tables du document, comme ``EnsureLayer`` ou
        ``DefineBlock``.

        ``msp`` est l'espace objet pour un lot ordinaire, mais la définition
        d'un bloc réemprunte ce même aiguillage avec le contenant du bloc: la
        géométrie d'un bloc s'écrit exactement comme celle du dessin, seule sa
        destination change.
        """
        if isinstance(operation, EnsureLayer):
            self._ensure_layer(doc, operation, result)
            return None
        if isinstance(operation, DefineBlock):
            self._define_block(doc, operation, result)
            return None
        if isinstance(operation, AddLine):
            return self._add_line(doc, msp, operation, result)
        if isinstance(operation, AddPolyline):
            return self._add_polyline(doc, msp, operation, result)
        if isinstance(operation, AddCircle):
            return self._add_circle(doc, msp, operation, result)
        if isinstance(operation, AddArc):
            return self._add_arc(doc, msp, operation, result)
        if isinstance(operation, AddText):
            return self._add_text(doc, msp, operation, result)
        if isinstance(operation, AddMText):
            return self._add_mtext(doc, msp, operation, result)
        if isinstance(operation, AddHatch):
            return self._add_hatch(doc, msp, operation, result)
        if isinstance(operation, AddDimAligned):
            return self._add_dim_aligned(doc, msp, operation, result)
        if isinstance(operation, AddBlockRef):
            return self._add_block_ref(doc, msp, operation, result)
        raise UnsupportedOperation(
            f"{self.name} ne sait pas exécuter {type(operation).__name__}",
            kind=getattr(operation, "kind", None),
            supported=sorted(KIND_TO_DXFTYPE),
        )

    # ---- écriture, opération par opération ----------------------------

    def _ensure_layer(
        self, doc: Drawing, operation: EnsureLayer, result: BatchResult
    ) -> None:
        """Garantit la présence d'un calque. Ne dessine rien, donc ne rend rien.

        Le nom est rapporté dans ``BatchResult.layers``, qu'il ait fallu créer le
        calque ou qu'il existât déjà: l'appelant sait ainsi sur quoi il peut
        dessiner, sans confondre cela avec une entité.
        """
        name = _valid_layer_name(operation.name)
        if name in doc.layers:
            # Le calque existe: on ne touche ni à sa couleur ni à sa description,
            # l'opération est une garantie de présence, pas une remise à zéro.
            _note_layer(result, str(doc.layers.get(name).dxf.name))
            return

        attribs: dict[str, Any] = {"color": color_index(operation.color)}
        if operation.linetype is not None:
            attribs["linetype"] = self._require_linetype(doc, operation.linetype)
        layer = doc.layers.add(name, **attribs)
        if operation.description:
            layer.description = operation.description
        _note_layer(result, str(layer.dxf.name))

    def _add_line(
        self, doc: Drawing, msp: BaseLayout, operation: AddLine, result: BatchResult
    ) -> EntityRef:
        start = _point3(operation.start, "start")
        end = _point3(operation.end, "end")
        if _same_point(start, end):
            raise InvalidGeometry("Segment de longueur nulle", start=start, end=end)
        attribs = self._dxfattribs(doc, operation.style, result)
        entity = msp.add_line(start, end, dxfattribs=attribs)
        return _ref(entity)

    def _add_polyline(
        self, doc: Drawing, msp: BaseLayout, operation: AddPolyline, result: BatchResult
    ) -> EntityRef:
        points = [_point2(p, f"points[{i}]") for i, p in enumerate(operation.points)]
        minimum = 3 if operation.closed else 2
        if len(points) < minimum:
            raise InvalidGeometry(
                "Polyligne dégénérée",
                vertices=len(points),
                required=minimum,
                closed=operation.closed,
            )
        if operation.width < 0.0:
            raise InvalidParameter(f"Épaisseur négative: {operation.width}", valid=">= 0")
        attribs = self._dxfattribs(doc, operation.style, result)
        entity = msp.add_lwpolyline(points, format="xy", close=operation.closed, dxfattribs=attribs)
        if operation.width > 0.0:
            entity.dxf.const_width = float(operation.width)
        return _ref(entity)

    def _add_circle(
        self, doc: Drawing, msp: BaseLayout, operation: AddCircle, result: BatchResult
    ) -> EntityRef:
        center = _point3(operation.center, "center")
        radius = _positive(operation.radius, "radius")
        attribs = self._dxfattribs(doc, operation.style, result)
        entity = msp.add_circle(center, radius, dxfattribs=attribs)
        return _ref(entity)

    def _add_arc(
        self, doc: Drawing, msp: BaseLayout, operation: AddArc, result: BatchResult
    ) -> EntityRef:
        center = _point3(operation.center, "center")
        radius = _positive(operation.radius, "radius")
        start = _degrees(operation.start_angle)
        end = _degrees(operation.end_angle)
        if math.isclose(start % 360.0, end % 360.0, abs_tol=1e-9):
            raise InvalidGeometry(
                "Arc d'ouverture nulle",
                start_angle=operation.start_angle,
                end_angle=operation.end_angle,
            )
        attribs = self._dxfattribs(doc, operation.style, result)
        # ezdxf trace toujours dans le sens direct de start vers end, ce qui est
        # la convention annoncée par model.ops.
        entity = msp.add_arc(center, radius, start, end, is_counter_clockwise=True, dxfattribs=attribs)
        return _ref(entity)

    def _add_text(
        self, doc: Drawing, msp: BaseLayout, operation: AddText, result: BatchResult
    ) -> EntityRef:
        if not operation.text:
            raise InvalidParameter("Texte vide", valid="chaîne non vide")
        height = _positive(operation.height, "height")
        alignment = _TEXT_ALIGNMENT.get((operation.halign, operation.valign))
        if alignment is None:
            raise InvalidParameter(
                f"Alignement inconnu: {operation.halign}/{operation.valign}",
                supported=sorted(f"{h}/{v}" for h, v in _TEXT_ALIGNMENT),
            )
        attribs = self._dxfattribs(doc, operation.style, result)
        entity = msp.add_text(
            operation.text,
            height=height,
            rotation=_degrees(operation.rotation),
            dxfattribs=attribs,
        )
        entity.set_placement(_point3(operation.position, "position"), align=alignment)
        return _ref(entity)

    def _add_mtext(
        self, doc: Drawing, msp: BaseLayout, operation: AddMText, result: BatchResult
    ) -> EntityRef:
        if not operation.text:
            raise InvalidParameter("Texte vide", valid="chaîne non vide")
        height = _positive(operation.height, "height")
        if operation.width < 0.0:
            raise InvalidParameter(f"Largeur négative: {operation.width}", valid=">= 0")
        attribs = self._dxfattribs(doc, operation.style, result)
        attribs["char_height"] = height
        # Une largeur nulle vaut « pas de retour automatique » côté modèle: on
        # laisse alors l'attribut DXF absent plutôt que d'écrire 0, qu'AutoCAD
        # interprète différemment selon les versions.
        if operation.width > 0.0:
            attribs["width"] = float(operation.width)
        attribs["attachment_point"] = _MTEXT_TOP_LEFT
        entity = msp.add_mtext(operation.text, dxfattribs=attribs)
        entity.set_location(
            _point3(operation.position, "position"),
            rotation=_degrees(operation.rotation),
            attachment_point=_MTEXT_TOP_LEFT,
        )
        return _ref(entity)

    def _add_hatch(
        self, doc: Drawing, msp: BaseLayout, operation: AddHatch, result: BatchResult
    ) -> EntityRef:
        if not operation.boundaries:
            raise InvalidGeometry("Hachure sans contour")
        boundaries: list[list[tuple[float, float]]] = []
        for index, boundary in enumerate(operation.boundaries):
            points = [_point2(p, f"boundaries[{index}][{i}]") for i, p in enumerate(boundary)]
            if len(points) < 3:
                raise InvalidGeometry(
                    "Contour de hachure dégénéré", boundary=index, vertices=len(points)
                )
            boundaries.append(points)
        scale = _positive(operation.scale, "scale")

        attribs = self._dxfattribs(doc, operation.style, result)
        aci = attribs["color"]
        pattern = operation.pattern.strip().upper()
        if pattern != "SOLID":
            self._require_pattern(pattern)

        entity = msp.add_hatch(dxfattribs=attribs)
        for points in boundaries:
            entity.paths.add_polyline_path(points, is_closed=True)
        if pattern == "SOLID":
            entity.set_solid_fill(color=aci)
        else:
            entity.set_pattern_fill(
                pattern, color=aci, angle=_degrees(operation.angle), scale=scale
            )
        return _ref(entity)

    def _add_dim_aligned(
        self, doc: Drawing, msp: BaseLayout, operation: AddDimAligned, result: BatchResult
    ) -> EntityRef:
        p1 = _point2(operation.p1, "p1")
        p2 = _point2(operation.p2, "p2")
        location = _point2(operation.location, "location")
        if _same_point(p1, p2):
            raise InvalidGeometry("Cote entre deux points confondus", p1=p1, p2=p2)

        # ezdxf attend un déport signé et non un point: c'est la distance
        # algébrique de la ligne de cote à la ligne de mesure, positive à gauche
        # du vecteur p1 vers p2. La projection est faite ici, elle n'a pas de
        # sens métier et n'a donc rien à faire dans la couche modèle.
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = math.hypot(dx, dy)
        distance = ((location[0] - p1[0]) * -dy + (location[1] - p1[1]) * dx) / length

        attribs = self._dxfattribs(doc, operation.style, result)
        dimension = msp.add_aligned_dim(
            p1=p1,
            p2=p2,
            distance=distance,
            text=operation.text_override if operation.text_override is not None else "<>",
            dimstyle=self._dimstyle(doc),
            dxfattribs=attribs,
        )
        # Sans rendu, la cote n'a pas de géométrie et reste invisible partout
        # sauf dans AutoCAD, qui la reconstruit à l'ouverture.
        dimension.render()
        entity = dimension.dimension
        return _ref(entity)

    def _add_block_ref(
        self, doc: Drawing, msp: BaseLayout, operation: AddBlockRef, result: BatchResult
    ) -> EntityRef:
        if operation.name not in doc.blocks:
            raise EntityNotFound(
                f"Bloc non défini: {operation.name!r}",
                remedy="définir le bloc avant de l'insérer",
                known=sorted(b.name for b in doc.blocks if not b.name.startswith("*"))[:20],
            )
        scale = tuple(float(s) for s in operation.scale)
        if len(scale) != 3 or any(s == 0.0 for s in scale):
            raise InvalidParameter(
                f"Échelle de bloc invalide: {operation.scale}", valid="trois facteurs non nuls"
            )
        attribs = self._dxfattribs(doc, operation.style, result)
        attribs.update(
            {
                "xscale": scale[0],
                "yscale": scale[1],
                "zscale": scale[2],
                "rotation": _degrees(operation.rotation),
            }
        )
        entity = msp.add_blockref(
            operation.name, _point3(operation.insert, "insert"), dxfattribs=attribs
        )
        if operation.attributes:
            block = doc.blocks.get(operation.name)
            defined = {attdef.dxf.tag for attdef in block.attdefs()}
            values = dict(operation.attributes)
            unknown = sorted(set(values) - defined)
            if unknown:
                # add_auto_attribs ignore les étiquettes inconnues sans rien
                # dire: la valeur serait perdue et la nomenclature fausse.
                raise InvalidParameter(
                    f"Attributs inconnus du bloc {operation.name!r}: {unknown}",
                    supported=sorted(defined),
                )
            entity.add_auto_attribs(values)
            # ``add_auto_attribs`` applique la transformation de l'occurrence,
            # donc un repère suit la rotation du bloc et se lit à l'envers sur un
            # symbole retourné. On le redresse après coup, une fois sa position
            # calculée: seule l'orientation change, jamais l'emplacement.
            upright = {
                attdef.dxf.tag for attdef in block.attdefs() if _wants_upright(attdef)
            }
            for attrib in entity.attribs:
                if attrib.dxf.tag in upright:
                    _straighten(attrib)
        return _ref(entity)

    def _define_block(
        self, doc: Drawing, operation: DefineBlock, result: BatchResult
    ) -> None:
        """Écrit une définition de bloc dans la table des blocs du document.

        Ne dessine rien, donc ne rend aucun handle: une définition n'est pas une
        entité de l'espace objet, seule l'occupation ``AddBlockRef`` en est une.
        Le nom est rapporté dans ``BatchResult.blocks``, comme un calque l'est
        dans ``layers``.

        **Garantie de présence, pas écrasement.** Un nom déjà pris est conservé
        tel quel. Deux raisons: un plan qui insère dix fois la même porte
        redéfinit dix fois le bloc, et redéfinir un bloc change silencieusement
        toutes les occurrences déjà posées, y compris celles d'un autre lot.

        **Rien de partiel.** Si une opération du contenu échoue, la définition
        entamée est retirée avant que l'erreur ne remonte. Un bloc à moitié
        écrit s'insérerait sans erreur et produirait un symbole tronqué.
        """
        name = operation.name.strip()
        if not name:
            raise InvalidParameter("Nom de bloc vide", valid="chaîne non vide")
        if name in doc.blocks:
            _note_block(result, name)
            return
        if not operation.operations and not operation.attributes:
            raise InvalidParameter(
                f"Définition de bloc sans contenu: {name!r}",
                remedy="fournir au moins une opération ou un attribut",
            )

        # Le BLOCK du format DXF n'accepte qu'un calque: ni couleur, ni type de
        # trait. Les refuser au lieu de les perdre est le parti pris du module.
        if (
            operation.style.color != BYLAYER
            or operation.style.linetype is not None
            or operation.style.lineweight is not None
        ):
            raise InvalidParameter(
                "Une définition de bloc ne porte qu'un calque",
                name=name,
                remedy="styler le contenu du bloc, ou l'occurrence insérée",
            )
        block = doc.blocks.new(
            name=name,
            base_point=_point3(operation.base_point, "base_point"),
            dxfattribs={"layer": self._resolve_layer(doc, operation.style.layer, result)},
        )
        try:
            # ``BlockLayout.block`` est l'entité BLOCK elle-même. Elle existe
            # toujours pour un bloc fraîchement créé, mais le type l'annonce
            # optionnelle: un bloc lu d'un fichier abîmé peut en manquer.
            if operation.description and block.block is not None:
                block.block.dxf.description = operation.description
            for index, child in enumerate(operation.operations):
                if isinstance(child, EnsureLayer | DefineBlock):
                    raise InvalidParameter(
                        "Une définition de bloc ne peut contenir ni calque ni autre bloc",
                        block=name,
                        at=index,
                        found=child.kind,
                    )
                # Même aiguillage que l'espace objet: la géométrie d'un bloc
                # s'écrit comme celle du dessin. Les handles ne sont pas
                # rapportés, ils ne désignent rien d'effaçable ni d'annulable.
                self._apply(doc, block, child, result)
            for attribute in operation.attributes:
                self._add_attdef(doc, block, attribute, result)
        except Exception:
            doc.blocks.delete_block(name, safe=False)
            raise
        _note_block(result, name)

    def _add_attdef(
        self, doc: Drawing, block: BaseLayout, attribute: AttributeDef, result: BatchResult
    ) -> None:
        """Ajoute une définition d'attribut au bloc en cours d'écriture.

        L'attribut est ce qui rend la nomenclature possible: sans lui, un bloc
        inséré cent fois ne dit rien d'autre que sa présence.
        """
        tag = attribute.tag.strip()
        if not tag:
            raise InvalidParameter("Étiquette d'attribut vide", valid="un mot sans espace")
        if any(c.isspace() for c in tag):
            raise InvalidParameter(
                f"Étiquette d'attribut avec espace: {attribute.tag!r}",
                valid="un seul mot, sans espace",
            )
        height = _positive(attribute.height, "attribute.height")
        alignment = _TEXT_ALIGNMENT.get((attribute.halign, attribute.valign))
        if alignment is None:
            raise InvalidParameter(
                f"Alignement inconnu: {attribute.halign}/{attribute.valign}",
                supported=sorted(f"{h}/{v}" for h, v in _TEXT_ALIGNMENT),
            )
        attribs = self._dxfattribs(doc, attribute.style, result)
        attribs["prompt"] = attribute.prompt or tag
        attdef = block.add_attdef(
            tag,
            text=attribute.default,
            height=height,
            rotation=_degrees(attribute.rotation),
            dxfattribs=attribs,
        )
        attdef.set_placement(
            _point3(attribute.position, "attribute.position"), align=alignment
        )
        attdef.is_invisible = bool(attribute.invisible)
        if attribute.keep_upright:
            # Marqué sur la définition, pas sur l'occurrence: c'est le bloc qui
            # sait que son repère est une annotation, et toute insertion
            # ultérieure doit l'apprendre de lui, même après relecture du DXF.
            if XDATA_APPID not in doc.appids:
                doc.appids.add(XDATA_APPID)
            attdef.set_xdata(XDATA_APPID, [(1000, XDATA_KEEP_UPRIGHT)])

    # ---- attributs graphiques -----------------------------------------

    def _dxfattribs(self, doc: Drawing, style: Style, result: BatchResult) -> dict[str, Any]:
        """Traduit un ``Style`` du modèle en attributs DXF validés."""
        attribs: dict[str, Any] = {
            "layer": self._resolve_layer(doc, style.layer, result),
            "color": color_index(style.color),
        }
        if style.linetype is not None:
            attribs["linetype"] = self._require_linetype(doc, style.linetype)
        if style.lineweight is not None:
            attribs["lineweight"] = self._require_lineweight(style.lineweight)
        return attribs

    @staticmethod
    def _resolve_layer(doc: Drawing, name: str, result: BatchResult) -> str:
        """Rend le nom canonique du calque d'une entité, en le créant au besoin.

        ``ezdxf`` accepte sans broncher une entité posée sur un calque qui
        n'existe pas, et son auditeur ne le signale pas: le calque fantôme
        n'apparaît alors dans aucun gestionnaire de calques. Faire échouer
        l'opération romprait la parité avec les autres backends, qui acceptent
        de dessiner sans déclaration préalable. Le calque est donc créé avec la
        couleur par défaut, et son nom est rapporté dans ``BatchResult.layers``:
        la création reste visible pour le modèle au lieu d'être devinée.

        Un nom invalide reste une erreur: il ne serait relu par aucun logiciel.
        """
        label = _valid_layer_name(name)
        if label in doc.layers:
            return str(doc.layers.get(label).dxf.name)
        layer = doc.layers.add(label, color=DEFAULT_LAYER_COLOR)
        _note_layer(result, str(layer.dxf.name))
        return str(layer.dxf.name)

    @staticmethod
    def _require_linetype(doc: Drawing, name: str) -> str:
        if name not in doc.linetypes:
            raise InvalidParameter(
                f"Type de ligne inconnu: {name!r}",
                supported=sorted(lt.dxf.name for lt in doc.linetypes)[:30],
            )
        return str(doc.linetypes.get(name).dxf.name)

    @staticmethod
    def _require_lineweight(value: int) -> int:
        if value not in dxf_const.VALID_DXF_LINEWEIGHT_VALUES:
            raise InvalidParameter(
                f"Épaisseur de trait invalide: {value}",
                supported=sorted(dxf_const.VALID_DXF_LINEWEIGHT_VALUES),
                note="centièmes de millimètre, -1 ByLayer, -2 ByBlock, -3 défaut",
            )
        return int(value)

    @staticmethod
    def _require_pattern(name: str) -> None:
        from ezdxf.tools import pattern as ezdxf_pattern

        known = ezdxf_pattern.load()
        if name not in known:
            raise InvalidParameter(
                f"Motif de hachures inconnu: {name!r}",
                note="un motif absent produirait une hachure vide, sans erreur",
                supported=sorted(known)[:30],
            )

    def _dimstyle(self, doc: Drawing) -> str:
        """Style de cote du projet, créé au besoin et dimensionné sur l'unité.

        **Une cote fausse est pire qu'une cote absente**, parce qu'elle se lit
        sans se relire. Les deux styles disponibles par défaut sont des pièges.

        * ``Standard`` est le style du format brut: hauteur de texte 1.0 et
          flèches 0.18, exprimées en unités de dessin. Dans un plan en mètres,
          le texte fait un mètre de haut ; dans un plan en millimètres, il est
          invisible.
        * ``EZDXF``, livré par la bibliothèque, vise le plan en mètres tracé au
          centième et porte ``dimlfac = 100``: **une mesure de six mètres
          s'affiche « 600 »**. Le facteur est correct pour qui dessine en mètres
          et cote en centimètres, muet pour tous les autres, et personne ne le
          voit dans le fichier produit. Il porte en outre ``dimzin = 12``, qui
          supprime les zéros de queue: « 6.00 » deviendrait « 6 ».

        Le style ``MCP`` créé ici fixe donc ``dimlfac = 1``, si bien que le
        nombre affiché est la mesure réelle dans l'unité du document, et tire
        toutes ses longueurs d'habillage de :class:`units.Defaults`: hauteur de
        texte, taille des tirets d'extrémité, dépassement et retrait des lignes
        d'attache, écart du texte. Ces grandeurs sont définies en mètres puis
        converties, donc l'habillage reste à la même taille apparente que le
        dessin soit en millimètres, en mètres ou en pieds.

        Le style est créé une fois et réutilisé. S'il existe déjà, il est
        conservé tel quel: un document ouvert garde ses conventions.
        """
        if PROJECT_DIMSTYLE in doc.dimstyles:
            return PROJECT_DIMSTYLE

        defaults = Defaults(self._unit)
        style = doc.dimstyles.add(PROJECT_DIMSTYLE)
        style.dxf.dimlfac = 1.0  # la mesure affichée est la mesure réelle
        style.dxf.dimscale = 1.0  # l'habillage est déjà à l'échelle du dessin
        style.dxf.dimtxt = defaults.text_height
        style.dxf.dimasz = defaults.dim_arrow_size
        style.dxf.dimexe = defaults.dim_extension
        style.dxf.dimexo = defaults.dim_offset
        style.dxf.dimgap = defaults.dim_text_gap
        style.dxf.dimdle = 0.0
        style.dxf.dimdec = defaults.DIM_DECIMALS
        style.dxf.dimlunit = DIM_DECIMAL_UNITS
        style.dxf.dimzin = DIM_KEEP_ALL_ZEROS
        style.dxf.dimdsep = DIM_DECIMAL_SEPARATOR
        style.dxf.dimtad = DIM_TEXT_ABOVE_LINE
        # Texte aligné sur la ligne de cote, dedans comme dehors: une cote
        # horizontale de plan ne doit pas basculer à l'horizontale du papier.
        style.dxf.dimtih = 0
        style.dxf.dimtoh = 0
        style.dxf.dimtxsty = self._dim_text_style(doc)
        # Tiret oblique d'architecte plutôt que flèche pleine. ``set_arrows``
        # crée le bloc ``_ARCHTICK`` s'il manque, y compris dans un document
        # ouvert sans les tables normalisées.
        style.set_arrows(blk=ARROWS.architectural_tick)
        return PROJECT_DIMSTYLE

    @staticmethod
    def _dim_text_style(doc: Drawing) -> str:
        """Style de texte de la cotation, choisi parmi ceux du document.

        Un style de texte absent ferait afficher la cote avec la police de
        repli du logiciel de lecture, ou rien du tout selon les versions.
        """
        for candidate in DIM_TEXT_STYLES:
            if candidate in doc.styles:
                return candidate
        raise UnsupportedOperation(
            "Aucun style de texte disponible pour la cotation",
            remedy="ouvrir le document avec setup=True",
            tried=list(DIM_TEXT_STYLES),
        )

    # ---- annulation ---------------------------------------------------

    def undo(self) -> None:
        """Annule le dernier lot en supprimant ce qu'il a créé.

        ``ezdxf`` n'a pas de journal d'annulation: il n'y a ni marque ni pile
        native. L'annulation est donc reconstruite ici, en mémorisant à chaque
        lot les objets réellement créés puis en les supprimant dans l'ordre
        inverse. Deux conséquences assumées, que le backend AutoCAD n'a pas.

        * Seules les entités de l'espace objet sont retirées. Les calques mis
          en place par le lot restent: un calque vide est inoffensif, alors
          qu'un calque supprimé emporterait des entités dessinées par d'autres
          lots. C'est aussi ce qui justifie que ``BatchResult.created`` ne
          contienne aucun calque.
        * Seules les créations sont annulées. Une couleur modifiée par
          :meth:`set_color` ou une entité supprimée par :meth:`delete` ne sont
          pas restaurées.
        * Un objet déjà disparu est ignoré: annuler après avoir supprimé à la
          main ne doit pas échouer.

        Les définitions de blocs anonymes engendrées par le rendu d'une cote
        subsistent. Elles ne sont référencées par rien et n'apparaissent pas au
        dessin, mais elles restent dans le fichier.
        """
        doc = self._require_doc()
        if not self._undo_stack:
            raise OperationFailed(
                "Aucun lot à annuler", remedy="exécuter un lot avant d'appeler undo()"
            )
        record = self._undo_stack.pop()
        msp = doc.modelspace()
        for handle in reversed(record):
            entity = doc.entitydb.get(handle)
            if entity is not None and entity.is_alive and isinstance(entity, DXFGraphic):
                msp.delete_entity(entity)

    # ---- sélection ----------------------------------------------------

    def delete(self, selector: EntityFilter) -> list[str]:
        """Supprime les entités correspondant au filtre, rend leurs handles.

        Un filtre vide désignerait tout le dessin. Plutôt que de l'exécuter,
        le backend lève ``ConfirmationRequired``: c'est à la couche outil de
        demander une confirmation explicite puis de passer un filtre désignant
        ce qu'elle veut vraiment détruire.
        """
        doc = self._require_doc()
        if selector is None or selector.is_empty:
            raise ConfirmationRequired(
                "Suppression sans filtre refusée",
                remedy="préciser au moins un critère: calque, type, couleur, handles ou fenêtre",
            )
        msp = doc.modelspace()
        entities = self._select(selector)
        handles = [entity.dxf.handle for entity in entities]
        for entity in entities:
            msp.delete_entity(entity)
        return handles

    def set_color(self, handle: str, color: int) -> None:
        """Change la couleur d'une entité désignée par son handle."""
        doc = self._require_doc()
        entity = self._require_entity(doc, handle)
        entity.dxf.color = color_index(color)

    def _require_entity(self, doc: Drawing, handle: str) -> DXFGraphic:
        entity = doc.entitydb.get(str(handle))
        msp = doc.modelspace()
        if (
            entity is None
            or not entity.is_alive
            or not isinstance(entity, DXFGraphic)
            or entity.dxf.owner != msp.block_record_handle
        ):
            raise EntityNotFound(
                f"Aucune entité de l'espace objet ne porte le handle {handle!r}", handle=handle
            )
        return entity

    def query(
        self, selector: EntityFilter | None = None, *, limit: int | None = None
    ) -> list[EntityInfo]:
        """Liste les entités correspondant au filtre."""
        if limit is not None and limit <= 0:
            raise InvalidParameter(f"Limite invalide: {limit}", valid="entier strictement positif")
        entities = self._select(selector)
        if limit is not None:
            entities = entities[:limit]
        cache = ezdxf_bbox.Cache()
        return [_info(entity, cache) for entity in entities]

    def count(self, selector: EntityFilter | None = None) -> int:
        """Compte les entités sans construire de description."""
        return len(self._select(selector))

    def _select(self, selector: EntityFilter | None) -> list[DXFGraphic]:
        """Applique le filtre, en déléguant à ``ezdxf`` ce qu'il sait faire.

        Le type DXF, le calque et une couleur explicite passent par la requête
        native d'``ezdxf``, qui indexe les entités. Restent côté Python les
        critères qu'elle ne sait pas exprimer: les handles, la fenêtre, et la
        couleur ByLayer, car une entité dépourvue d'attribut ``color`` est
        ByLayer sans que la requête puisse la trouver.
        """
        doc = self._require_doc()
        msp = doc.modelspace()
        if selector is None or selector.is_empty:
            return list(msp)

        if selector.handles is not None:
            entities = []
            for handle in selector.handles:
                entity = doc.entitydb.get(str(handle))
                if (
                    entity is not None
                    and entity.is_alive
                    and isinstance(entity, DXFGraphic)
                    and entity.dxf.owner == msp.block_record_handle
                ):
                    entities.append(entity)
        else:
            entities = list(msp.query(_query_string(selector)))

        if selector.handles is not None or _needs_python_filter(selector):
            entities = [e for e in entities if _matches(e, selector)]
        if selector.window is not None:
            window = _normalized_window(selector.window)
            cache = ezdxf_bbox.Cache()
            entities = [e for e in entities if _inside(e, window, cache)]
        return entities

    # ---- lecture ------------------------------------------------------

    def document_info(self) -> dict[str, Any]:
        """Métadonnées du document courant."""
        doc = self._require_doc()
        msp = doc.modelspace()
        return {
            "backend": self.name,
            "path": str(self._path) if self._path is not None else None,
            "dxfversion": doc.dxfversion,
            "release": doc.acad_release,
            "unit": self._unit.value,
            "insunits": int(doc.header.get("$INSUNITS", 0)),
            "entity_count": len(msp),
            "layers": [
                {
                    "name": layer.dxf.name,
                    "color": layer.dxf.color,
                    "linetype": layer.dxf.linetype,
                    "description": layer.description,
                    "on": layer.is_on(),
                    "locked": layer.is_locked(),
                }
                for layer in doc.layers
            ],
            "extents": list(self.extents() or ()) or None,
            "undo_depth": len(self._undo_stack),
        }

    def extents(self) -> tuple[float, float, float, float] | None:
        """Boîte englobante de l'espace objet, ``None`` si le dessin est vide."""
        doc = self._require_doc()
        box = ezdxf_bbox.extents(doc.modelspace(), fast=False)
        if not box.has_data:
            return None
        return (
            float(box.extmin.x),
            float(box.extmin.y),
            float(box.extmax.x),
            float(box.extmax.y),
        )

    # ---- persistance --------------------------------------------------

    def save(self, path: str | None = None) -> str:
        """Enregistre le document et rend le chemin absolu du fichier écrit."""
        doc = self._require_doc()
        target = Path(path).expanduser() if path is not None else self._path
        if target is None:
            raise InvalidParameter(
                "Aucun chemin d'enregistrement",
                remedy="passer un chemin à save() ou construire le backend avec un chemin",
            )
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc.saveas(str(target))
        except (OSError, dxf_const.DXFError) as exc:
            raise OperationFailed(f"Enregistrement impossible: {exc}", path=str(target)) from exc
        self._path = target
        return str(target)

    # ---- rendu --------------------------------------------------------

    def render_png(self, width: int = 1600, height: int = 1200) -> bytes:
        """Rend le dessin en PNG. Implémente ``base.SupportsRender``."""
        from ..render import render_png as _render

        return _render(self._require_doc(), width, height)


# ---- utilitaires de module --------------------------------------------


def _valid_layer_name(name: str) -> str:
    """Valide un nom de calque, sans se prononcer sur son existence."""
    label = name.strip()
    if not label:
        raise InvalidParameter("Nom de calque vide", valid="chaîne non vide")
    if not dxf_validator.is_valid_layer_name(label):
        raise InvalidParameter(
            f"Nom de calque invalide: {label!r}",
            valid="sans les caractères < > / \\ \" : ; ? * | = `",
        )
    return label


def _note_layer(result: BatchResult, name: str) -> None:
    """Rapporte un calque mis en place par le lot, une seule fois."""
    if name not in result.layers:
        result.layers.append(name)


def _note_block(result: BatchResult, name: str) -> None:
    """Rapporte un bloc défini ou confirmé par le lot, une seule fois."""
    if name not in result.blocks:
        result.blocks.append(name)


def _ref(entity: DXFGraphic) -> EntityRef:
    """Référence portant le handle réellement attribué par ``ezdxf``."""
    return EntityRef(entity.dxf.handle, entity.dxftype(), entity.dxf.layer)


def _wants_upright(attdef: Any) -> bool:
    """Vrai si la définition d'attribut demande un texte maintenu horizontal.

    L'intention est lue dans les données étendues écrites par
    :meth:`EzdxfBackend._add_attdef`, jamais devinée: un bloc venu d'ailleurs
    n'en porte pas et garde donc le comportement du format.
    """
    if not attdef.has_xdata(XDATA_APPID):
        return False
    return any(
        code == 1000 and value == XDATA_KEEP_UPRIGHT
        for code, value in attdef.get_xdata(XDATA_APPID)
    )


def _straighten(attrib: Any) -> None:
    """Redresse un attribut déjà posé, sans le déplacer d'un pouce.

    Le texte d'un attribut s'ancre sur un point et tourne autour de lui: remettre
    la rotation à zéro le redresse sur place. Deux artefacts de transformation
    doivent aussi être défaits, sans quoi le repère resterait illisible.

    * Une échelle négative ne retourne pas le texte par sa rotation mais en
      inversant la **direction d'extrusion** du repère objet. Le point d'ancrage
      est alors exprimé dans ce repère retourné: on le ramène donc en
      coordonnées du dessin avant de rétablir l'extrusion, faute de quoi le
      repère sauterait de l'autre côté de l'axe.
    * Les indicateurs de génération de texte, miroir horizontal et vertical,
      qu'un logiciel peut poser au lieu d'inverser l'extrusion.
    """
    ocs = attrib.ocs()
    anchor = ocs.to_wcs(attrib.dxf.insert)
    aligned = attrib.dxf.hasattr("align_point")
    align_anchor = ocs.to_wcs(attrib.dxf.align_point) if aligned else None

    attrib.dxf.extrusion = (0.0, 0.0, 1.0)
    attrib.dxf.rotation = 0.0
    attrib.dxf.text_generation_flag = 0
    attrib.dxf.insert = anchor
    if align_anchor is not None:
        attrib.dxf.align_point = align_anchor


def _info(entity: DXFGraphic, cache: ezdxf_bbox.Cache) -> EntityInfo:
    box = ezdxf_bbox.extents([entity], fast=False, cache=cache)
    bounds = (
        (float(box.extmin.x), float(box.extmin.y), float(box.extmax.x), float(box.extmax.y))
        if box.has_data
        else None
    )
    return EntityInfo(
        handle=entity.dxf.handle,
        kind=entity.dxftype(),
        layer=entity.dxf.layer,
        color=int(entity.dxf.get("color", BYLAYER)),
        bbox=bounds,
        extra=_extra(entity),
    )


def _extra(entity: DXFGraphic) -> dict[str, Any]:
    """Le peu d'information qui permet au modèle de reconnaître une entité.

    Volontairement bref: une requête sert à se repérer, pas à rapatrier le
    dessin dans le contexte du modèle.

    S'y ajoutent les deux clés de mesure de ``base.MEASURE_KEYS``, ``length`` et
    ``area``, **uniquement là où elles sont exactes**. Les valeurs sont dans
    l'unité du document, sans conversion. Ce qui n'est pas calculable reste
    absent: ``ops.query`` compte alors la mesure comme manquante, ce qui est
    honnête, plutôt que de la noyer dans un total faux.
    """
    dxftype = entity.dxftype()
    if dxftype == "LINE":
        start = _point2(entity.dxf.start, "start")
        end = _point2(entity.dxf.end, "end")
        return {"length": math.dist(start, end)}
    if dxftype == "CIRCLE":
        radius = float(entity.dxf.radius)
        return {
            "radius": radius,
            "length": 2.0 * math.pi * radius,
            "area": math.pi * radius * radius,
        }
    if dxftype == "ARC":
        # Un arc est une ligne courbe: il a une longueur, jamais une aire.
        radius = float(entity.dxf.radius)
        sweep = _arc_sweep(entity)
        info: dict[str, Any] = {"radius": radius}
        if sweep > 0.0:
            info["length"] = radius * sweep
        return info
    if dxftype == "TEXT":
        return {"text": entity.dxf.text}
    if dxftype == "MTEXT":
        return {"text": getattr(entity, "text", "")}
    if dxftype == "LWPOLYLINE":
        # Lu par attribut DXF plutôt que par l'API de haut niveau: la même
        # description doit pouvoir être produite pour une entité venue d'un
        # fichier tiers, sans supposer la classe ezdxf qui la porte.
        flags = int(entity.dxf.get("flags", 0))
        closed = bool(flags & 1)
        described: dict[str, Any] = {
            "vertices": int(entity.dxf.get("count", 0)),
            "closed": closed,
        }
        described.update(_polyline_measures(entity, closed=closed))
        return described
    if dxftype == "HATCH":
        # Aucune aire publiée. ``ezdxf`` n'expose pas celle d'une hachure, et sa
        # seule approche, ``triangulate()``, exige une tolérance d'approximation
        # et dérive sur les bords courbes: mesurée à 78.44 pour un disque de
        # rayon cinq, dont l'aire vaut 78.54. Une aire fausse se lit sans se
        # relire, une aire absente se voit.
        return {"pattern": entity.dxf.pattern_name}
    if dxftype == "INSERT":
        return {"block": entity.dxf.name}
    return {}


def _arc_sweep(entity: DXFGraphic) -> float:
    """Ouverture d'un arc en radians, ``0.0`` si elle est indéterminable.

    Le DXF stocke deux angles, pas une ouverture. Un tour complet s'y écrit
    ``0 → 360``, qui se ramène à zéro modulo un tour: il faut donc distinguer
    l'arc entier de l'arc dégénéré, dont l'ouverture est réellement nulle et qui
    ne doit alors publier aucune longueur.
    """
    span = float(entity.dxf.end_angle) - float(entity.dxf.start_angle)
    sweep = span % 360.0
    if sweep == 0.0 and span != 0.0:
        sweep = 360.0
    return math.radians(sweep)


def _polyline_measures(entity: Any, *, closed: bool) -> dict[str, float]:
    """Longueur développée d'une polyligne légère, et son aire si elle est fermée.

    Un renflement est un arc. L'ignorer ferait mesurer la corde au lieu de
    l'arc, donc la longueur en tient compte: l'ouverture d'un renflement vaut
    ``4·arctan(bulge)``, d'où la longueur ``|corde · θ / (2·sin(θ/2))|``.

    L'aire, en revanche, n'est publiée que pour un contour **à côtés droits**.
    Retrancher ou ajouter les segments circulaires de chaque renflement est un
    calcul que ce module ne fait pas ; une aire fausse serait pire qu'absente.
    """
    points = [
        (float(x), float(y), float(bulge)) for x, y, bulge in entity.get_points("xyb")
    ]
    if len(points) < 2:
        return {}

    edges = list(pairwise(points))
    if closed:
        edges.append((points[-1], points[0]))

    length = 0.0
    bulged = False
    for (x1, y1, bulge), (x2, y2, _) in edges:
        chord = math.hypot(x2 - x1, y2 - y1)
        if bulge:
            bulged = True
            theta = 4.0 * math.atan(bulge)
            length += abs(chord * theta / (2.0 * math.sin(theta / 2.0)))
        else:
            length += chord

    measures = {"length": length}
    if closed and not bulged:
        # ``ezdxf.math.area`` rend un flottant numpy, que ``json`` ne sait pas
        # sérialiser: la mesure remonterait jusqu'à la réponse MCP pour y
        # échouer. On le ramène donc au type du langage.
        measures["area"] = float(abs(ezdxf_area([(x, y) for x, y, _ in points])))
    return measures


def _query_string(selector: EntityFilter) -> str:
    """Construit la requête native ``ezdxf`` couverte par le filtre."""
    dxftype = "*"
    if selector.kind is not None:
        key = selector.kind.strip().lower()
        dxftype = KIND_TO_DXFTYPE.get(key, selector.kind.strip().upper())
    attributes: list[str] = []
    if selector.layer is not None and '"' not in selector.layer:
        attributes.append(f'layer=="{selector.layer}"')
    if selector.color is not None and selector.color != BYLAYER:
        attributes.append(f"color=={int(selector.color)}")
    if attributes:
        return f"{dxftype}[{' & '.join(attributes)}]"
    return dxftype


def _needs_python_filter(selector: EntityFilter) -> bool:
    """Vrai si la requête native ne couvre pas tout le filtre."""
    if selector.color is not None and selector.color == BYLAYER:
        return True
    return selector.layer is not None and '"' in selector.layer


def _matches(entity: DXFGraphic, selector: EntityFilter) -> bool:
    if selector.layer is not None and entity.dxf.layer.lower() != selector.layer.lower():
        return False
    if selector.color is not None and int(entity.dxf.get("color", BYLAYER)) != selector.color:
        return False
    if selector.kind is not None:
        key = selector.kind.strip().lower()
        expected = KIND_TO_DXFTYPE.get(key, selector.kind.strip().upper())
        if entity.dxftype() != expected:
            return False
    return True


def _normalized_window(window: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = (float(v) for v in window)
    return (min(xmin, xmax), min(ymin, ymax), max(xmin, xmax), max(ymin, ymax))


def _inside(
    entity: DXFGraphic, window: tuple[float, float, float, float], cache: ezdxf_bbox.Cache
) -> bool:
    """Sélection par fenêtre au sens d'AutoCAD: entité entièrement contenue."""
    box = ezdxf_bbox.extents([entity], fast=False, cache=cache)
    if not box.has_data:
        return False
    xmin, ymin, xmax, ymax = window
    return bool(
        box.extmin.x >= xmin
        and box.extmin.y >= ymin
        and box.extmax.x <= xmax
        and box.extmax.y <= ymax
    )


def _point2(value: Any, label: str) -> tuple[float, float]:
    try:
        x, y = (float(value[0]), float(value[1]))
    except (TypeError, ValueError, IndexError) as exc:
        raise InvalidParameter(f"Point 2D invalide pour {label}: {value!r}") from exc
    if not (math.isfinite(x) and math.isfinite(y)):
        raise InvalidGeometry(f"Coordonnée non finie pour {label}: {value!r}")
    return (x, y)


def _point3(value: Any, label: str) -> tuple[float, float, float]:
    try:
        coords = [float(c) for c in tuple(value)[:3]]
    except (TypeError, ValueError) as exc:
        raise InvalidParameter(f"Point invalide pour {label}: {value!r}") from exc
    if len(coords) == 2:
        coords.append(0.0)
    if len(coords) != 3:
        raise InvalidParameter(f"Point invalide pour {label}: {value!r}", valid="2 ou 3 réels")
    if not all(math.isfinite(c) for c in coords):
        raise InvalidGeometry(f"Coordonnée non finie pour {label}: {value!r}")
    return (coords[0], coords[1], coords[2])


def _positive(value: float, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise InvalidGeometry(f"Valeur non strictement positive pour {label}: {value!r}")
    return number


def _same_point(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return all(math.isclose(p, q, abs_tol=1e-12) for p, q in zip(a, b, strict=True))
