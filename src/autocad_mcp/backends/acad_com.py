"""Backend AutoCAD piloté par l'API ActiveX, via ``pywin32``.

Ce module est le seul endroit du projet autorisé à toucher ``win32com`` et
``pythoncom``. Il reste **importable sur macOS et Linux**: aucun symbole COM
n'est résolu au chargement, uniquement à l'appel de :meth:`AcadComBackend.connect`.
Un import raté devient une :class:`~autocad_mcp.errors.BackendUnavailable`, jamais
une ``ImportError`` qui ferait planter le serveur MCP au démarrage.

Ce qui est repris du code historique
------------------------------------
* La recette VARIANT des points, seule forme qui fonctionne réellement:
  ``VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x, y, z])``. Une liste Python
  nue échoue de façon obscure côté AutoCAD.
* ``GetActiveObject("AutoCAD.Application")`` d'abord, ``Dispatch`` en secours.

Ce qui est corrigé
------------------
* **Plus de ``time.sleep(1)`` par opération.** L'ancien ``safe_operation``
  dormait une seconde après chaque entité, réussie ou non, et avalait l'erreur.
  Un plan de deux cents entités coûtait donc plus de trois minutes de sommeil et
  rapportait un succès même lorsque rien n'avait été dessiné. Les seules
  temporisations qui subsistent sont nommées, justifiées et configurables:
  l'attente du démarrage d'AutoCAD et la reprise sur ``RPC_E_CALL_REJECTED``.
* **Un seul ``Regen`` à la fin du lot**, au lieu d'un par entité.
* **Un seul aller-retour COM par lot**: tout le lot est exécuté dans une unique
  fonction soumise au thread STA.
* **Recherche par handle en temps constant** avec ``Document.HandleToObject``,
  au lieu de parcourir le ModelSpace, qui coûtait un appel interprocessus par
  entité et retournait parfois la mauvaise entité.
* **Filtrage côté AutoCAD** par jeux de sélection et codes DXF, au lieu de
  boucles Python sur tout le dessin.
* **Aucun succès sans preuve.** Les handles renvoyés viennent de
  ``entity.Handle``. Le code historique fabriquait ``"line_created"`` quand
  l'opération échouait, ce qui rendait toute correction impossible.
* **Aucun ``except`` nu.** Seul ``pythoncom.com_error`` est intercepté, et il est
  toujours traduit en exception typée du module ``errors``.
* **``SendCommand`` n'est plus la voie d'exécution par défaut.** Il ne sert plus
  qu'à :meth:`AcadComBackend.run_command`, derrière une liste blanche et un
  assainissement du texte transmis, et ce qu'il rapporte ne prétend jamais que
  la commande a abouti: l'appel est asynchrone et ne rend rien.

Deux tables du document, jamais des entités
-------------------------------------------
``EnsureLayer`` alimente la table des calques, ``DefineBlock`` celle des blocs.
Ni l'une ni l'autre ne dessine, donc aucune ne rend de handle: elles se
rapportent dans ``BatchResult.layers`` et ``BatchResult.blocks``. Les deux ont
la même sémantique de garantie de présence, sans écrasement de l'existant. Ce
découpage est celui du contrat et celui du backend ``ezdxf``, qui sert de
référence vérifiable.

Modèle d'exécution
------------------
COM est cloisonné par appartement: un pointeur d'interface obtenu sur un thread
n'est pas utilisable tel quel sur un autre. Tous les appels passent donc par un
unique thread worker qui a fait ``pythoncom.CoInitialize()`` (STA). L'API
publique de ce module est **synchrone et bloquante**; un appelant qui tourne sur
une boucle asyncio doit l'envelopper dans ``asyncio.to_thread`` pour ne pas
bloquer la boucle, exactement ce que l'ancien serveur ne faisait pas.

Limite de vérification
----------------------
Ce fichier a été écrit sur macOS, sans AutoCAD. **Rien de ce qui touche COM n'a
pu être exécuté.** Chaque point non vérifiable est signalé par un commentaire
``HYPOTHÈSE`` et repris dans ``docs/windows-checklist.md``. Les recettes dont la
forme exacte est incertaine sont implémentées en cascade: on tente la variante
la plus probable, et on retombe sur une autre si AutoCAD la refuse, la variante
gagnante étant mémorisée pour la session.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from queue import Queue
from typing import TYPE_CHECKING, Any, TypeVar

from ..errors import (
    BackendUnavailable,
    CadError,
    ConfirmationRequired,
    EntityNotFound,
    InvalidGeometry,
    InvalidParameter,
    NotConnected,
    OperationFailed,
)
from ..model.layers import color_index
from ..model.ops import (
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
    validate,
)
from ..units import INSUNITS, Unit
from .base import BatchResult, CadBackend, EntityFilter, EntityInfo, EntityRef

if TYPE_CHECKING:  # pragma: no cover - uniquement pour l'annotation de types
    from types import ModuleType

_LOG = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Constantes ActiveX
# ---------------------------------------------------------------------------
# ``win32com.client.constants`` n'est peuplé que si la bibliothèque de types
# d'AutoCAD a été générée par makepy. En liaison tardive pure, elle est vide et
# ``constants.acActiveViewport`` lève ``AttributeError``. C'est très
# probablement ce qui se passait dans le code historique: chaque ``Regen`` était
# enveloppé dans ``safe_operation``, qui avalait l'exception, donc le regen ne
# s'exécutait sans doute jamais.
#
# On résout donc chaque constante à l'exécution, avec repli sur la valeur
# littérale ci-dessous. HYPOTHÈSE: ces valeurs viennent de la documentation
# ActiveX et de sources communautaires, pas d'une lecture de la typelib. Elles
# sont à confirmer sous Windows via :meth:`AcadComBackend.diagnostics`.
_FALLBACK_CONSTANTS: dict[str, int] = {
    # AcRegenType. Non publiées numériquement par Autodesk.
    "acAllViewports": 2,
    "acActiveViewport": 3,
    # AcSelect.
    "acSelectionSetWindow": 0,
    "acSelectionSetCrossing": 1,
    "acSelectionSetFence": 2,
    "acSelectionSetPrevious": 3,
    "acSelectionSetLast": 4,
    "acSelectionSetAll": 5,
    # AcHatchStyle.
    "acHatchStyleNormal": 0,
    "acHatchStyleOuter": 1,
    "acHatchStyleIgnore": 2,
    # AcPatternType.
    "acHatchPatternTypeUserDefined": 0,
    "acHatchPatternTypePreDefined": 1,
    "acHatchPatternTypeCustomDefined": 2,
    # AcAlignment.
    "acAlignmentLeft": 0,
    "acAlignmentCenter": 1,
    "acAlignmentRight": 2,
    "acAlignmentTopLeft": 6,
    "acAlignmentTopCenter": 7,
    "acAlignmentTopRight": 8,
    "acAlignmentMiddleLeft": 9,
    "acAlignmentMiddleCenter": 10,
    "acAlignmentMiddleRight": 11,
    "acAlignmentBottomLeft": 12,
    "acAlignmentBottomCenter": 13,
    "acAlignmentBottomRight": 14,
    # AcAttributeMode, passé à AcadBlock.AddAttribute. C'est un champ de bits:
    # « normal » vaut zéro, donc aucun mode particulier.
    "acAttributeModeNormal": 0,
    "acAttributeModeInvisible": 1,
}

#: ``AcadText.Alignment`` par couple (alignement horizontal, vertical).
#: HYPOTHÈSE importante: dès que ``Alignment`` vaut autre chose que
#: ``acAlignmentLeft``, AutoCAD ignore ``InsertionPoint`` et se cale sur
#: ``TextAlignmentPoint``. Le code affecte donc les deux, dans cet ordre.
_TEXT_ALIGNMENT: dict[tuple[str, str], str] = {
    ("left", "baseline"): "acAlignmentLeft",
    ("center", "baseline"): "acAlignmentCenter",
    ("right", "baseline"): "acAlignmentRight",
    ("left", "top"): "acAlignmentTopLeft",
    ("center", "top"): "acAlignmentTopCenter",
    ("right", "top"): "acAlignmentTopRight",
    ("left", "middle"): "acAlignmentMiddleLeft",
    ("center", "middle"): "acAlignmentMiddleCenter",
    ("right", "middle"): "acAlignmentMiddleRight",
    ("left", "bottom"): "acAlignmentBottomLeft",
    ("center", "bottom"): "acAlignmentBottomCenter",
    ("right", "bottom"): "acAlignmentBottomRight",
}

#: HRESULT renvoyés quand AutoCAD est occupé, typiquement parce que
#: l'utilisateur est au milieu d'une commande. Ce sont les seules erreurs pour
#: lesquelles une nouvelle tentative a un sens.
_RPC_E_CALL_REJECTED = -2147418111  # 0x80010001
_RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
_BUSY_HRESULTS = frozenset({_RPC_E_CALL_REJECTED, _RPC_E_SERVERCALL_RETRYLATER})

#: ``DISP_E_EXCEPTION``: le vrai code se trouve alors dans ``excepinfo[5]``.
_DISP_E_EXCEPTION = -2147352567

#: Nom d'objet ObjectARX vers nom d'opération du projet.
_OBJECT_NAME_TO_KIND: dict[str, str] = {
    "AcDbLine": "line",
    "AcDbPolyline": "polyline",
    "AcDb2dPolyline": "polyline",
    "AcDb3dPolyline": "polyline",
    "AcDbCircle": "circle",
    "AcDbArc": "arc",
    "AcDbText": "text",
    "AcDbMText": "mtext",
    "AcDbHatch": "hatch",
    "AcDbAlignedDimension": "dim_aligned",
    "AcDbRotatedDimension": "dim_linear",
    "AcDbBlockReference": "block_ref",
    "AcDbEllipse": "ellipse",
    "AcDbSpline": "spline",
    "AcDbPoint": "point",
    "AcDbSolid": "solid",
}

#: Nom d'opération du projet vers nom de type DXF, pour le code de groupe 0.
#: Attention: le groupe 0 attend le nom **DXF** (``LWPOLYLINE``), pas le nom
#: ObjectARX (``AcDbPolyline``). Toutes les cotations partagent ``DIMENSION``.
_KIND_TO_DXF: dict[str, str] = {
    "line": "LINE",
    "polyline": "LWPOLYLINE",
    "circle": "CIRCLE",
    "arc": "ARC",
    "text": "TEXT",
    "mtext": "MTEXT",
    "hatch": "HATCH",
    "dim_aligned": "DIMENSION",
    "dim_linear": "DIMENSION",
    "block_ref": "INSERT",
    "ellipse": "ELLIPSE",
    "spline": "SPLINE",
    "point": "POINT",
    "solid": "SOLID",
}

#: Code $INSUNITS vers unité du projet.
_UNIT_BY_INSUNITS: dict[int, Unit] = {code: unit for unit, code in INSUNITS.items()}

#: Valeur sentinelle d'EXTMIN/EXTMAX dans un dessin vide.
_EXTENTS_SENTINEL = 1.0e19


# ---------------------------------------------------------------------------
# Commandes natives: liste blanche et assainissement
# ---------------------------------------------------------------------------
# ``SendCommand`` transmet du texte à l'interpréteur d'AutoCAD. Ce texte peut
# contenir une expression AutoLISP, laquelle sait ouvrir un fichier, lancer un
# processus ou joindre le réseau: transmettre une chaîne libre revient donc à
# exécuter du code arbitraire sur la machine de l'utilisateur.
#
# ``Config.allowed_commands`` filtre déjà côté outil. Le backend refait le
# contrôle: un moteur qui fait confiance à son appelant n'est pas défendu, il
# est seulement défendu ailleurs. Les deux listes doivent rester identiques,
# celle-ci est recopiée volontairement plutôt qu'importée pour que le backend
# ne dépende pas de la configuration du serveur.

#: Commandes natives que ce backend accepte de transmettre. Miroir de
#: ``config.Config.allowed_commands``.
_DEFAULT_ALLOWED_COMMANDS = frozenset(
    {"OFFSET", "TRIM", "EXTEND", "FILLET", "CHAMFER", "ARRAY", "MIRROR", "BOUNDARY", "HATCH"}
)

#: Caractères qui, dans le texte d'une commande, changent sa nature.
#:
#: ``(`` ouvre une expression AutoLISP et ``'`` la cite ; ``;`` et les fins de
#: ligne valent ENTRÉE et permettent donc d'enchaîner une seconde commande
#: derrière la première ; ``"`` délimite une chaîne LISP ; ``\`` échappe ;
#: ``!`` déréférence une variable LISP ; ``\x1b`` est ÉCHAP. Aucun de ces
#: caractères n'a de raison d'être dans un nom de commande ni dans un argument
#: géométrique, et leur présence est le signe d'une tentative d'injection.
_COMMAND_INJECTION_CHARS = ("\n", "\r", "\t", "\x00", "\x1b", ";", "(", ")", '"', "'", "\\", "!")

#: Nom de commande acceptable, une fois retirés les préfixes ``.`` et ``_``.
#: Une lettre puis des lettres ou des chiffres: pas d'espace, donc pas de
#: seconde commande cachée derrière une espace, qui vaut ENTRÉE.
_COMMAND_NAME = re.compile(r"\A[A-Za-z][A-Za-z0-9]*\Z")

#: Argument acceptable: une coordonnée (``12.5,-3``), un nombre, un mot-clé
#: d'option international (``_T``), une référence relative (``@1,0``). Tout le
#: reste est refusé, y compris l'espace, qui vaut ENTRÉE pour AutoCAD.
_COMMAND_ARGUMENT = re.compile(r"\A[A-Za-z0-9_.,+@<-]{1,64}\Z")

#: Application propriétaire sous laquelle le projet inscrit ses données étendues.
#: Doit rester identique à ``ezdxf_be.XDATA_APPID``: c'est le même dessin qui
#: passe d'un moteur à l'autre, et une consigne écrite par l'un doit être relue
#: par l'autre.
_XDATA_APPID = "AUTOCAD_MCP"

#: Marqueur d'un attribut à maintenir horizontal, voir ``ops.AttributeDef``.
#: Identique à ``ezdxf_be.XDATA_KEEP_UPRIGHT``.
_XDATA_KEEP_UPRIGHT = "KEEP_UPRIGHT"

#: Code de groupe d'une chaîne de données étendues, et celui du nom
#: d'application qui doit ouvrir toute liste de XDATA.
_XDATA_STRING = 1000
_XDATA_APPNAME = 1001

#: Nom ObjectARX d'une définition d'attribut, telle qu'on la retrouve en
#: parcourant le contenu d'un bloc.
_ATTDEF_OBJECT_NAME = "AcDbAttributeDefinition"

#: Nom ObjectARX vers propriétés ActiveX portant les mesures de ``MEASURE_KEYS``.
#: Voir :meth:`AcadComBackend._measures`.
_LENGTH_PROPERTY: dict[str, str] = {
    "AcDbLine": "Length",
    "AcDbPolyline": "Length",
    "AcDb2dPolyline": "Length",
    "AcDbCircle": "Circumference",
    "AcDbArc": "ArcLength",
}


# ---------------------------------------------------------------------------
# Thread STA
# ---------------------------------------------------------------------------


class _Task:
    """Une fonction à exécuter sur le thread COM, et son résultat."""

    __slots__ = ("done", "error", "fn", "result")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class _StaWorker:
    """Thread unique en appartement cloisonné, par lequel passe tout appel COM.

    COM associe un pointeur d'interface au thread qui l'a obtenu. Utiliser le
    document AutoCAD depuis plusieurs threads sans marshalling donne des
    ``RPC_E_WRONG_THREAD`` ou, pire, des corruptions silencieuses. Ce worker
    garantit qu'un seul thread, celui qui a fait ``CoInitialize``, touche COM.

    Le thread est ``daemon``: si le processus hôte s'arrête sans appeler
    :meth:`AcadComBackend.close`, il ne retient pas l'interpréteur.
    """

    def __init__(self, *, name: str = "acad-com-sta", call_timeout: float = 300.0) -> None:
        self._queue: Queue[_Task | None] = Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._name = name
        self._call_timeout = call_timeout
        self._pythoncom: Any = None

    @property
    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, pythoncom: Any) -> None:
        """Démarre le thread et attend que ``CoInitialize`` ait abouti."""
        with self._lock:
            if self.alive:
                return
            self._pythoncom = pythoncom
            self._ready.clear()
            self._startup_error = None
            thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread = thread
            thread.start()

        if not self._ready.wait(timeout=self._call_timeout):
            raise BackendUnavailable(
                "Le thread COM ne s'est pas initialisé dans le délai imparti",
                timeout_s=self._call_timeout,
            )
        if self._startup_error is not None:
            raise BackendUnavailable(
                f"CoInitialize a échoué: {self._startup_error}",
                cause=type(self._startup_error).__name__,
            )

    def _run(self) -> None:
        try:
            # CoInitialize, et non CoInitializeEx(COINIT_MULTITHREADED): AutoCAD
            # est un serveur STA, le modèle cloisonné est le seul correct ici.
            self._pythoncom.CoInitialize()
        except BaseException as exc:
            # Relayée telle quelle au thread appelant: une erreur d'init COM ne
            # doit pas rester coincée dans le worker.
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    return
                try:
                    task.result = task.fn()
                except BaseException as exc:
                    # Capturée ici, relevée à l'identique dans submit(). Le
                    # worker ne doit jamais mourir sur l'erreur d'une tâche.
                    task.error = exc
                finally:
                    task.done.set()
        finally:
            try:
                self._pythoncom.CoUninitialize()
            except Exception as exc:
                # Fin de vie du thread: plus rien à sauver, mais on trace.
                _LOG.debug("CoUninitialize a échoué: %s", exc)

    def submit(self, fn: Callable[[], T]) -> T:
        """Exécute ``fn`` sur le thread COM et rend son résultat.

        Bloque l'appelant. Les exceptions levées dans le worker sont relevées
        ici, à l'identique: aucune erreur n'est convertie en valeur de retour.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            raise NotConnected(
                "Le thread COM n'est pas démarré. Appelez connect() d'abord.",
            )
        if threading.current_thread() is thread:
            # Réentrance: une fonction déjà exécutée sur le worker qui
            # resoumettrait s'attendrait elle-même indéfiniment.
            return fn()

        task = _Task(fn)
        self._queue.put(task)
        if not task.done.wait(timeout=self._call_timeout):
            raise OperationFailed(
                "AutoCAD n'a pas répondu dans le délai imparti. "
                "Une boîte de dialogue modale est peut-être ouverte.",
                timeout_s=self._call_timeout,
            )
        if task.error is not None:
            raise task.error
        return task.result  # type: ignore[no-any-return]

    def stop(self, *, join_timeout: float = 10.0) -> None:
        """Arrête le thread. Idempotent."""
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            _LOG.warning("Le thread COM ne s'est pas arrêté dans le délai imparti")


@dataclass(slots=True)
class _DocContext:
    """Caches valables le temps d'une opération, pour éviter les allers-retours.

    Les tables de calques et de types de ligne sont lues une fois par lot plutôt
    qu'interrogées entité par entité.
    """

    #: Noms existants, en majuscules: AutoCAD ignore la casse des calques.
    layers: set[str] = field(default_factory=set)
    linetypes: set[str] | None = None
    #: Calques créés ou confirmés par le lot, dans leur casse d'origine et dans
    #: l'ordre de rencontre. Alimente ``BatchResult.layers``, qui est distinct
    #: de ``created``: un calque n'est pas une entité de l'espace objet.
    touched: list[str] = field(default_factory=list)
    #: Présence dans la table des blocs, par nom en majuscules. Rempli à la
    #: demande, un nom à la fois: sonder un nom coûte un appel, énumérer la
    #: table en coûte un par bloc, et un dessin de production en compte des
    #: centaines dont aucune n'intéresse le lot.
    blocks: dict[str, bool] = field(default_factory=dict)
    #: Blocs définis ou confirmés par le lot. Alimente ``BatchResult.blocks``,
    #: pour la même raison que ``touched`` alimente ``layers``: une définition
    #: vit dans la table des blocs, elle n'est pas dessinée.
    touched_blocks: list[str] = field(default_factory=list)
    #: Étiquettes à maintenir horizontales, par nom de bloc en majuscules. Lire
    #: cette consigne demande de parcourir le contenu d'un bloc et d'interroger
    #: les données étendues de chaque définition d'attribut: c'est cher, et une
    #: planche en insère la même vingtaine de fois.
    upright_tags: dict[str, frozenset[str]] = field(default_factory=dict)

    def note_layer(self, name: str) -> None:
        if name not in self.touched:
            self.touched.append(name)

    def note_block(self, name: str) -> None:
        if name not in self.touched_blocks:
            self.touched_blocks.append(name)


@dataclass(slots=True)
class _BatchRecord:
    """Trace d'un lot exécuté, pour :meth:`AcadComBackend.undo`."""

    label: str
    handles: tuple[str, ...]


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class AcadComBackend(CadBackend):
    """Moteur de dessin branché sur une instance AutoCAD par ActiveX.

    Exige Windows, ``pywin32`` et une installation AutoCAD complète. AutoCAD LT
    n'expose pas l'automatisation ActiveX et ne conviendra pas.

    Toutes les méthodes publiques sont synchrones et bloquantes. Sur une boucle
    asyncio, les envelopper dans ``asyncio.to_thread``.

    :param create_if_missing: lancer AutoCAD si aucune instance n'est en cours.
    :param default_unit: unité retenue quand ``$INSUNITS`` vaut 0 (sans unité)
        ou une valeur que le projet ne gère pas.
    :param call_timeout: délai maximal d'un appel COM, en secondes. Dépassé
        typiquement lorsqu'une boîte de dialogue modale attend l'utilisateur.
    :param startup_timeout: délai d'attente du démarrage d'AutoCAD.
    :param startup_poll_interval: pas de scrutation pendant ce démarrage. C'est
        la seule temporisation de confort admise, et elle est bornée.
    :param busy_retries: nombre de reprises sur ``RPC_E_CALL_REJECTED``.
    :param busy_retry_delay: délai entre deux reprises, en secondes.
    :param undo_depth: nombre de lots conservés pour :meth:`undo`.
    :param allowed_commands: liste blanche des commandes natives transmissibles
        par :meth:`run_command`. Par défaut ``_DEFAULT_ALLOWED_COMMANDS``, qui
        recopie ``config.Config.allowed_commands``. Le backend refuse tout ce
        qui n'y figure pas, même si la couche outil l'a laissé passer.
    """

    name = "autocad"

    def __init__(
        self,
        *,
        create_if_missing: bool = True,
        default_unit: Unit = Unit.MILLIMETER,
        call_timeout: float = 300.0,
        startup_timeout: float = 120.0,
        startup_poll_interval: float = 0.5,
        busy_retries: int = 8,
        busy_retry_delay: float = 0.25,
        undo_depth: int = 32,
        selection_set_name: str = "MCP_FILTER",
        allowed_commands: frozenset[str] | None = None,
    ) -> None:
        self._create_if_missing = create_if_missing
        self._default_unit = default_unit
        self._startup_timeout = startup_timeout
        self._startup_poll_interval = startup_poll_interval
        self._busy_retries = max(0, int(busy_retries))
        self._busy_retry_delay = max(0.0, float(busy_retry_delay))
        self._undo_depth = max(0, int(undo_depth))
        # Un nom de jeu de sélection est limité à 31 caractères et interdit les
        # espaces. On tronque plutôt que d'échouer à l'usage.
        self._selection_set_name = selection_set_name.replace(" ", "_")[:31]
        self._allowed_commands = frozenset(
            name.strip().upper()
            for name in (
                _DEFAULT_ALLOWED_COMMANDS if allowed_commands is None else allowed_commands
            )
        )

        self._win32: ModuleType | None = None
        self._pythoncom: Any = None
        self._com_error: type[BaseException] = _NeverRaised
        self._app: Any = None
        self._doc: Any = None
        self._consts: dict[str, int] = {}
        self._undo_stack: list[_BatchRecord] = []
        self._select_strategy: str | None = None
        self._hatch_loop_strategy: str | None = None
        #: L'application des données étendues a déjà été déclarée dans ce
        #: document. La déclarer est idempotent mais coûte un aller-retour.
        self._xdata_registered = False
        #: Repères à maintenir horizontaux, par nom de bloc défini pendant cette
        #: session. Filet de sécurité du marquage par données étendues, voir
        #: :meth:`_upright_tags`.
        self._upright_by_block: dict[str, frozenset[str]] = {}
        self._worker = _StaWorker(call_timeout=call_timeout)

    # -- chargement paresseux de COM ------------------------------------

    def _ensure_modules(self) -> None:
        """Importe ``pywin32`` au premier besoin, jamais au chargement du module.

        C'est ce qui permet au reste du projet, aux tests et à l'outillage de
        s'importer sur macOS. L'échec est une ``BackendUnavailable``, pas une
        ``ImportError``: le serveur MCP doit pouvoir répondre « ce backend n'est
        pas disponible ici » au lieu de refuser de démarrer.
        """
        if self._pythoncom is not None:
            return

        if sys.platform != "win32":
            raise BackendUnavailable(
                "Le backend AutoCAD exige Windows. Utilisez le backend DXF sur "
                "cette machine.",
                backend=self.name,
                platform=sys.platform,
            )

        # mypy analyse ce fichier sur la plateforme courante, macOS, où le
        # garde-fou ci-dessus a déjà levé. Le code suivant n'est atteignable
        # que sous Windows, seule plateforme où ces modules existent.
        try:  # type: ignore[unreachable]
            import pythoncom
            import win32com.client as win32client
        except ImportError as exc:
            raise BackendUnavailable(
                "pywin32 est introuvable. Installez-le avec "
                "`pip install autocad-mcp[windows]` ou `pip install pywin32`.",
                backend=self.name,
                platform=sys.platform,
                missing=getattr(exc, "name", None) or "pywin32",
            ) from exc

        self._pythoncom = pythoncom
        self._win32 = win32client
        self._com_error = pythoncom.com_error

    def _const(self, name: str, fallback: int | None = None) -> int:
        """Valeur d'une constante ActiveX, lue dans la typelib si possible.

        Si makepy a été passé sur la bibliothèque de types d'AutoCAD, la valeur
        réelle est utilisée. Sinon on retombe sur le littéral documenté dans
        ``_FALLBACK_CONSTANTS``, dont la justesse reste à confirmer.
        """
        cached = self._consts.get(name)
        if cached is not None:
            return cached

        default = _FALLBACK_CONSTANTS[name] if fallback is None else fallback
        value = default
        win32 = self._win32
        if win32 is not None:
            try:
                value = int(getattr(win32.constants, name))
            except (AttributeError, TypeError, ValueError):
                _LOG.debug(
                    "Constante %s absente de la bibliothèque de types, repli sur %d",
                    name,
                    default,
                )
        self._consts[name] = value
        return value

    # -- VARIANT ---------------------------------------------------------

    def _point(self, x: float, y: float, z: float = 0.0) -> Any:
        """Point 3D au format attendu par AutoCAD.

        Recette éprouvée en production dans le serveur historique: un tableau
        SAFEARRAY de trois doubles. Une liste Python nue provoque un échec
        obscur, souvent un ``Invalid argument`` sans indication de position.
        """
        variant = self._win32.VARIANT  # type: ignore[union-attr]
        pythoncom = self._pythoncom
        return variant(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [float(x), float(y), float(z)],
        )

    def _doubles(self, values: Sequence[float]) -> Any:
        """Tableau plat de doubles, pour les sommets de polyligne."""
        variant = self._win32.VARIANT  # type: ignore[union-attr]
        pythoncom = self._pythoncom
        return variant(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [float(v) for v in values],
        )

    def _shorts(self, values: Sequence[int]) -> Any:
        """Tableau d'entiers courts, pour les codes de groupe DXF d'un filtre."""
        variant = self._win32.VARIANT  # type: ignore[union-attr]
        pythoncom = self._pythoncom
        return variant(
            pythoncom.VT_ARRAY | pythoncom.VT_I2,
            [int(v) for v in values],
        )

    def _variants(self, values: Sequence[Any]) -> Any:
        """Tableau de VARIANT hétérogènes, pour les valeurs d'un filtre DXF."""
        variant = self._win32.VARIANT  # type: ignore[union-attr]
        pythoncom = self._pythoncom
        return variant(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, list(values))

    def _dispatches(self, objects: Sequence[Any]) -> Any:
        """Tableau d'objets COM, pour les contours de hachure."""
        variant = self._win32.VARIANT  # type: ignore[union-attr]
        pythoncom = self._pythoncom
        return variant(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, list(objects))

    # -- traduction des erreurs COM --------------------------------------

    @staticmethod
    def _com_hresult(exc: BaseException) -> int | None:
        args = getattr(exc, "args", ())
        if not args or not isinstance(args[0], int):
            return None
        hresult = int(args[0])
        if hresult == _DISP_E_EXCEPTION and len(args) > 2:
            excepinfo = args[2]
            if isinstance(excepinfo, tuple) and len(excepinfo) > 5:
                scode = excepinfo[5]
                if isinstance(scode, int):
                    return int(scode)
        return hresult

    @classmethod
    def _com_details(cls, exc: BaseException) -> dict[str, Any]:
        """Détaille une ``com_error`` de façon exploitable par le modèle."""
        args: tuple[Any, ...] = getattr(exc, "args", ())
        hresult = cls._com_hresult(exc)
        details: dict[str, Any] = {}
        if hresult is not None:
            details["hresult"] = hresult
            details["hresult_hex"] = f"0x{hresult & 0xFFFFFFFF:08X}"
        if len(args) > 1 and args[1]:
            details["com_message"] = str(args[1])
        if len(args) > 2 and isinstance(args[2], tuple) and len(args[2]) > 2:
            source, description = args[2][1], args[2][2]
            if source:
                details["com_source"] = str(source)
            if description:
                details["com_description"] = str(description)
        return details

    @classmethod
    def _is_busy(cls, exc: BaseException) -> bool:
        return cls._com_hresult(exc) in _BUSY_HRESULTS

    def _fail(self, exc: BaseException, message: str, **extra: Any) -> OperationFailed:
        """Convertit une ``com_error`` en erreur typée, sans perdre le HRESULT."""
        details = self._com_details(exc)
        details.update(extra)
        if self._is_busy(exc):
            message = (
                f"{message} — AutoCAD est occupé, probablement au milieu d'une "
                "commande interactive. Terminez la commande et réessayez."
            )
        return OperationFailed(message, **details)

    # -- soumission au worker --------------------------------------------

    def _submit(self, fn: Callable[[], T]) -> T:
        return self._worker.submit(fn)

    def _submit_read(self, fn: Callable[[], T]) -> T:
        """Soumet une lecture, avec reprise si AutoCAD est momentanément occupé.

        Les reprises ne sont admises que pour les lectures, qui sont idempotentes.
        Rejouer une écriture créerait des doublons: c'est exactement le genre de
        bug que ce projet cherche à éliminer.
        """

        def retrying() -> T:
            attempt = 0
            while True:
                try:
                    return fn()
                except self._com_error as exc:
                    if self._is_busy(exc) and attempt < self._busy_retries:
                        attempt += 1
                        # Temporisation nommée et bornée, pas un sleep de confort:
                        # AutoCAD a explicitement refusé l'appel via
                        # RPC_E_CALL_REJECTED et demande à être rappelé.
                        time.sleep(self._busy_retry_delay)
                        continue
                    raise self._fail(
                        exc, "Lecture AutoCAD impossible", attempts=attempt + 1
                    ) from exc

        return self._worker.submit(retrying)

    # -- cycle de vie -----------------------------------------------------

    def connect(self) -> None:
        """Rejoint l'instance AutoCAD en cours, ou en démarre une. Idempotent."""
        self._ensure_modules()
        self._worker.start(self._pythoncom)
        self._submit(self._connect_on_worker)

    def _connect_on_worker(self) -> None:
        if self._doc is not None and self._document_is_alive():
            return

        app = None
        try:
            app = self._win32.GetActiveObject("AutoCAD.Application")  # type: ignore[union-attr]
        except self._com_error as exc:
            if not self._create_if_missing:
                raise BackendUnavailable(
                    "Aucune instance AutoCAD en cours d'exécution et le "
                    "démarrage automatique est désactivé.",
                    backend=self.name,
                    **self._com_details(exc),
                ) from exc
            _LOG.info("Aucune instance AutoCAD trouvée, démarrage d'une nouvelle")
            app = self._launch()

        try:
            app.Visible = True
        except self._com_error as exc:
            # Non bloquant: une instance pilotée sans interface reste utilisable.
            _LOG.debug("Impossible de rendre AutoCAD visible: %s", exc)

        self._app = app
        self._doc = self._acquire_document(app)
        # La table des applications de données étendues appartient au document,
        # et les noms de blocs n'ont de sens que dans le dessin qui les porte:
        # changer de document invalide l'un et l'autre.
        self._xdata_registered = False
        self._upright_by_block.clear()
        _LOG.info("Connecté à AutoCAD, document %s", self._safe_str(self._doc, "Name"))

    def _launch(self) -> Any:
        """Démarre AutoCAD et attend qu'il réponde, dans une limite de temps.

        AutoCAD s'enregistre dans la table des objets en cours bien avant d'être
        prêt à répondre. La scrutation remplace le ``time.sleep(2)`` aveugle du
        code historique, qui était soit trop court, soit du temps perdu.
        """
        try:
            app = self._win32.Dispatch("AutoCAD.Application")  # type: ignore[union-attr]
        except self._com_error as exc:
            raise BackendUnavailable(
                "AutoCAD est introuvable sur cette machine: le serveur "
                "d'automatisation « AutoCAD.Application » n'est pas enregistré.",
                backend=self.name,
                **self._com_details(exc),
            ) from exc

        deadline = time.monotonic() + self._startup_timeout
        last: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                int(app.Documents.Count)
            except self._com_error as exc:
                last = exc
                time.sleep(self._startup_poll_interval)
                continue
            return app

        raise BackendUnavailable(
            "AutoCAD a été lancé mais n'a pas répondu dans le délai imparti.",
            backend=self.name,
            timeout_s=self._startup_timeout,
            **(self._com_details(last) if last is not None else {}),
        )

    def _acquire_document(self, app: Any) -> Any:
        """Rend le document actif, en en créant un si le bureau est vide."""
        try:
            if int(app.Documents.Count) == 0:
                return app.Documents.Add()
            return app.ActiveDocument
        except self._com_error as exc:
            raise self._fail(exc, "Impossible d'obtenir un document AutoCAD") from exc

    def _document_is_alive(self) -> bool:
        """Sonde bon marché: lire ``Name`` échoue si le document a été fermé."""
        try:
            str(self._doc.Name)
        except self._com_error:
            return False
        return True

    def close(self) -> None:
        """Relâche les pointeurs COM et arrête le thread. Ne ferme pas AutoCAD."""
        if self._worker.alive:
            # Les pointeurs doivent être relâchés dans l'appartement qui les a
            # obtenus, donc sur le worker, avant de l'arrêter.
            try:
                self._submit(self._release_on_worker)
            except Exception as exc:
                # La fermeture ne doit jamais lever: on ferme quand même.
                _LOG.debug("Relâche des objets COM impossible: %s", exc)
        self._worker.stop()
        self._app = None
        self._doc = None
        self._undo_stack.clear()

    def _release_on_worker(self) -> None:
        self._doc = None
        self._app = None
        self._xdata_registered = False
        self._upright_by_block.clear()

    def _document(self) -> Any:
        """Document courant, vérifié vivant. À n'appeler que sur le worker."""
        if self._doc is None:
            raise NotConnected(
                "Aucun document AutoCAD. Appelez connect() d'abord.",
                backend=self.name,
            )
        if not self._document_is_alive():
            self._doc = None
            raise NotConnected(
                "Le document AutoCAD a été fermé ou AutoCAD s'est arrêté. "
                "Appelez connect() pour vous reconnecter.",
                backend=self.name,
            )
        return self._doc

    def _require_connection(self) -> None:
        """Vérifie côté appelant, avant tout aller-retour, que connect() a eu lieu."""
        if not self._worker.alive or self._doc is None:
            raise NotConnected(
                "Backend AutoCAD non connecté. Appelez connect() d'abord.",
                backend=self.name,
            )

    @property
    def unit(self) -> Unit:
        """Unité du document, lue dans ``$INSUNITS``.

        Un dessin sans unité déclarée (``$INSUNITS`` à 0) est fréquent. On
        retombe alors sur ``default_unit`` plutôt que de deviner, et la valeur
        brute reste visible dans :meth:`document_info`.
        """
        self._require_connection()
        code = self._submit_read(lambda: int(self._document().GetVariable("INSUNITS")))
        unit = _UNIT_BY_INSUNITS.get(code)
        if unit is None:
            _LOG.debug("$INSUNITS=%s non géré, repli sur %s", code, self._default_unit.value)
            return self._default_unit
        return unit

    # -- écriture ---------------------------------------------------------

    def execute(self, batch: OperationBatch) -> BatchResult:
        """Exécute le lot comme une transaction unique.

        Le lot entier part sur le worker en un seul aller-retour. Il est encadré
        par ``StartUndoMark`` / ``EndUndoMark``, si bien qu'un Ctrl+Z dans
        AutoCAD annule tout le plan d'un geste. Un unique ``Regen`` clôt le lot.

        Une opération qui échoue remplit ``BatchResult.failures`` et n'interrompt
        pas les suivantes: un plan partiellement dessiné doit être rapporté comme
        tel, avec la liste exacte de ce qui manque.
        """
        self._require_connection()
        return self._submit(lambda: self._execute_on_worker(batch))

    def _execute_on_worker(self, batch: OperationBatch) -> BatchResult:
        doc = self._document()
        result = BatchResult(label=batch.label)
        if len(batch) == 0:
            return result

        try:
            model_space = doc.ModelSpace
        except self._com_error as exc:
            raise self._fail(exc, "ModelSpace inaccessible") from exc

        context = _DocContext(layers=self._layer_names(doc))

        # StartUndoMark ne prend aucun argument dans l'API ActiveX: le libellé du
        # lot ne peut pas lui être transmis. Il est conservé côté backend, dans
        # BatchResult.label et dans la pile d'annulation.
        try:
            doc.StartUndoMark()
        except self._com_error as exc:
            raise self._fail(exc, "StartUndoMark a échoué", label=batch.label) from exc

        try:
            for index, operation in enumerate(batch.operations):
                self._run_one(doc, model_space, operation, context, index, result)
        finally:
            try:
                doc.EndUndoMark()
            except self._com_error as exc:
                # Ne jamais masquer une erreur d'opération par celle-ci, mais ne
                # jamais la taire non plus: une marque non refermée fausse les
                # annulations suivantes.
                _LOG.warning("EndUndoMark a échoué: %s", exc)
                result.failures.append(
                    {
                        "index": None,
                        "operation": "end_undo_mark",
                        "error": "La marque d'annulation n'a pas pu être refermée",
                        "code": OperationFailed.code,
                        **self._com_details(exc),
                    }
                )

        # Les calques touchés sont rapportés à part. Les compter comme créés
        # gonflerait le nombre d'objets dessinés et ferait supprimer des calques
        # à l'annulation, puisque celle-ci efface tout ce que porte ``handles``.
        result.layers = context.touched
        # Même raisonnement pour les définitions de blocs: elles vivent dans la
        # table des blocs, elles ne sont pas dessinées, et les supprimer à
        # l'annulation emporterait toutes les occurrences posées par d'autres
        # lots. Seule l'occurrence ``AddBlockRef`` est une entité.
        result.blocks = context.touched_blocks

        if result.created:
            self._regen(doc)
            self._push_undo(batch.label, [ref.handle for ref in result.created])
        return result

    def _run_one(
        self,
        doc: Any,
        model_space: Any,
        operation: Operation,
        context: _DocContext,
        index: int,
        result: BatchResult,
    ) -> None:
        """Exécute une opération et consigne son issue, succès comme échec."""
        kind = getattr(operation, "kind", "unknown")
        try:
            # Validation mutualisée: un rayon négatif doit échouer à l'identique
            # sur AutoCAD et sur DXF. Dupliquer ces règles ici les ferait diverger,
            # et la suite de contrat passerait sur un moteur mais pas sur l'autre.
            validate(operation)
            refs, problems = self._apply(doc, model_space, operation, context)
        except CadError as exc:
            result.failures.append({"index": index, "operation": kind, **exc.to_dict()})
            return
        except self._com_error as exc:
            failure = self._fail(exc, f"L'opération {kind} a échoué")
            result.failures.append({"index": index, "operation": kind, **failure.to_dict()})
            return

        result.created.extend(refs)
        if problems:
            # L'entité existe mais n'a pas tous les attributs demandés. Le modèle
            # doit le savoir: un trait plein là où il fallait des pointillés est
            # une erreur de dessin, pas un détail. Une définition de bloc passe
            # par le même canal, sans handle: le bloc est écrit, mais une partie
            # de son style ou de ses attributs a été refusée.
            result.failures.append(
                {
                    "index": index,
                    "operation": kind,
                    "stage": "style",
                    "handles": [ref.handle for ref in refs],
                    "error": "; ".join(problems),
                    "code": OperationFailed.code,
                }
            )

    def _apply(
        self,
        doc: Any,
        model_space: Any,
        operation: Operation,
        context: _DocContext,
    ) -> tuple[list[EntityRef], list[str]]:
        if isinstance(operation, EnsureLayer):
            self._ensure_layer(
                doc,
                context,
                operation.name,
                color=operation.color,
                description=operation.description,
                linetype=operation.linetype,
            )
            # Créé ou simplement confirmé, le calque a été demandé explicitement:
            # il est rapporté dans BatchResult.layers, jamais dans created.
            context.note_layer(operation.name)
            return [], []

        if isinstance(operation, DefineBlock):
            # Une définition de bloc alimente la table des blocs, pas l'espace
            # objet: aucun handle n'est rendu, le nom part dans
            # BatchResult.blocks. Même raisonnement que pour un calque.
            return [], self._define_block(doc, operation, context)

        style = operation.style
        # AutoCAD refuse d'affecter une entité à un calque inexistant. On crée le
        # calque manquant plutôt que de perdre l'entité.
        self._ensure_layer(doc, context, style.layer)

        entities, problems = self._create(doc, model_space, operation, context)

        refs: list[EntityRef] = []
        for entity, entity_kind in entities:
            problems.extend(self._apply_style(doc, entity, style, context))
            try:
                handle = str(entity.Handle)
            except self._com_error as exc:
                # Sans handle, l'entité est peut-être dessinée mais inutilisable.
                # On le dit, on n'invente pas d'identifiant.
                raise self._fail(
                    exc, f"Entité {entity_kind} créée mais son handle est illisible"
                ) from exc
            refs.append(EntityRef(handle=handle, kind=entity_kind, layer=style.layer))
        return refs, problems

    def _create(
        self, doc: Any, model_space: Any, operation: Operation, context: _DocContext
    ) -> tuple[list[tuple[Any, str]], list[str]]:
        """Crée la ou les entités d'une opération. Aiguillage exhaustif.

        ``model_space`` est l'espace objet pour un lot ordinaire, mais
        :meth:`_define_block` réemprunte cet aiguillage avec un ``AcadBlock``
        pour destination: dans la hiérarchie ActiveX, ``AcadModelSpace`` dérive
        de ``AcadBlock`` et expose donc les mêmes méthodes de dessin. La
        géométrie d'un bloc s'écrit exactement comme celle du dessin.
        """
        if isinstance(operation, AddLine):
            return self._add_line(model_space, operation)
        if isinstance(operation, AddPolyline):
            return self._add_polyline(model_space, operation)
        if isinstance(operation, AddCircle):
            return self._add_circle(model_space, operation)
        if isinstance(operation, AddArc):
            return self._add_arc(model_space, operation)
        if isinstance(operation, AddText):
            return self._add_text(model_space, operation)
        if isinstance(operation, AddMText):
            return self._add_mtext(model_space, operation)
        if isinstance(operation, AddHatch):
            return self._add_hatch(model_space, operation)
        if isinstance(operation, AddDimAligned):
            return self._add_dim_aligned(model_space, operation)
        if isinstance(operation, AddBlockRef):
            return self._add_block_ref(doc, model_space, operation, context)
        raise InvalidParameter(
            f"Opération inconnue du backend AutoCAD: {type(operation).__name__}",
            backend=self.name,
        )

    # -- créations élémentaires -------------------------------------------

    def _add_line(self, model_space: Any, op: AddLine) -> tuple[list[tuple[Any, str]], list[str]]:
        line = model_space.AddLine(self._point(*op.start), self._point(*op.end))
        return [(line, "line")], []

    def _add_polyline(
        self, model_space: Any, op: AddPolyline
    ) -> tuple[list[tuple[Any, str]], list[str]]:
        pline = self._lightweight_polyline(model_space, op.points, closed=op.closed)
        problems: list[str] = []
        if op.width > 0:
            try:
                pline.ConstantWidth = float(op.width)
            except self._com_error as exc:
                problems.append(f"largeur constante refusée: {self._short(exc)}")
        return [(pline, "polyline")], problems

    def _lightweight_polyline(
        self,
        model_space: Any,
        points: Sequence[tuple[float, float]],
        *,
        closed: bool,
    ) -> Any:
        """Crée une polyligne légère, une entité unique et non quatre segments.

        Le code historique dessinait un rectangle comme quatre ``AddLine``
        indépendants: rien n'était sélectionnable d'un clic, hachurable, ni
        mesurable en aire. ``AddLightWeightPolyline`` attend un tableau **plat**
        de coordonnées 2D, ``[x1, y1, x2, y2, ...]``, de longueur paire.
        """
        if len(points) < 2:
            raise InvalidGeometry(
                "Une polyligne exige au moins deux sommets",
                vertices=len(points),
            )
        if closed and len(points) < 3:
            raise InvalidGeometry(
                "Une polyligne fermée exige au moins trois sommets",
                vertices=len(points),
            )

        flat: list[float] = []
        for point in points:
            flat.append(float(point[0]))
            flat.append(float(point[1]))

        pline = model_space.AddLightWeightPolyline(self._doubles(flat))
        if closed:
            # .Closed relie le dernier sommet au premier. Répéter le premier
            # point en fin de liste produirait à la place un sommet doublon.
            pline.Closed = True
        return pline

    def _add_circle(
        self, model_space: Any, op: AddCircle
    ) -> tuple[list[tuple[Any, str]], list[str]]:
        circle = model_space.AddCircle(self._point(*op.center), float(op.radius))
        return [(circle, "circle")], []

    def _add_arc(self, model_space: Any, op: AddArc) -> tuple[list[tuple[Any, str]], list[str]]:
        # AddArc attend des angles en RADIANS et tourne dans le sens direct.
        # Documentation ActiveX d'Autodesk, méthode AddArc: « the start angle in
        # radians ». Aucune conversion n'est donc faite ici, contrairement à ce
        # qu'annonce l'en-tête de model/ops.py, qui vaut pour le DXF, lequel
        # stocke bien des degrés. Convertir ici décalerait tous les arcs d'un
        # facteur 180/pi. Voir W-21 de docs/windows-checklist.md.
        arc = model_space.AddArc(
            self._point(*op.center),
            float(op.radius),
            float(op.start_angle),
            float(op.end_angle),
        )
        return [(arc, "arc")], []

    def _add_text(self, model_space: Any, op: AddText) -> tuple[list[tuple[Any, str]], list[str]]:
        position = self._point(*op.position)
        text = model_space.AddText(str(op.text), position, float(op.height))
        problems: list[str] = []

        if op.rotation:
            try:
                # AcadText.Rotation est en radians, comme tous les angles ActiveX.
                text.Rotation = float(op.rotation)
            except self._com_error as exc:
                problems.append(f"rotation du texte refusée: {self._short(exc)}")

        alignment_name = _TEXT_ALIGNMENT.get((op.halign, op.valign))
        if alignment_name is None:
            problems.append(f"alignement {op.halign}/{op.valign} inconnu, texte laissé à gauche")
        elif alignment_name != "acAlignmentLeft":
            try:
                text.Alignment = self._const(alignment_name)
                # Obligatoire: hors alignement à gauche, AutoCAD positionne le
                # texte par TextAlignmentPoint et ignore InsertionPoint.
                text.TextAlignmentPoint = self._point(*op.position)
            except self._com_error as exc:
                problems.append(f"alignement du texte refusé: {self._short(exc)}")

        return [(text, "text")], problems

    def _add_mtext(self, model_space: Any, op: AddMText) -> tuple[list[tuple[Any, str]], list[str]]:
        # AddMText(InsertionPoint, Width, Text). Une largeur nulle désactive le
        # retour à la ligne automatique.
        mtext = model_space.AddMText(self._point(*op.position), float(op.width), str(op.text))
        problems: list[str] = []
        try:
            # HYPOTHÈSE: AcadMText.Height est la hauteur de caractère, et non
            # celle du cartouche, qui se lit dans la propriété en lecture seule.
            mtext.Height = float(op.height)
        except self._com_error as exc:
            problems.append(f"hauteur de texte multiligne refusée: {self._short(exc)}")
        if op.rotation:
            try:
                mtext.Rotation = float(op.rotation)
            except self._com_error as exc:
                problems.append(f"rotation du texte multiligne refusée: {self._short(exc)}")
        return [(mtext, "mtext")], problems

    def _add_hatch(self, model_space: Any, op: AddHatch) -> tuple[list[tuple[Any, str]], list[str]]:
        """Crée une hachure et les contours qui la délimitent.

        Convention alignée sur le backend ``ezdxf``, qui lui est vérifiable sur
        la machine de développement: **tous** les contours sont ajoutés comme
        boucles extérieures, et c'est la détection d'îlots qui décide de ce qui
        est un trou. Traiter le premier contour comme extérieur et les suivants
        comme des trous, ce qui serait tentant, ferait diverger les deux moteurs
        dès qu'un lot porte deux régions disjointes: AutoCAD percerait la
        seconde au lieu de la hachurer. ``HatchStyle`` est posé explicitement
        pour que la détection ne dépende pas des variables système du poste.

        Une hachure ActiveX n'a pas de géométrie propre: elle s'appuie sur des
        entités de contour réelles, créées ici sous forme de polylignes fermées.
        Elles sont conservées dans le dessin, et leurs handles sont rapportés
        comme créés: elles existent, il serait malhonnête de les taire.
        """
        hatch = model_space.AddHatch(
            self._const("acHatchPatternTypePreDefined"),
            str(op.pattern),
            False,  # non associative: les contours restent des entités libres
        )

        created: list[tuple[Any, str]] = [(hatch, "hatch")]
        problems: list[str] = []

        try:
            hatch.HatchStyle = self._const("acHatchStyleNormal")
        except self._com_error as exc:
            problems.append(f"style de hachure refusé: {self._short(exc)}")

        for loop in op.boundaries:
            boundary = self._lightweight_polyline(model_space, loop, closed=True)
            created.append((boundary, "polyline"))
            self._append_loop(hatch, boundary)

        try:
            hatch.PatternScale = float(op.scale)
        except self._com_error as exc:
            problems.append(f"échelle de motif refusée: {self._short(exc)}")
        try:
            # HYPOTHÈSE: radians, par cohérence avec le reste d'ActiveX. La
            # documentation Autodesk ne précise pas l'unité de cette propriété,
            # contrairement à AddArc et Rotation. Voir W-22.
            hatch.PatternAngle = float(op.angle)
        except self._com_error as exc:
            problems.append(f"angle de motif refusé: {self._short(exc)}")

        # Evaluate calcule le remplissage. Sans lui la hachure reste vide.
        hatch.Evaluate()
        return created, problems

    def _append_loop(self, hatch: Any, boundary: Any) -> None:
        """Ajoute un contour à une hachure, en essayant les deux formes connues.

        HYPOTHÈSE: ``AppendOuterLoop`` attend un SAFEARRAY d'IDispatch. Certaines
        combinaisons de version de pywin32 et d'AutoCAD acceptent directement une
        liste Python. On tente donc le VARIANT, puis la liste nue, et on retient
        la forme qui passe pour ne pas refaire l'essai à chaque contour.
        """
        append = hatch.AppendOuterLoop
        strategies: tuple[str, ...] = ("variant", "list")
        if self._hatch_loop_strategy is not None:
            strategies = (self._hatch_loop_strategy,)

        last: BaseException | None = None
        for strategy in strategies:
            payload = self._dispatches([boundary]) if strategy == "variant" else [boundary]
            try:
                append(payload)
            except self._com_error as exc:
                last = exc
                _LOG.debug("AppendLoop en mode %s refusé: %s", strategy, exc)
                continue
            self._hatch_loop_strategy = strategy
            return

        assert last is not None
        raise self._fail(last, "Contour de hachure refusé par AutoCAD")

    def _add_dim_aligned(
        self, model_space: Any, op: AddDimAligned
    ) -> tuple[list[tuple[Any, str]], list[str]]:
        dim = model_space.AddDimAligned(
            self._point(op.p1[0], op.p1[1]),
            self._point(op.p2[0], op.p2[1]),
            self._point(op.location[0], op.location[1]),
        )
        problems: list[str] = []
        if op.text_override is not None:
            try:
                dim.TextOverride = str(op.text_override)
            except self._com_error as exc:
                problems.append(f"texte de cote refusé: {self._short(exc)}")
        return [(dim, "dim_aligned")], problems

    def _add_block_ref(
        self, doc: Any, model_space: Any, op: AddBlockRef, context: _DocContext
    ) -> tuple[list[tuple[Any, str]], list[str]]:
        """Insère une occurrence de bloc, renseigne et redresse ses attributs.

        ``InsertBlock`` accepte aussi bien le nom d'un bloc déjà défini dans le
        dessin qu'un chemin de fichier DWG, qu'il importe alors.

        Les repères marqués ``keep_upright`` dans la définition sont redressés
        **après** la pose, une fois leur position calculée par AutoCAD: seule
        l'orientation change, jamais l'emplacement. Voir :meth:`_straighten` et
        :meth:`_upright_tags`.
        """
        try:
            reference = model_space.InsertBlock(
                self._point(*op.insert),
                str(op.name),
                float(op.scale[0]),
                float(op.scale[1]),
                float(op.scale[2]),
                float(op.rotation),
            )
        except self._com_error as exc:
            raise self._fail(
                exc,
                f"Insertion du bloc {op.name!r} refusée. Le bloc est-il défini "
                "dans le dessin, ou le chemin DWG est-il correct ?",
                block=op.name,
            ) from exc

        problems: list[str] = []
        placed = self._reference_attributes(reference)
        if isinstance(placed, str):
            # Les attributs de l'occurrence sont illisibles: on le dit, on ne
            # fait pas comme si les valeurs avaient été posées.
            if op.attributes:
                problems.append(placed)
        else:
            if op.attributes:
                problems.extend(self._fill_attributes(placed, op.attributes, op.name))
            problems.extend(self._straighten_marks(doc, placed, op.name, context))
        return [(reference, "block_ref")], problems

    def _reference_attributes(self, reference: Any) -> dict[str, Any] | str:
        """Attributs posés par l'occurrence, par étiquette en majuscules.

        Rend une chaîne décrivant l'empêchement plutôt qu'un dictionnaire vide
        quand ils sont illisibles: un dictionnaire vide se confondrait avec un
        bloc sans attribut, et l'appelant croirait avoir tout renseigné.
        """
        try:
            if not bool(reference.HasAttributes):
                return {}
        except self._com_error as exc:
            return f"attributs illisibles: {self._short(exc)}"

        try:
            existing = reference.GetAttributes()
        except self._com_error as exc:
            return f"GetAttributes a échoué: {self._short(exc)}"

        by_tag: dict[str, Any] = {}
        for attribute in existing:
            try:
                by_tag[str(attribute.TagString).upper()] = attribute
            except self._com_error as exc:
                _LOG.debug("Étiquette d'attribut illisible: %s", exc)
        return by_tag

    def _straighten_marks(
        self, doc: Any, placed: dict[str, Any], block_name: str, context: _DocContext
    ) -> list[str]:
        """Redresse les attributs que la définition veut maintenus horizontaux.

        Le format fait suivre à un attribut la rotation de l'occurrence qui le
        porte: un symbole posé sur un mur vertical afficherait son repère à la
        verticale, un symbole retourné l'afficherait à l'envers. Un repère est
        une annotation, sa vocation est d'être lu.

        Même mécanique que le backend ``ezdxf``, qui lui est vérifiable sur la
        machine de développement: la consigne est lue dans les données étendues
        de la **définition**, jamais devinée. Un bloc venu d'ailleurs n'en porte
        pas et garde le comportement du format.
        """
        if not placed:
            return []
        tags = self._upright_tags(doc, block_name, context)
        if not tags:
            return []

        problems: list[str] = []
        for tag in sorted(tags):
            target = placed.get(tag)
            if target is None:
                continue
            problems.extend(self._straighten(target, tag))
        return problems

    def _straighten(self, attribute: Any, tag: str) -> list[str]:
        """Remet un attribut déjà posé à l'horizontale, sans le déplacer.

        Le texte s'ancre sur un point et tourne autour de lui: remettre la
        rotation à zéro le redresse sur place. Les deux indicateurs de
        génération de texte sont remis à plat dans la foulée, car une échelle
        d'insertion négative retourne le texte par eux plutôt que par sa
        rotation.

        HYPOTHÈSE, voir W-71: ``AcadAttributeReference`` expose ``Rotation``,
        ``UpsideDown`` et ``Backward``, et affecter ``Rotation`` ne déplace pas
        le point d'ancrage. La direction d'extrusion n'est **pas** touchée: en
        ActiveX les points sont documentés en coordonnées du dessin, alors que
        le DXF brut les exprime dans le repère objet, ce qui oblige le backend
        ``ezdxf`` à une gymnastique que rien ici ne justifie a priori.
        """
        problems: list[str] = []
        try:
            attribute.Rotation = 0.0
        except self._com_error as exc:
            problems.append(f"redressement du repère {tag!r} refusé: {self._short(exc)}")
            # Sans la rotation, les deux indicateurs ne servent à rien.
            return problems
        for flag in ("UpsideDown", "Backward"):
            try:
                setattr(attribute, flag, False)
            except self._com_error as exc:
                _LOG.debug("%s non géré pour l'attribut %s: %s", flag, tag, exc)
        return problems

    def _upright_tags(
        self, doc: Any, block_name: str, context: _DocContext
    ) -> frozenset[str]:
        """Étiquettes marquées ``keep_upright`` dans la définition d'un bloc.

        **Deux sources, réunies.** La première est la donnée étendue portée par
        la définition d'attribut, seule qui survive à l'enregistrement du
        fichier et qui vaille donc pour un bloc défini lors d'une session
        précédente, ou par le backend ``ezdxf``. La seconde est ce que
        :meth:`_define_block` a lui-même déclaré pendant cette session.

        La seconde existe parce que la première repose sur une hypothèse non
        vérifiable ici, W-73: si ``GetXData`` ne rend pas ce qu'on croit en
        liaison tardive, les blocs définis dans la session gardent malgré tout
        des repères lisibles. Le repli couvre le cas courant — définir puis
        insérer dans le même lot — sans rien promettre pour le reste.

        Le résultat est mémorisé pour la durée du lot: une planche insère la
        même vingtaine de blocs, et relire le contenu d'une définition coûte un
        appel par objet qu'elle contient.

        HYPOTHÈSE, voir W-72: le contenu d'un ``AcadBlock`` se parcourt par
        ``Count`` et ``Item(i)``, et une définition d'attribut s'y reconnaît à
        son ``ObjectName`` ``AcDbAttributeDefinition``.
        """
        key = block_name.strip().upper()
        cached = context.upright_tags.get(key)
        if cached is not None:
            return cached

        tags: set[str] = set(self._upright_by_block.get(key, ()))
        try:
            block = doc.Blocks.Item(str(block_name))
            for index in range(int(block.Count)):
                member = block.Item(index)
                if str(member.ObjectName) != _ATTDEF_OBJECT_NAME:
                    continue
                if self._wants_upright(member):
                    tags.add(str(member.TagString).upper())
        except self._com_error as exc:
            # Un bloc importé d'un DWG, ou dont le contenu est inaccessible:
            # aucune consigne lisible au-delà de ce que la session a déclaré.
            _LOG.debug("Consignes de redressement illisibles pour %s: %s", block_name, exc)

        frozen = frozenset(tags)
        context.upright_tags[key] = frozen
        return frozen

    def _remember_upright(self, block_name: str, tags: Sequence[str]) -> None:
        """Retient les repères à redresser d'un bloc défini par cette session.

        Filet de sécurité du mécanisme de données étendues, voir
        :meth:`_upright_tags`. Vidé au changement de document: les noms de blocs
        n'ont de sens que dans le dessin qui les porte.
        """
        if not tags:
            return
        key = block_name.strip().upper()
        self._upright_by_block[key] = frozenset(tag.upper() for tag in tags)

    def _wants_upright(self, attdef: Any) -> bool:
        """Vrai si la définition d'attribut porte le marqueur du projet.

        HYPOTHÈSE, voir W-73: ``GetXData(nom_application)`` rend en liaison
        tardive pywin32 un couple ``(codes, valeurs)``, les paramètres de sortie
        de la signature ActiveX ``GetXData(AppName, XDataType, XDataValue)``.
        Une application inconnue de l'entité rend deux tableaux vides, ou bien
        lève: les deux cas signifient « pas de consigne » et sont traités comme
        tels.
        """
        try:
            xdata = attdef.GetXData(_XDATA_APPID)
        except (self._com_error, TypeError, ValueError) as exc:
            _LOG.debug("GetXData indisponible sur une définition d'attribut: %s", exc)
            return False
        if not xdata or len(xdata) < 2:
            return False
        codes, values = xdata[0], xdata[1]
        if codes is None or values is None:
            return False
        return any(
            int(code) == _XDATA_STRING and str(value) == _XDATA_KEEP_UPRIGHT
            for code, value in zip(codes, values, strict=False)
        )

    def _mark_upright(self, doc: Any, attdef: Any, tag: str) -> list[str]:
        """Inscrit la consigne de redressement dans la définition d'attribut.

        Marquée sur la **définition**, pas sur l'occurrence: c'est le bloc qui
        sait que son repère est une annotation, et toute insertion ultérieure
        doit l'apprendre de lui, y compris après enregistrement et relecture du
        fichier. Le format n'a pas de champ pour cela, d'où la donnée étendue.

        HYPOTHÈSE, voir W-73: ``Document.RegisterApplication(nom)`` déclare
        l'application, et ``SetXData(codes, valeurs)`` attend deux tableaux de
        même longueur dont le premier couple est ``(1001, nom_application)``.
        """
        if not self._xdata_registered:
            try:
                doc.RegisterApplication(_XDATA_APPID)
            except self._com_error as exc:
                # Non bloquant ici: l'application est peut-être déjà déclarée,
                # auquel cas SetXData passera quand même.
                _LOG.debug("RegisterApplication(%s) refusé: %s", _XDATA_APPID, exc)
            else:
                self._xdata_registered = True

        try:
            attdef.SetXData(
                self._shorts([_XDATA_APPNAME, _XDATA_STRING]),
                self._variants([_XDATA_APPID, _XDATA_KEEP_UPRIGHT]),
            )
        except self._com_error as exc:
            # Le bloc reste valide, son repère suivra simplement la rotation de
            # l'occurrence. C'est une dégradation visible, donc rapportée.
            return [
                f"consigne d'horizontalité du repère {tag!r} non inscrite: {self._short(exc)}"
            ]
        return []

    def _fill_attributes(
        self, placed: dict[str, Any], attributes: Sequence[tuple[str, str]], block_name: str
    ) -> list[str]:
        """Renseigne les valeurs d'attributs d'une occurrence déjà posée."""
        if not placed:
            return [
                f"le bloc {block_name!r} ne porte aucun attribut, "
                f"{len(attributes)} valeur(s) ignorée(s)"
            ]

        problems: list[str] = []
        for tag, value in attributes:
            target = placed.get(str(tag).upper())
            if target is None:
                problems.append(f"attribut {tag!r} absent du bloc")
                continue
            try:
                target.TextString = str(value)
            except self._com_error as exc:
                problems.append(f"attribut {tag!r} non renseigné: {self._short(exc)}")
        return problems

    # -- définition de bloc -------------------------------------------------

    def _define_block(
        self, doc: Any, operation: DefineBlock, context: _DocContext
    ) -> list[str]:
        """Écrit une définition dans la table des blocs. Ne dessine rien.

        Rend la liste des attributs graphiques refusés, jamais de handle: une
        définition n'est pas une entité de l'espace objet. Le nom part dans
        ``BatchResult.blocks``, comme un calque part dans ``layers``.

        **Garantie de présence, pas écrasement**, décision alignée sur le
        backend ``ezdxf``, qui lui est vérifiable: un nom déjà pris est conservé
        tel quel et le lot réussit. Deux raisons: un plan qui insère dix fois la
        même porte redéfinit dix fois le bloc, et redéfinir un bloc change
        silencieusement **toutes** les occurrences déjà posées, y compris celles
        d'un autre lot et celles dessinées à la main par l'utilisateur.

        **Rien de partiel.** Si une opération du contenu échoue, le bloc entamé
        est retiré avant que l'erreur ne remonte. Un bloc à moitié écrit
        s'insérerait sans erreur et produirait un symbole tronqué, ce qui ne se
        voit pas sur un plan.

        HYPOTHÈSE centrale, non vérifiable ici, voir W-66 de
        ``docs/windows-checklist.md``: ``doc.Blocks.Add(point_base, nom)`` rend
        un ``AcadBlock`` qui expose les mêmes méthodes de dessin que le
        ModelSpace — ``AddLine``, ``AddLightWeightPolyline``, ``AddArc``,
        ``InsertBlock`` — parce que ``AcadModelSpace`` dérive de ``AcadBlock``
        dans la hiérarchie ActiveX. Le contenu réemprunte donc exactement
        l'aiguillage de :meth:`_create`, avec le bloc pour destination.
        """
        name = operation.name.strip()
        if not name:
            raise InvalidParameter("Nom de bloc vide", valid="chaîne non vide")

        if self._block_exists(doc, context, name):
            context.note_block(name)
            return []

        # Le calque de l'enregistrement de bloc doit exister avant lui. Par
        # convention c'est le calque ``0``, toujours présent, mais le modèle
        # autorise d'en nommer un autre.
        self._ensure_layer(doc, context, operation.style.layer)

        try:
            block = doc.Blocks.Add(self._point(*operation.base_point), str(name))
        except self._com_error as exc:
            raise self._fail(
                exc,
                f"Création de la définition de bloc {name!r} refusée",
                block=name,
            ) from exc

        problems: list[str] = []
        try:
            if operation.description:
                try:
                    # HYPOTHÈSE, voir W-69: ``AcadBlock.Comments`` est bien la
                    # description du bloc, celle qu'affiche le gestionnaire de
                    # blocs, et l'équivalent du code de groupe 4 du DXF.
                    # Purement documentaire: son refus n'invalide pas le bloc.
                    block.Comments = str(operation.description)
                except self._com_error as exc:
                    problems.append(f"description de bloc refusée: {self._short(exc)}")

            for index, child in enumerate(operation.operations):
                if isinstance(child, EnsureLayer | DefineBlock):
                    # Déjà refusé par ``validate``; répété ici parce qu'une
                    # définition imbriquée écrirait dans la table du document
                    # au lieu du bloc, sans que rien ne le signale.
                    raise InvalidParameter(
                        "Une définition de bloc ne peut contenir ni calque ni autre bloc",
                        block=name,
                        at=index,
                        found=child.kind,
                    )
                # Même chemin que l'espace objet: la géométrie d'un bloc s'écrit
                # comme celle du dessin, seule la destination change. Les
                # handles ainsi obtenus ne sont pas rapportés: ils désignent des
                # entités de la définition, que l'appelant ne peut ni effacer ni
                # annuler indépendamment.
                self._ensure_layer(doc, context, child.style.layer)
                entities, refused = self._create(doc, block, child, context)
                problems.extend(refused)
                for entity, _kind in entities:
                    problems.extend(self._apply_style(doc, entity, child.style, context))

            for attribute in operation.attributes:
                problems.extend(self._add_attdef(doc, block, attribute, name, context))
            # Retenu pour la session, en plus du marquage inscrit dans le
            # dessin: si la relecture des données étendues se révélait
            # impossible sous Windows, les repères des blocs définis ici
            # resteraient malgré tout redressés à l'insertion.
            self._remember_upright(
                name, [a.tag.strip() for a in operation.attributes if a.keep_upright]
            )
        except Exception as exc:
            leftover = self._discard_block(doc, name)
            if leftover is None:
                raise
            # Le bloc entamé n'a pas pu être retiré. Le taire laisserait dans le
            # dessin un symbole tronqué qui s'insère sans erreur.
            raise OperationFailed(
                f"Définition du bloc {name!r} interrompue, et le bloc entamé n'a "
                "pas pu être retiré: il reste dans le dessin, incomplet.",
                block=name,
                cause=str(exc),
                cleanup=leftover,
            ) from exc

        context.blocks[name.upper()] = True
        context.note_block(name)
        return problems

    def _add_attdef(
        self,
        doc: Any,
        block: Any,
        attribute: AttributeDef,
        block_name: str,
        context: _DocContext,
    ) -> list[str]:
        """Ajoute une définition d'attribut au bloc en cours d'écriture.

        L'attribut est ce qui rend une nomenclature possible: sans lui, un bloc
        inséré cent fois ne dit rien d'autre que sa présence.

        HYPOTHÈSE, voir W-67: la signature ActiveX est
        ``AddAttribute(Height, Mode, Prompt, InsertionPoint, Tag, Value)``,
        dans cet ordre, ``Mode`` étant un champ de bits ``AcAttributeMode``.
        L'objet rendu est un ``AcadAttribute``, qui porte les mêmes propriétés
        de texte qu'un ``AcadText``: ``Alignment``, ``TextAlignmentPoint``,
        ``Rotation``.

        ``keep_upright`` n'a pas de champ dans le format: l'intention est
        inscrite en donnée étendue sur la définition, et relue à chaque
        insertion par :meth:`_upright_tags`. Même convention que le backend
        ``ezdxf``, mêmes nom d'application et marqueur, afin qu'un dessin passe
        d'un moteur à l'autre sans perdre la consigne.
        """
        tag = attribute.tag.strip()
        if not tag:
            raise InvalidParameter("Étiquette d'attribut vide", block=block_name)
        if any(c.isspace() for c in tag):
            raise InvalidParameter(
                f"Étiquette d'attribut avec espace: {attribute.tag!r}",
                block=block_name,
                valid="un seul mot, sans espace",
            )
        height = float(attribute.height)
        if height <= 0.0:
            raise InvalidGeometry(
                "Hauteur d'attribut nulle ou négative",
                block=block_name,
                tag=tag,
                height=height,
            )

        self._ensure_layer(doc, context, attribute.style.layer)
        mode = self._const(
            "acAttributeModeInvisible" if attribute.invisible else "acAttributeModeNormal"
        )
        try:
            attdef = block.AddAttribute(
                height,
                mode,
                # Sans invite, AutoCAD n'a rien à afficher quand il demande la
                # valeur à l'insertion: l'étiquette fait alors l'affaire.
                str(attribute.prompt or tag),
                self._point(attribute.position[0], attribute.position[1]),
                str(tag),
                str(attribute.default),
            )
        except self._com_error as exc:
            raise self._fail(
                exc,
                f"Définition de l'attribut {tag!r} refusée",
                block=block_name,
                tag=tag,
            ) from exc

        problems: list[str] = []
        if attribute.rotation:
            try:
                # Radians, comme toute grandeur angulaire d'ActiveX. Voir W-21.
                attdef.Rotation = float(attribute.rotation)
            except self._com_error as exc:
                problems.append(f"rotation de l'attribut {tag!r} refusée: {self._short(exc)}")

        alignment_name = _TEXT_ALIGNMENT.get((attribute.halign, attribute.valign))
        if alignment_name is None:
            problems.append(
                f"alignement {attribute.halign}/{attribute.valign} inconnu, "
                f"attribut {tag!r} laissé à gauche"
            )
        elif alignment_name != "acAlignmentLeft":
            try:
                attdef.Alignment = self._const(alignment_name)
                # Même piège que pour AddText, voir W-27: hors alignement à
                # gauche, AutoCAD ignore InsertionPoint et se cale sur
                # TextAlignmentPoint.
                attdef.TextAlignmentPoint = self._point(
                    attribute.position[0], attribute.position[1]
                )
            except self._com_error as exc:
                problems.append(f"alignement de l'attribut {tag!r} refusé: {self._short(exc)}")

        if attribute.keep_upright:
            problems.extend(self._mark_upright(doc, attdef, tag))

        problems.extend(self._apply_style(doc, attdef, attribute.style, context))
        return problems

    def _block_exists(self, doc: Any, context: _DocContext, name: str) -> bool:
        """Dit si la table des blocs contient déjà ce nom, en un seul appel.

        HYPOTHÈSE, voir W-68: ``Blocks.Item(nom)`` lève une ``com_error`` pour
        un nom inconnu, au lieu de rendre ``None``. C'est la même forme que
        ``SelectionSets.Item``, déjà utilisée par ce fichier. Si la méthode
        rendait autre chose, la conséquence serait bénigne: ``Blocks.Add``
        refuserait ensuite un nom déjà pris, et l'échec serait rapporté.
        """
        key = name.upper()
        cached = context.blocks.get(key)
        if cached is not None:
            return cached
        try:
            doc.Blocks.Item(str(name))
        except self._com_error as exc:
            _LOG.debug("Bloc %s absent de la table des blocs: %s", name, exc)
            context.blocks[key] = False
            return False
        context.blocks[key] = True
        return True

    def _discard_block(self, doc: Any, name: str) -> str | None:
        """Retire une définition entamée. Rend ``None`` si le retrait a réussi.

        Une chaîne décrivant l'échec est rendue sinon, afin que l'appelant le
        dise au lieu de laisser un bloc incomplet dans le dessin.

        HYPOTHÈSE, voir W-69: ``Blocks.Item(nom).Delete()`` retire une
        définition de la table des blocs. Le bloc vient d'être créé par ce lot,
        donc aucune occurrence ne le référence: AutoCAD refuse de supprimer une
        définition encore utilisée, et ce cas ne devrait pas se présenter ici.
        """
        try:
            doc.Blocks.Item(str(name)).Delete()
        except self._com_error as exc:
            message = self._short(exc)
            _LOG.warning("Retrait du bloc incomplet %s impossible: %s", name, message)
            return message
        return None

    # -- style -------------------------------------------------------------

    def _apply_style(
        self, doc: Any, entity: Any, style: Style, context: _DocContext
    ) -> list[str]:
        """Applique le style et rend la liste des attributs refusés.

        Les attributs sont posés explicitement, y compris la couleur ByLayer.
        Une entité neuve hérite sinon des variables système courantes (CECOLOR,
        CELTYPE), qui dépendent de ce que l'utilisateur a fait avant nous: le
        résultat ne serait pas reproductible.
        """
        problems: list[str] = []

        try:
            entity.Layer = str(style.layer)
        except self._com_error as exc:
            problems.append(f"calque {style.layer!r} refusé: {self._short(exc)}")

        try:
            entity.Color = int(style.color)
        except self._com_error as exc:
            problems.append(f"couleur {style.color} refusée: {self._short(exc)}")

        if style.linetype:
            problems.extend(self._apply_linetype(doc, entity, style.linetype, context))

        if style.lineweight is not None:
            try:
                # AcLineWeight est une énumération: centièmes de millimètre pris
                # dans une liste fermée, plus -1 ByLayer, -2 ByBlock, -3 Default.
                entity.Lineweight = int(style.lineweight)
            except self._com_error as exc:
                problems.append(
                    f"épaisseur de trait {style.lineweight} refusée "
                    f"(valeur hors énumération AcLineWeight ?): {self._short(exc)}"
                )

        return problems

    def _apply_linetype(
        self, doc: Any, entity: Any, linetype: str, context: _DocContext
    ) -> list[str]:
        if context.linetypes is None:
            context.linetypes = self._linetype_names(doc)

        if linetype.upper() not in context.linetypes:
            loaded = False
            # acad.lin en unités impériales, acadiso.lin en métrique. Le fichier
            # utile dépend du gabarit, on tente les deux.
            for library in ("acad.lin", "acadiso.lin"):
                try:
                    doc.Linetypes.Load(str(linetype), library)
                except self._com_error as exc:
                    _LOG.debug("Chargement de %s depuis %s refusé: %s", linetype, library, exc)
                    continue
                loaded = True
                break
            if not loaded:
                return [
                    f"type de ligne {linetype!r} introuvable dans acad.lin ni acadiso.lin"
                ]
            context.linetypes.add(linetype.upper())

        try:
            entity.Linetype = str(linetype)
        except self._com_error as exc:
            return [f"type de ligne {linetype!r} refusé: {self._short(exc)}"]
        return []

    # -- calques -----------------------------------------------------------

    def _layer_names(self, doc: Any) -> set[str]:
        """Noms de calques en majuscules. AutoCAD ignore la casse des calques."""
        try:
            layers = doc.Layers
            return {str(layers.Item(i).Name).upper() for i in range(int(layers.Count))}
        except self._com_error as exc:
            raise self._fail(exc, "Lecture de la table des calques impossible") from exc

    def _linetype_names(self, doc: Any) -> set[str]:
        try:
            linetypes = doc.Linetypes
            return {str(linetypes.Item(i).Name).upper() for i in range(int(linetypes.Count))}
        except self._com_error as exc:
            _LOG.debug("Lecture de la table des types de ligne impossible: %s", exc)
            return set()

    def _ensure_layer(
        self,
        doc: Any,
        context: _DocContext,
        name: str,
        *,
        color: int | str = 7,
        description: str = "",
        linetype: str | None = None,
    ) -> None:
        """Crée le calque s'il manque. N'écrase jamais un calque existant.

        Un calque déjà présent porte peut-être des réglages voulus par
        l'utilisateur. Les réécrire à chaque lot détruirait son travail.
        """
        if not name:
            raise InvalidParameter("Nom de calque vide")
        key = name.upper()
        if key in context.layers:
            return

        try:
            layer = doc.Layers.Add(str(name))
        except self._com_error as exc:
            raise self._fail(exc, f"Création du calque {name!r} impossible", layer=name) from exc

        context.layers.add(key)
        # Créé à la volée pour accueillir une entité: à signaler, sans quoi le
        # modèle ne saurait pas qu'un calque est apparu dans le document.
        context.note_layer(name)

        try:
            layer.Color = color_index(color)
        except self._com_error as exc:
            _LOG.warning("Couleur du calque %s refusée: %s", name, exc)

        if linetype:
            for library in ("acad.lin", "acadiso.lin"):
                try:
                    doc.Linetypes.Load(str(linetype), library)
                except self._com_error:
                    continue
                break
            try:
                layer.Linetype = str(linetype)
            except self._com_error as exc:
                _LOG.warning("Type de ligne du calque %s refusé: %s", name, exc)

        if description:
            try:
                # Description n'existe pas sur les versions anciennes d'AutoCAD.
                # Purement documentaire: son absence n'invalide pas le calque.
                layer.Description = str(description)
            except self._com_error as exc:
                _LOG.debug("Description de calque non gérée par cette version: %s", exc)

    # -- suppression -------------------------------------------------------

    def delete(self, selector: EntityFilter) -> list[str]:
        """Supprime les entités du filtre et rend les handles réellement supprimés.

        Un filtre vide désignerait tout le dessin. Le backend refuse: effacer un
        dessin entier est une décision de l'utilisateur, pas un effet de bord.
        """
        if selector is None or selector.is_empty:
            raise ConfirmationRequired(
                "Refus de supprimer sans filtre: un filtre vide viserait toutes "
                "les entités du dessin. Précisez un calque, un type, une couleur, "
                "une fenêtre ou des handles.",
                backend=self.name,
            )
        self._require_connection()
        return self._submit(lambda: self._delete_on_worker(selector))

    def _delete_on_worker(self, selector: EntityFilter) -> list[str]:
        doc = self._document()
        entities = self._resolve(doc, selector)
        if not entities:
            return []

        # Les handles sont lus avant suppression: après Delete, l'objet n'est
        # plus interrogeable.
        targets: list[tuple[str, Any]] = []
        for entity in entities:
            try:
                targets.append((str(entity.Handle), entity))
            except self._com_error as exc:
                _LOG.debug("Handle illisible avant suppression: %s", exc)

        deleted: list[str] = []
        failed: list[dict[str, Any]] = []

        try:
            doc.StartUndoMark()
        except self._com_error as exc:
            raise self._fail(exc, "StartUndoMark a échoué avant suppression") from exc

        try:
            for handle, entity in targets:
                try:
                    entity.Delete()
                except self._com_error as exc:
                    failed.append({"handle": handle, **self._com_details(exc)})
                    continue
                deleted.append(handle)
        finally:
            try:
                doc.EndUndoMark()
            except self._com_error as exc:
                _LOG.warning("EndUndoMark a échoué après suppression: %s", exc)

        if deleted:
            self._regen(doc)

        if failed:
            raise OperationFailed(
                f"{len(failed)} entité(s) n'ont pas pu être supprimées "
                f"(calque verrouillé ou entité sur un espace protégé ?)",
                deleted=deleted,
                failed=failed,
            )
        return deleted

    def set_color(self, handle: str, color: int) -> None:
        """Change la couleur d'une entité, retrouvée en temps constant."""
        index = color_index(color)
        self._require_connection()
        self._submit(lambda: self._set_color_on_worker(handle, index))

    def _set_color_on_worker(self, handle: str, index: int) -> None:
        doc = self._document()
        entity = self._by_handle(doc, handle)
        try:
            doc.StartUndoMark()
        except self._com_error as exc:
            raise self._fail(exc, "StartUndoMark a échoué avant changement de couleur") from exc
        try:
            entity.Color = int(index)
        except self._com_error as exc:
            raise self._fail(
                exc, f"Couleur {index} refusée pour l'entité {handle}", handle=handle
            ) from exc
        finally:
            try:
                doc.EndUndoMark()
            except self._com_error as exc:
                _LOG.warning("EndUndoMark a échoué après changement de couleur: %s", exc)
        self._regen(doc)

    def undo(self) -> None:
        """Annule le dernier lot exécuté par ce backend.

        Pourquoi pas ``SendCommand("._UNDO _1 ")``, comme le code historique:
        ``SendCommand`` est traité de façon asynchrone par AutoCAD, sans aucune
        garantie de moment ni de succès, et il exécute du texte de commande
        arbitraire. Il rendait donc « annulation envoyée » sans savoir si quoi
        que ce soit avait été annulé.

        L'API ActiveX n'expose pas de méthode ``Undo`` sur le document: seules
        ``StartUndoMark`` et ``EndUndoMark`` existent. Le backend annule donc de
        façon déterministe, en supprimant par handle les entités créées par le
        dernier lot, dans l'ordre inverse, le tout dans sa propre marque
        d'annulation. Ce qui a réellement disparu est vérifié, pas supposé.

        Portée: les créations. Un ``set_color`` n'est pas défait par cette
        méthode. Les marques d'annulation posées par :meth:`execute` restent en
        place, si bien qu'un Ctrl+Z dans AutoCAD reste possible côté utilisateur.
        """
        self._require_connection()
        self._submit(self._undo_on_worker)

    def _undo_on_worker(self) -> None:
        if not self._undo_stack:
            raise OperationFailed(
                "Aucun lot à annuler: la pile d'annulation du backend est vide.",
                backend=self.name,
            )
        record = self._undo_stack.pop()
        doc = self._document()

        removed: list[str] = []
        missing: list[str] = []
        failed: list[dict[str, Any]] = []

        try:
            doc.StartUndoMark()
        except self._com_error as exc:
            self._undo_stack.append(record)
            raise self._fail(exc, "StartUndoMark a échoué avant annulation") from exc

        try:
            for handle in reversed(record.handles):
                try:
                    entity = doc.HandleToObject(handle)
                except self._com_error:
                    # Déjà supprimée, par l'utilisateur ou par un undo AutoCAD.
                    missing.append(handle)
                    continue
                try:
                    entity.Delete()
                except self._com_error as exc:
                    failed.append({"handle": handle, **self._com_details(exc)})
                    continue
                removed.append(handle)
        finally:
            try:
                doc.EndUndoMark()
            except self._com_error as exc:
                _LOG.warning("EndUndoMark a échoué après annulation: %s", exc)

        if removed:
            self._regen(doc)

        _LOG.info(
            "Annulation du lot %r: %d supprimée(s), %d déjà absente(s)",
            record.label,
            len(removed),
            len(missing),
        )
        if failed:
            raise OperationFailed(
                f"Annulation partielle du lot {record.label!r}: "
                f"{len(failed)} entité(s) n'ont pas pu être supprimées.",
                label=record.label,
                removed=removed,
                missing=missing,
                failed=failed,
            )

    def _push_undo(self, label: str, handles: Sequence[str]) -> None:
        if self._undo_depth == 0 or not handles:
            return
        self._undo_stack.append(_BatchRecord(label=label, handles=tuple(handles)))
        while len(self._undo_stack) > self._undo_depth:
            self._undo_stack.pop(0)

    # -- lecture -----------------------------------------------------------

    def document_info(self) -> dict[str, Any]:
        """Métadonnées du document courant."""
        self._require_connection()
        return self._submit_read(self._document_info_on_worker)

    def _document_info_on_worker(self) -> dict[str, Any]:
        doc = self._document()
        insunits = int(doc.GetVariable("INSUNITS"))
        unit = _UNIT_BY_INSUNITS.get(insunits, self._default_unit)

        layers: list[dict[str, Any]] = []
        table = doc.Layers
        for index in range(int(table.Count)):
            layer = table.Item(index)
            entry: dict[str, Any] = {
                "name": str(layer.Name),
                "color": int(layer.Color),
                "frozen": bool(layer.Freeze),
                "locked": bool(layer.Lock),
            }
            try:
                entry["linetype"] = str(layer.Linetype)
            except self._com_error as exc:
                _LOG.debug("Type de ligne du calque %s illisible: %s", entry["name"], exc)
            layers.append(entry)

        return {
            "backend": self.name,
            "name": self._safe_str(doc, "Name"),
            "path": self._safe_str(doc, "Path"),
            "full_name": self._safe_str(doc, "FullName"),
            "saved": bool(doc.Saved),
            "unit": unit.value,
            "insunits": insunits,
            "entity_count": int(doc.ModelSpace.Count),
            "active_layer": self._safe_str(doc.ActiveLayer, "Name"),
            "layer_count": len(layers),
            "layers": layers,
            "autocad_version": self._safe_str(self._app, "Version"),
        }

    def query(
        self, selector: EntityFilter | None = None, *, limit: int | None = None
    ) -> list[EntityInfo]:
        """Liste les entités du filtre, au plus ``limit``.

        Sans filtre, la lecture parcourt le ModelSpace, bornée par ``limit``.
        Avec un filtre exploitable en codes DXF, c'est AutoCAD qui trie: un seul
        aller-retour pour la sélection au lieu d'un par entité du dessin.
        """
        if limit is not None and limit <= 0:
            raise InvalidParameter("limit doit être strictement positif", limit=limit)
        self._require_connection()
        return self._submit_read(lambda: self._query_on_worker(selector, limit))

    def _query_on_worker(
        self, selector: EntityFilter | None, limit: int | None
    ) -> list[EntityInfo]:
        doc = self._document()
        if selector is None or selector.is_empty:
            model_space = doc.ModelSpace
            total = int(model_space.Count)
            count = total if limit is None else min(total, limit)
            entities = [model_space.Item(i) for i in range(count)]
        else:
            entities = self._resolve(doc, selector)
            if limit is not None:
                entities = entities[:limit]
        return [self._describe(entity) for entity in entities]

    def count(self, selector: EntityFilter | None = None) -> int:
        """Compte les entités sans lire leurs propriétés."""
        self._require_connection()
        return self._submit_read(lambda: self._count_on_worker(selector))

    def _count_on_worker(self, selector: EntityFilter | None) -> int:
        doc = self._document()
        if selector is None or selector.is_empty:
            # Un seul appel, quelle que soit la taille du dessin.
            return int(doc.ModelSpace.Count)
        if selector.handles is not None:
            return len(self._resolve(doc, selector))
        selection = self._select(doc, selector)
        try:
            return int(selection.Count)
        finally:
            self._discard_selection_set(selection)

    def _describe(self, entity: Any) -> EntityInfo:
        object_name = str(entity.ObjectName)
        kind = _OBJECT_NAME_TO_KIND.get(object_name, object_name)
        extra: dict[str, Any] = {"object_name": object_name}
        extra.update(self._measures(entity, object_name))

        bbox: tuple[float, float, float, float] | None = None
        try:
            # En pywin32 les paramètres [out] de GetBoundingBox sont rendus
            # comme un couple de tuples de trois flottants.
            min_point, max_point = entity.GetBoundingBox()
            bbox = (
                float(min_point[0]),
                float(min_point[1]),
                float(max_point[0]),
                float(max_point[1]),
            )
        except (self._com_error, TypeError, ValueError, IndexError) as exc:
            _LOG.debug("Boîte englobante indisponible pour %s: %s", object_name, exc)

        return EntityInfo(
            handle=str(entity.Handle),
            kind=kind,
            layer=str(entity.Layer),
            color=int(entity.Color),
            bbox=bbox,
            extra=extra,
        )

    def _measures(self, entity: Any, object_name: str) -> dict[str, Any]:
        """Mesures publiables d'une entité, dans l'unité du document.

        Alimente les deux clés de ``base.MEASURE_KEYS``, ``length`` et ``area``,
        que ``ops.query`` additionne pour bâtir une nomenclature. **Rien n'est
        publié qu'AutoCAD n'ait calculé lui-même**, et rien n'est estimé: une
        mesure absente est comptée comme manquante par ``ops.query``, ce qui est
        honnête, tandis qu'une mesure fausse se lit sans se relire.

        Ce que chaque type rend, et pourquoi:

        * **LINE** — ``Length``.
        * **LWPOLYLINE** — ``Length``, la longueur développée, renflements
          compris. ``Area`` **uniquement si la polyligne est fermée**: AutoCAD
          rend pour une polyligne ouverte l'aire du contour qu'on obtiendrait en
          la refermant, ce qui serait une mesure de quelque chose qui n'est pas
          dessiné. La publier gonflerait tout total de surfaces.
        * **CIRCLE** — ``Circumference`` en longueur développée, et ``Area``.
        * **ARC** — ``ArcLength`` seulement. Un arc est une ligne courbe, il n'a
          pas d'aire ; AutoCAD sait pourtant rendre celle du segment circulaire
          délimité par la corde, qui ne correspond à aucune surface du dessin.
        * **tout le reste** — rien. Une hachure, un texte, une occurrence de
          bloc ou une cote ne reçoivent aucune clé.

        Coût: une à deux lectures de propriété par entité décrite, donc un à
        deux allers-retours COM. ``query`` étant borné par ``limit``, la
        dépense reste bornée elle aussi.

        HYPOTHÈSE, voir W-70: les noms de propriétés supposés existants sont
        ``AcadLine.Length``, ``AcadLWPolyline.Length`` et ``.Area``,
        ``AcadCircle.Circumference`` et ``.Area``, ``AcadArc.ArcLength``. Une
        propriété absente d'une version d'AutoCAD lève une erreur, qui est
        interceptée: la clé est alors simplement omise.
        """
        measures: dict[str, Any] = {}

        length_property = _LENGTH_PROPERTY.get(object_name)
        if length_property is not None:
            length = self._number_property(entity, length_property)
            if length is not None:
                measures["length"] = length

        if object_name == "AcDbCircle":
            area = self._number_property(entity, "Area")
            if area is not None:
                measures["area"] = area
        elif object_name in ("AcDbPolyline", "AcDb2dPolyline"):
            closed = self._flag_property(entity, "Closed")
            if closed is not None:
                # Publié parce qu'il est déjà lu, et parce que le backend
                # ``ezdxf`` le publie: une polyligne se reconnaît d'abord à
                # cela.
                measures["closed"] = closed
            if closed:
                area = self._number_property(entity, "Area")
                if area is not None:
                    measures["area"] = area

        return measures

    def _number_property(self, entity: Any, name: str) -> float | None:
        """Lit une propriété numérique, ``None`` si elle est inutilisable.

        Un infini ou un ``NaN`` empoisonnerait tout total et ne se sérialise
        même pas en JSON: il est refusé comme une valeur absente.
        """
        try:
            value = float(getattr(entity, name))
        except (self._com_error, AttributeError, TypeError, ValueError) as exc:
            _LOG.debug("Propriété %s illisible ou non numérique: %s", name, exc)
            return None
        if not math.isfinite(value):
            _LOG.debug("Propriété %s hors domaine: %s", name, value)
            return None
        return value

    def _flag_property(self, entity: Any, name: str) -> bool | None:
        """Lit une propriété booléenne, ``None`` si elle est illisible."""
        try:
            return bool(getattr(entity, name))
        except (self._com_error, AttributeError, TypeError, ValueError) as exc:
            _LOG.debug("Propriété %s illisible: %s", name, exc)
            return None

    def extents(self) -> tuple[float, float, float, float] | None:
        """Limites du dessin, lues dans ``$EXTMIN`` et ``$EXTMAX``.

        Ces variables ne sont rafraîchies qu'à la régénération. Le backend
        régénérant en fin de lot, elles sont à jour après un :meth:`execute`.
        Un dessin vide porte des valeurs sentinelles de l'ordre de 1e20, dans
        quel cas la méthode rend ``None``.
        """
        self._require_connection()
        return self._submit_read(self._extents_on_worker)

    def _extents_on_worker(self) -> tuple[float, float, float, float] | None:
        doc = self._document()
        low = doc.GetVariable("EXTMIN")
        high = doc.GetVariable("EXTMAX")
        xmin, ymin = float(low[0]), float(low[1])
        xmax, ymax = float(high[0]), float(high[1])
        if abs(xmin) >= _EXTENTS_SENTINEL or xmin > xmax or ymin > ymax:
            return None
        return (xmin, ymin, xmax, ymax)

    def zoom_extents(self) -> None:
        """Cadre la vue sur le dessin. ZoomExtents vit sur l'application."""
        self._require_connection()
        self._submit(self._zoom_extents_on_worker)

    def _zoom_extents_on_worker(self) -> None:
        self._document()
        try:
            self._app.ZoomExtents()
        except self._com_error as exc:
            raise self._fail(exc, "ZoomExtents a échoué") from exc

    # -- commandes natives --------------------------------------------------

    def run_command(self, command: str, arguments: Sequence[str] = ()) -> dict[str, Any]:
        """Transmet une commande native d'AutoCAD, prise dans une liste blanche.

        C'est ce qui démultiplie les capacités du serveur sans coder chaque
        opération: le décalage, l'ajustement, le raccord, le réseau, la symétrie
        et le contour existent déjà dans AutoCAD et valent mieux que leur
        réimplémentation approximative.

        Sécurité
        --------
        ``SendCommand`` transmet du texte à l'interpréteur d'AutoCAD, lequel
        exécute aussi bien une commande qu'une expression AutoLISP. Une chaîne
        libre équivaut donc à une exécution de code arbitraire sur la machine de
        l'utilisateur. Trois barrières, dans cet ordre:

        1. la couche outil filtre contre ``Config.allowed_commands`` ;
        2. **ce backend refilte**, contre ``allowed_commands`` du constructeur,
           parce qu'un moteur qui fait confiance à son appelant n'est pas
           défendu, il est seulement défendu ailleurs ;
        3. le texte réellement transmis est **fabriqué ici**, jamais recopié:
           seuls le nom canonique retenu et des arguments assainis y entrent.

        Sont refusés: les fins de ligne et le point-virgule, qui valent ENTRÉE
        et permettraient d'enchaîner une seconde commande ; la parenthèse
        ouvrante, préfixe d'une expression LISP ; les guillemets, la barre
        oblique inverse, le point d'exclamation et ÉCHAP ; l'espace à
        l'intérieur d'un nom ou d'un argument ; et tout nom qui, préfixes ``.``
        et ``_`` retirés, ne figure pas dans la liste blanche.

        Ce qu'on sait, et ce qu'on ne sait pas
        -------------------------------------
        **``SendCommand`` est traité de façon asynchrone par AutoCAD.** L'appel
        rend la main dès que la chaîne est déposée dans la file de commandes, et
        rien ne dit qu'elle a été exécutée, ni quand, ni avec quel résultat.
        Aucune valeur de retour, aucun code d'erreur. Le backend ne peut donc
        pas affirmer que la commande a abouti, et il ne l'affirme pas:
        ``completed`` vaut toujours ``None``.

        Ce qu'il rapporte est ce qu'il peut constater: le nombre d'entités de
        l'espace objet avant et après l'envoi, et la valeur de ``CMDACTIVE``
        relue ensuite. Un écart non nul prouve qu'il s'est passé quelque chose ;
        un écart nul ne prouve rien, la commande ayant pu ne pas encore tourner.
        ``command_active`` à 1 signale en revanche un cas précis et fréquent:
        la commande attend une saisie qu'on ne lui a pas donnée, et AutoCAD
        restera bloqué dessus, rejetant les appels COM suivants, tant que
        personne n'appuie sur ÉCHAP.

        Aucune marque d'annulation n'est posée: elle serait refermée avant même
        que la commande ne s'exécute, donc elle ne couvrirait rien. AutoCAD
        groupe de lui-même chaque commande dans une étape d'annulation, si bien
        qu'un Ctrl+Z dans l'interface reste le bon geste. ``undo()`` du backend,
        lui, ne défait pas ce que fait cette méthode: il ne connaît que les
        handles des lots qu'il a créés.

        :raises InvalidParameter: commande hors liste blanche ou texte suspect.
        :raises OperationFailed: AutoCAD a refusé l'appel ``SendCommand``.
        """
        name = self._validated_command(command)
        safe_arguments = tuple(
            self._validated_argument(value, index) for index, value in enumerate(arguments)
        )
        self._require_connection()
        return self._submit(lambda: self._run_command_on_worker(name, safe_arguments))

    def _validated_command(self, command: str) -> str:
        """Rend le nom canonique en majuscules, ou lève. N'appelle pas COM.

        Volontairement exécutée sur le thread appelant, avant tout aller-retour:
        une commande refusée ne doit même pas atteindre le thread COM.
        """
        # Seules les espaces sont retirées, jamais les autres blancs: une
        # tabulation ou une fin de ligne doit être refusée, pas normalisée en
        # silence. Elles valent ENTRÉE pour AutoCAD.
        raw = str(command).strip(" ")
        if not raw:
            raise InvalidParameter(
                "Commande vide", supported=sorted(self._allowed_commands)
            )
        found = self._injection_char(raw)
        if found is not None:
            raise InvalidParameter(
                f"Caractère interdit dans une commande: {found!r}",
                got=raw,
                reason="fin de ligne, point-virgule, parenthèse LISP ou caractère d'échappement",
            )
        # Les préfixes « . » (commande intégrée, insensible à UNDEFINE) et « _ »
        # (nom anglais quelle que soit la langue) sont légitimes en entrée. Ils
        # sont retirés pour l'identification, puis réimposés par le backend.
        core = raw.lstrip("._")
        if not _COMMAND_NAME.match(core):
            raise InvalidParameter(
                f"Nom de commande invalide: {raw!r}",
                valid="une lettre puis des lettres ou des chiffres, sans espace",
            )
        name = core.upper()
        if name not in self._allowed_commands:
            raise InvalidParameter(
                f"Commande hors liste blanche: {name}",
                supported=sorted(self._allowed_commands),
                reason="transmettre une commande quelconque revient à exécuter du code arbitraire",
            )
        return name

    def _validated_argument(self, value: str, index: int) -> str:
        """Assainit un argument de commande. N'appelle pas COM."""
        raw = str(value).strip(" ")
        if not raw:
            raise InvalidParameter(
                "Argument de commande vide",
                at=index,
                note="une chaîne vide vaudrait ENTRÉE et validerait l'invite courante",
            )
        found = self._injection_char(raw)
        if found is not None:
            raise InvalidParameter(
                f"Caractère interdit dans un argument: {found!r}", at=index, got=raw
            )
        if not _COMMAND_ARGUMENT.match(raw):
            raise InvalidParameter(
                f"Argument de commande invalide: {raw!r}",
                at=index,
                valid="chiffres, lettres, « . , + - _ @ < », sans espace, 64 caractères au plus",
            )
        return raw

    @staticmethod
    def _injection_char(text: str) -> str | None:
        """Premier caractère d'injection rencontré, ``None`` s'il n'y en a pas."""
        for char in _COMMAND_INJECTION_CHARS:
            if char in text:
                return char
        # Tout caractère de contrôle non listé est refusé au même titre.
        for char in text:
            if ord(char) < 0x20 or ord(char) == 0x7F:
                return char
        return None

    def _run_command_on_worker(self, name: str, arguments: tuple[str, ...]) -> dict[str, Any]:
        doc = self._document()
        before = self._model_space_count(doc)

        # Texte entièrement fabriqué ici. Le point impose la commande intégrée
        # même si elle a été redéfinie par UNDEFINE, le tiret bas impose le nom
        # anglais quelle que soit la langue de l'installation. L'espace finale
        # vaut ENTRÉE et déclenche l'exécution: sans elle, la chaîne resterait
        # en attente sur la ligne de commande.
        payload = " ".join(("._" + name, *arguments)) + " "

        try:
            doc.SendCommand(payload)
        except self._com_error as exc:
            raise self._fail(
                exc,
                f"AutoCAD a refusé la commande {name}",
                command=name,
                sent=payload,
            ) from exc

        after = self._model_space_count(doc)
        active = self._command_active(doc)

        report: dict[str, Any] = {
            "backend": self.name,
            "command": name,
            "arguments": list(arguments),
            "sent": payload,
            # L'appel a été accepté. Ce n'est pas la même chose que réussi.
            "accepted": True,
            # Indéterminable par construction: SendCommand est asynchrone et
            # ne rend rien. Inventer un booléen ici serait le bug historique
            # du projet sous une autre forme.
            "completed": None,
            "entities_before": before,
            "entities_after": after,
            "entities_delta": (
                after - before if before is not None and after is not None else None
            ),
            "command_active": active,
            "note": (
                "SendCommand est traité de façon asynchrone par AutoCAD: l'appel rend la "
                "main sans que la commande ait forcément été exécutée. Les comptes "
                "d'entités sont lus juste après l'envoi et peuvent donc précéder son "
                "effet. Un écart nul ne prouve pas l'échec, un écart non nul ne prouve "
                "pas que la commande est terminée."
            ),
        }
        if active:
            report["warning"] = (
                "AutoCAD est resté dans la commande et attend une saisie. Les appels "
                "suivants seront rejetés tant qu'elle n'est pas terminée ou annulée par "
                "ÉCHAP. Vérifiez le nombre et l'ordre des arguments."
            )
        _LOG.info(
            "Commande %s transmise (%s), entités %s -> %s, CMDACTIVE=%s",
            name,
            payload.strip(),
            before,
            after,
            active,
        )
        return report

    def _model_space_count(self, doc: Any) -> int | None:
        """Nombre d'entités de l'espace objet, ``None`` s'il est illisible."""
        try:
            return int(doc.ModelSpace.Count)
        except self._com_error as exc:
            _LOG.debug("Comptage de l'espace objet impossible: %s", exc)
            return None

    def _command_active(self, doc: Any) -> int | None:
        """Valeur de ``CMDACTIVE``, ``None`` si la variable est illisible.

        Zéro signifie qu'aucune commande n'est en cours. Une valeur non nulle
        signifie qu'AutoCAD est resté dans une commande, donc que la nôtre
        attend une saisie. ``None`` est lui-même un indice: AutoCAD a refusé la
        lecture, ce qui arrive typiquement lorsqu'il est occupé.
        """
        try:
            return int(doc.GetVariable("CMDACTIVE"))
        except self._com_error as exc:
            _LOG.debug("Lecture de CMDACTIVE impossible: %s", exc)
            return None

    # -- persistance -------------------------------------------------------

    def save(self, path: str | None = None) -> str:
        """Enregistre le dessin et rend son chemin complet."""
        self._require_connection()
        return self._submit(lambda: self._save_on_worker(path))

    def _save_on_worker(self, path: str | None) -> str:
        doc = self._document()
        try:
            if path is None:
                doc.Save()
            else:
                doc.SaveAs(str(path))
        except self._com_error as exc:
            raise self._fail(exc, "Enregistrement impossible", path=path) from exc
        return self._safe_str(doc, "FullName")

    # -- diagnostic --------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """État interne utile à la première mise en service sous Windows.

        Rend les constantes ActiveX réellement retenues et les stratégies
        d'appel choisies, ce qui permet de valider en une fois les hypothèses
        listées dans ``docs/windows-checklist.md``.
        """
        constants_used = {
            name: self._const(name) for name in sorted(_FALLBACK_CONSTANTS)
        }
        resolved_from_typelib = {
            name: constants_used[name] != _FALLBACK_CONSTANTS[name]
            for name in constants_used
        }
        return {
            "backend": self.name,
            "platform": sys.platform,
            "modules_loaded": self._pythoncom is not None,
            "worker_alive": self._worker.alive,
            "connected": self._doc is not None,
            "autocad_version": self._safe_str(self._app, "Version") if self._app else None,
            "constants": constants_used,
            "constants_differ_from_fallback": resolved_from_typelib,
            "select_strategy": self._select_strategy,
            "hatch_loop_strategy": self._hatch_loop_strategy,
            "undo_stack_depth": len(self._undo_stack),
            # Ce que ce backend accepte de transmettre à SendCommand. Doit
            # coïncider avec Config.allowed_commands: un écart signale que l'une
            # des deux listes a bougé sans l'autre.
            "allowed_commands": sorted(self._allowed_commands),
            "xdata_appid": _XDATA_APPID,
            "xdata_registered": self._xdata_registered,
        }

    # -- sélection ---------------------------------------------------------

    def _by_handle(self, doc: Any, handle: str) -> Any:
        """Entité par handle, en temps constant.

        ``HandleToObject`` interroge directement la table des handles du
        document. Le code historique parcourait tout le ModelSpace en comparant
        les handles un par un, soit un appel interprocessus par entité.
        """
        if not handle:
            raise InvalidParameter("Handle vide")
        try:
            return doc.HandleToObject(str(handle))
        except self._com_error as exc:
            raise EntityNotFound(
                f"Aucune entité ne porte le handle {handle!r}",
                handle=handle,
                **self._com_details(exc),
            ) from exc

    def _resolve(self, doc: Any, selector: EntityFilter) -> list[Any]:
        """Entités correspondant au filtre, résolues au moindre coût.

        Des handles explicites l'emportent: ils vont droit au but. Les autres
        critères passent par un jeu de sélection filtré côté AutoCAD.
        """
        if selector.handles is not None:
            entities: list[Any] = []
            for handle in selector.handles:
                try:
                    entity = doc.HandleToObject(str(handle))
                except self._com_error:
                    # Un handle inconnu n'est pas une erreur de filtre: le
                    # dessin a pu changer. On l'ignore, on ne l'invente pas.
                    _LOG.debug("Handle %s introuvable, ignoré", handle)
                    continue
                if self._matches_residual(entity, selector):
                    entities.append(entity)
            return entities

        selection = self._select(doc, selector)
        try:
            return [selection.Item(i) for i in range(int(selection.Count))]
        finally:
            self._discard_selection_set(selection)

    def _matches_residual(self, entity: Any, selector: EntityFilter) -> bool:
        """Applique les critères restants à une entité déjà trouvée par handle.

        Ce filtrage Python ne porte que sur les quelques entités désignées
        nommément, jamais sur le dessin entier.
        """
        try:
            if selector.layer is not None and str(entity.Layer).upper() != selector.layer.upper():
                return False
            if selector.color is not None and int(entity.Color) != int(selector.color):
                return False
            if selector.kind is not None:
                wanted = self._dxf_type(selector.kind)
                actual = _OBJECT_NAME_TO_KIND.get(str(entity.ObjectName), "")
                if self._dxf_type(actual) != wanted:
                    return False
            if selector.window is not None:
                xmin, ymin, xmax, ymax = selector.window
                low, high = entity.GetBoundingBox()
                if float(low[0]) < xmin or float(low[1]) < ymin:
                    return False
                if float(high[0]) > xmax or float(high[1]) > ymax:
                    return False
        except self._com_error as exc:
            _LOG.debug("Entité illisible pendant le filtrage: %s", exc)
            return False
        return True

    def _dxf_type(self, kind: str) -> str:
        """Nom DXF pour le code de groupe 0.

        Un nom inconnu du projet est transmis tel quel en majuscules, ce qui
        permet de viser un type DXF que le modèle d'opérations ne couvre pas
        encore, par exemple ``ELLIPSE`` ou ``LEADER``.
        """
        return _KIND_TO_DXF.get(kind.lower(), kind.upper())

    def _select(self, doc: Any, selector: EntityFilter) -> Any:
        """Jeu de sélection filtré côté AutoCAD, par codes de groupe DXF.

        Codes utilisés: 0 pour le type d'entité, 8 pour le calque, 62 pour la
        couleur. Le tri se fait dans AutoCAD, en un appel, au lieu de rapatrier
        chaque entité pour la tester en Python.

        Attention, le filtre par couleur ne voit que la couleur **propre** de
        l'entité. Une entité en ByLayer porte la couleur 256, pas celle de son
        calque.
        """
        codes: list[int] = []
        values: list[Any] = []
        if selector.kind is not None:
            codes.append(0)
            values.append(self._dxf_type(selector.kind))
        if selector.layer is not None:
            codes.append(8)
            values.append(str(selector.layer))
        if selector.color is not None:
            codes.append(62)
            values.append(int(selector.color))

        selection = self._fresh_selection_set(doc)
        try:
            self._run_select(selection, codes, values, selector.window)
        except self._com_error as exc:
            self._discard_selection_set(selection)
            raise self._fail(exc, "Sélection filtrée refusée par AutoCAD") from exc
        return selection

    def _run_select(
        self,
        selection: Any,
        codes: list[int],
        values: list[Any],
        window: tuple[float, float, float, float] | None,
    ) -> None:
        """Appelle ``SelectionSet.Select`` avec ou sans filtre, avec ou sans fenêtre.

        HYPOTHÈSE: en mode ``acSelectionSetAll``, les paramètres Point1 et Point2
        doivent être omis. La façon d'omettre un paramètre optionnel en liaison
        tardive pywin32 n'est pas certaine, d'où une cascade de trois formes.
        La première qui passe est mémorisée pour la session.
        """
        has_filter = bool(codes)
        filter_type = self._shorts(codes) if has_filter else None
        filter_data = self._variants(values) if has_filter else None

        if window is not None:
            # Mode fenêtre: ne retient que les entités entièrement contenues.
            # acSelectionSetCrossing retiendrait aussi celles qui la traversent.
            xmin, ymin, xmax, ymax = window
            mode = self._const("acSelectionSetWindow")
            corner1 = self._point(min(xmin, xmax), min(ymin, ymax))
            corner2 = self._point(max(xmin, xmax), max(ymin, ymax))
            if has_filter:
                selection.Select(mode, corner1, corner2, filter_type, filter_data)
            else:
                selection.Select(mode, corner1, corner2)
            return

        mode = self._const("acSelectionSetAll")
        if not has_filter:
            selection.Select(mode)
            return

        strategies: tuple[str, ...] = ("missing", "none", "keyword")
        if self._select_strategy is not None:
            strategies = (self._select_strategy,)

        last: BaseException | None = None
        for strategy in strategies:
            try:
                if strategy == "missing":
                    empty = self._pythoncom.Missing
                    selection.Select(mode, empty, empty, filter_type, filter_data)
                elif strategy == "none":
                    selection.Select(mode, None, None, filter_type, filter_data)
                else:
                    selection.Select(mode, FilterType=filter_type, FilterData=filter_data)
            except (AttributeError, TypeError) as exc:
                # pywin32 refuse la forme avant même d'atteindre AutoCAD.
                last = exc
                _LOG.debug("Forme de Select %s inutilisable: %s", strategy, exc)
                continue
            except self._com_error as exc:
                last = exc
                _LOG.debug("Forme de Select %s refusée par AutoCAD: %s", strategy, exc)
                continue
            self._select_strategy = strategy
            return

        assert last is not None
        raise last

    def _fresh_selection_set(self, doc: Any) -> Any:
        """Jeu de sélection vide, portant toujours le même nom.

        Les jeux de sélection persistent dans le document: réutiliser le nom sans
        supprimer l'ancien lève une erreur « déjà existant » au deuxième appel.
        """
        name = self._selection_set_name
        try:
            doc.SelectionSets.Item(name).Delete()
        except self._com_error as exc:
            # Cas normal au premier appel: le jeu n'existe pas encore.
            _LOG.debug("Pas de jeu de sélection %s à supprimer: %s", name, exc)
        try:
            return doc.SelectionSets.Add(name)
        except self._com_error as exc:
            raise self._fail(
                exc, f"Création du jeu de sélection {name!r} impossible", name=name
            ) from exc

    def _discard_selection_set(self, selection: Any) -> None:
        """Supprime le jeu de sélection. Il ne doit pas rester dans le document."""
        try:
            selection.Delete()
        except self._com_error as exc:
            _LOG.debug("Suppression du jeu de sélection impossible: %s", exc)

    # -- divers ------------------------------------------------------------

    def _regen(self, doc: Any) -> None:
        """Régénère l'affichage. Une seule fois par lot, jamais par entité.

        Le regen est l'appel le plus coûteux de l'API: il reconstruit
        l'affichage du dessin entier. Le code historique en déclenchait un par
        entité créée, ce qui rendait un plan de deux cents traits inutilisable.
        """
        try:
            doc.Regen(self._const("acActiveViewport"))
        except self._com_error as exc:
            # Purement visuel: les entités existent, leurs handles sont réels.
            _LOG.warning("Regen a échoué, l'affichage peut être en retard: %s", exc)

    def _safe_str(self, obj: Any, attribute: str) -> str:
        """Lit une propriété texte facultative sans faire échouer l'appelant."""
        if obj is None:
            return ""
        try:
            value = getattr(obj, attribute)
        except self._com_error as exc:
            _LOG.debug("Propriété %s illisible: %s", attribute, exc)
            return ""
        return "" if value is None else str(value)

    def _short(self, exc: BaseException) -> str:
        """Message COM condensé, pour l'insérer dans une liste de problèmes."""
        details = self._com_details(exc)
        return (
            details.get("com_description")
            or details.get("com_message")
            or details.get("hresult_hex")
            or str(exc)
        )


class _NeverRaised(Exception):
    """Sentinelle utilisée comme type d'exception avant le chargement de COM.

    Elle permet d'écrire ``except self._com_error`` partout sans jamais tester
    si pywin32 est chargé: tant qu'il ne l'est pas, aucun code COM ne tourne, et
    cette exception ne peut par construction pas être levée.
    """


__all__ = ["AcadComBackend"]
