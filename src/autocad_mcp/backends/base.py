"""Contrat commun à tous les moteurs de dessin.

Un backend exécute des opérations et rend des handles. Il ne calcule aucune
géométrie et ne prend aucune décision métier. Cette frontière est ce qui permet
de tester le projet sur une machine sans AutoCAD.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..errors import UnsupportedOperation
from ..model.ops import Operation, OperationBatch
from ..units import Unit


@dataclass(frozen=True, slots=True)
class EntityRef:
    """Entité réellement créée dans le document.

    ``handle`` provient du moteur. Il n'est jamais inventé: le code historique
    fabriquait des chaînes comme ``line_created`` quand l'opération échouait,
    ce qui rendait toute correction impossible.
    """

    handle: str
    kind: str
    layer: str


@dataclass(slots=True)
class BatchResult:
    """Résultat d'exécution d'un lot.

    Porte à la fois les succès et les échecs. Un lot partiellement exécuté est
    rapporté comme tel, jamais comme un succès complet.

    ``created`` ne contient que les **entités de l'espace objet**. Un calque
    n'en est pas une: il appartient à la table des calques du document. Le
    compter ici gonflerait le nombre d'objets dessinés, ferait supprimer des
    calques à l'annulation, et rendrait ``handles`` inutilisable pour désigner
    ce qu'il faut effacer. Les calques touchés sont donc rapportés à part,
    dans ``layers``.

    Une **définition de bloc** relève du même raisonnement: elle vit dans la
    table des blocs, elle n'est pas dessinée, et la supprimer à l'annulation
    emporterait toutes les occurrences insérées par d'autres lots. Elle est
    donc rapportée dans ``blocks``, jamais dans ``created``. Seule l'occurrence
    ``AddBlockRef`` est une entité.
    """

    created: list[EntityRef] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: Noms des calques créés ou confirmés par ce lot.
    layers: list[str] = field(default_factory=list)
    #: Noms des blocs définis ou confirmés par ce lot.
    blocks: list[str] = field(default_factory=list)
    label: str = "mcp"

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "label": self.label,
            "created_count": len(self.created),
            "handles": [e.handle for e in self.created],
        }
        if self.layers:
            payload["layers"] = self.layers
        if self.blocks:
            payload["blocks"] = self.blocks
        if self.failures:
            payload["failed_count"] = len(self.failures)
            payload["failures"] = self.failures
        return payload


#: Clés de ``EntityInfo.extra`` que la logique de mesure sait exploiter.
#: Un backend qui peut les calculer doit les publier, faute de quoi
#: ``ops.query`` rapporte la mesure comme manquante plutôt que fausse.
MEASURE_KEYS = ("length", "area")


@dataclass(frozen=True, slots=True)
class EntityInfo:
    """Description d'une entité lue dans le document.

    ``extra`` porte le peu d'information qui permet au modèle de reconnaître
    une entité, plus les mesures quand le backend sait les calculer: la clé
    ``length`` pour tout ce qui a une longueur développée, la clé ``area``
    pour toute surface fermée. Voir ``MEASURE_KEYS``.
    """

    handle: str
    kind: str
    layer: str
    color: int
    #: Boîte englobante, absente si le moteur ne sait pas la calculer.
    bbox: tuple[float, float, float, float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "handle": self.handle,
            "type": self.kind,
            "layer": self.layer,
            "color": self.color,
        }
        if self.bbox is not None:
            payload["bbox"] = list(self.bbox)
        payload.update(self.extra)
        return payload


@dataclass(frozen=True, slots=True)
class EntityFilter:
    """Filtre de sélection.

    Les critères se combinent par ET logique. Un backend qui sait filtrer côté
    moteur, par jeu de sélection AutoCAD ou par requête DXF, doit le faire
    plutôt que de parcourir toutes les entités une par une.
    """

    layer: str | None = None
    kind: str | None = None
    color: int | None = None
    handles: tuple[str, ...] | None = None
    #: Fenêtre rectangulaire xmin, ymin, xmax, ymax.
    window: tuple[float, float, float, float] | None = None

    @property
    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (self.layer, self.kind, self.color, self.handles, self.window)
        )


@runtime_checkable
class SupportsRender(Protocol):
    """Backend capable de produire une image du dessin."""

    def render_png(self, width: int, height: int) -> bytes: ...


class CadBackend(ABC):
    """Moteur de dessin.

    Toute méthode qui ne peut pas être honorée lève ``UnsupportedOperation``.
    Ignorer silencieusement une opération produirait un dessin incomplet sans
    que personne ne le sache, ce qui est le pire des comportements.
    """

    #: Nom court, utilisé dans les journaux et les réponses d'outils.
    name: str = "abstract"

    # ---- cycle de vie -------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Ouvre ou rejoint un document. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Libère les ressources. Ne ferme pas AutoCAD."""

    @property
    @abstractmethod
    def unit(self) -> Unit:
        """Unité de longueur du document courant."""

    # ---- écriture -----------------------------------------------------

    @abstractmethod
    def execute(self, batch: OperationBatch) -> BatchResult:
        """Exécute un lot comme une transaction unique.

        L'implémentation doit encadrer le lot par une marque d'annulation et ne
        déclencher qu'un seul rafraîchissement d'affichage, à la fin.
        """

    def execute_one(self, operation: Operation, label: str = "mcp") -> BatchResult:
        """Raccourci pour une opération isolée."""
        return self.execute(OperationBatch((operation,), label=label))

    @abstractmethod
    def delete(self, selector: EntityFilter) -> list[str]:
        """Supprime les entités correspondant au filtre, rend leurs handles."""

    @abstractmethod
    def set_color(self, handle: str, color: int) -> None:
        """Change la couleur d'une entité désignée par son handle."""

    def undo(self) -> None:
        """Annule le dernier lot."""
        raise UnsupportedOperation(f"{self.name} ne gère pas l'annulation")

    # ---- lecture ------------------------------------------------------

    @abstractmethod
    def document_info(self) -> dict[str, Any]:
        """Métadonnées du document: nom, unité, nombre d'entités, calques."""

    @abstractmethod
    def query(
        self, selector: EntityFilter | None = None, *, limit: int | None = None
    ) -> list[EntityInfo]:
        """Liste les entités correspondant au filtre.

        ``limit`` borne la réponse. Renvoyer un dessin entier de cinquante mille
        entités saturerait le contexte du modèle sans rien lui apprendre.
        """

    @abstractmethod
    def count(self, selector: EntityFilter | None = None) -> int:
        """Compte les entités sans les matérialiser."""

    def extents(self) -> tuple[float, float, float, float] | None:
        """Boîte englobante de tout le dessin, si le moteur sait la calculer."""
        raise UnsupportedOperation(f"{self.name} ne calcule pas les limites")

    def zoom_extents(self) -> None:
        """Cadre la vue sur l'ensemble du dessin. Sans objet hors interface."""
        return None

    # ---- commandes natives ---------------------------------------------

    def run_command(self, command: str, arguments: Sequence[str] = ()) -> dict[str, Any]:
        """Exécute une commande native du logiciel de CAO.

        Démultiplie les capacités sans coder chaque opération: le décalage,
        l'ajustement, le raccord, le réseau et le contour existent déjà dans
        AutoCAD et valent mieux que leur réimplémentation.

        **La commande doit être validée contre une liste blanche avant d'arriver
        ici.** Transmettre une chaîne libre à un logiciel de CAO revient à
        exécuter du code arbitraire sur la machine de l'utilisateur. La liste
        vit dans ``Config.allowed_commands`` et la couche outil la fait
        respecter.

        Un backend qui n'a pas d'interpréteur de commandes lève
        ``UnsupportedOperation``.
        """
        raise UnsupportedOperation(
            f"{self.name} n'exécute pas de commandes natives",
            command=command,
        )

    # ---- persistance --------------------------------------------------

    def save(self, path: str | None = None) -> str:
        raise UnsupportedOperation(f"{self.name} ne gère pas l'enregistrement")
