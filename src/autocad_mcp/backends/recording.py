"""Backend d'enregistrement, destiné aux tests.

N'écrit nulle part. Il conserve les opérations reçues afin qu'un test puisse
affirmer sur ce que la logique métier a décidé de dessiner, sans dépendre
d'AutoCAD ni même d'un fichier. C'est le moyen le plus direct de vérifier
qu'un mur produit bien une polyligne fermée sur le bon calque.
"""

from __future__ import annotations

import itertools
from typing import Any

from ..errors import (
    CadError,
    ConfirmationRequired,
    EntityNotFound,
    InvalidParameter,
)
from ..model.ops import (
    BYLAYER,
    DefineBlock,
    EnsureLayer,
    Operation,
    OperationBatch,
    validate,
)
from ..units import Unit
from .base import BatchResult, CadBackend, EntityFilter, EntityInfo, EntityRef


class RecordingBackend(CadBackend):
    """Moteur factice qui mémorise tout et ne dessine rien."""

    name = "recording"

    def __init__(self, unit: Unit = Unit.METER) -> None:
        self._unit = unit
        self._connected = False
        self._counter = itertools.count(1)
        #: Historique complet, lot par lot, dans l'ordre d'exécution.
        self.batches: list[OperationBatch] = []
        #: Entités vivantes, indexées par handle.
        self._entities: dict[str, tuple[Operation, EntityInfo]] = {}
        #: Calques créés, pour vérifier qu'une structure prépare bien son calque.
        self.layers: dict[str, EnsureLayer] = {}
        #: Définitions de blocs, indexées par nom.
        self.blocks: dict[str, DefineBlock] = {}
        #: Handles créés par lot, pour l'annulation.
        self._per_batch: list[list[str]] = []

    # ---- cycle de vie -------------------------------------------------

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    @property
    def unit(self) -> Unit:
        return self._unit

    # ---- accès pratique pour les tests --------------------------------

    @property
    def operations(self) -> list[Operation]:
        """Toutes les opérations reçues, tous lots confondus."""
        return [op for batch in self.batches for op in batch.operations]

    def ops_of_kind(self, kind: str) -> list[Operation]:
        """Opérations d'un type donné, par exemple ``polyline``."""
        return [op for op in self.operations if op.kind == kind]

    def ops_on_layer(self, layer: str) -> list[Operation]:
        """Opérations déposées sur un calque donné.

        ``EnsureLayer`` est exclu: il agit sur la table des calques et ne porte
        donc pas de style.
        """
        return [
            op
            for op in self.operations
            if not isinstance(op, EnsureLayer | DefineBlock) and op.style.layer == layer
        ]

    def reset(self) -> None:
        self.batches.clear()
        self._entities.clear()
        self.layers.clear()
        self.blocks.clear()
        self._per_batch.clear()

    # ---- écriture -----------------------------------------------------

    def execute(self, batch: OperationBatch) -> BatchResult:
        if not self._connected:
            self.connect()
        self.batches.append(batch)
        result = BatchResult(label=batch.label)
        created_here: list[str] = []

        for index, op in enumerate(batch.operations):
            try:
                # Même validation que les vrais backends: ce moteur ne serait
                # pas un substitut fidèle s'il acceptait ce qu'AutoCAD refuse.
                validate(op)
                if isinstance(op, EnsureLayer):
                    self.layers[op.name] = op
                    if op.name not in result.layers:
                        result.layers.append(op.name)
                    continue
                if isinstance(op, DefineBlock):
                    # Une définition vit dans la table des blocs, pas dans
                    # l'espace objet. La compter parmi les entités dessinées
                    # fausserait le décompte et l'annulation. Sémantique de
                    # garantie: un nom déjà pris n'est pas redéfini.
                    self.blocks.setdefault(op.name, op)
                    if op.name not in result.blocks:
                        result.blocks.append(op.name)
                    continue
                ref = self._record(op)
                # Calque non déclaré: on le crée à la volée plutôt que de
                # refuser l'entité, et on le signale. Même sémantique que les
                # backends DXF et AutoCAD, verrouillée par la suite de contrat.
                if ref.layer not in self.layers:
                    self.layers[ref.layer] = EnsureLayer(name=ref.layer, color=7)
                    if ref.layer not in result.layers:
                        result.layers.append(ref.layer)
            except CadError as exc:
                result.failures.append(
                    {
                        "index": index,
                        "operation": op.kind,
                        "code": exc.code,
                        "error": exc.message,
                    }
                )
                continue
            result.created.append(ref)
            created_here.append(ref.handle)

        self._per_batch.append(created_here)
        return result

    def _record(self, op: Operation) -> EntityRef:
        style = getattr(op, "style", None)
        layer = style.layer if style is not None else "0"
        color = style.color if style is not None else BYLAYER
        handle = f"{next(self._counter):X}"
        info = EntityInfo(
            handle=handle,
            kind=op.kind,
            layer=layer,
            color=color,
            bbox=self._bbox_of(op),
        )
        self._entities[handle] = (op, info)
        return EntityRef(handle=handle, kind=op.kind, layer=layer)

    @staticmethod
    def _bbox_of(op: Operation) -> tuple[float, float, float, float] | None:
        """Boîte englobante approchée, suffisante pour tester les filtres."""
        points: list[tuple[float, float]] = []
        if op.kind == "line":
            points = [op.start[:2], op.end[:2]]
        elif op.kind == "polyline":
            points = list(op.points)
        elif op.kind in {"circle", "arc"}:
            cx, cy = op.center[0], op.center[1]
            r = op.radius
            points = [(cx - r, cy - r), (cx + r, cy + r)]
        elif op.kind in {"text", "mtext", "block_ref"}:
            attr = "insert" if op.kind == "block_ref" else "position"
            p = getattr(op, attr)
            points = [p[:2]]
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def delete(self, selector: EntityFilter) -> list[str]:
        # Un filtre vide désigne le dessin entier. Les deux autres backends
        # exigent une confirmation dans ce cas; ce moteur ne serait pas un
        # substitut fidèle s'il effaçait tout sans broncher.
        if selector.is_empty:
            raise ConfirmationRequired(
                "Suppression de toutes les entités: préciser un filtre ou confirmer"
            )
        handles = [info.handle for _, info in self._matching(selector)]
        for handle in handles:
            self._entities.pop(handle, None)
        return handles

    def set_color(self, handle: str, color: int) -> None:
        entry = self._entities.get(handle)
        if entry is None:
            raise EntityNotFound(f"Handle inconnu: {handle}", handle=handle)
        op, info = entry
        self._entities[handle] = (
            op,
            EntityInfo(info.handle, info.kind, info.layer, color, info.bbox, info.extra),
        )

    def undo(self) -> None:
        if not self._per_batch:
            return
        for handle in self._per_batch.pop():
            self._entities.pop(handle, None)
        if self.batches:
            self.batches.pop()

    # ---- lecture ------------------------------------------------------

    def document_info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "unit": self._unit.value,
            "entity_count": len(self._entities),
            "batch_count": len(self.batches),
            "layers": sorted(self.layers),
            "blocks": sorted(self.blocks),
        }

    def _matching(
        self, selector: EntityFilter | None
    ) -> list[tuple[Operation, EntityInfo]]:
        entries = list(self._entities.values())
        if selector is None or selector.is_empty:
            return entries
        out = []
        for op, info in entries:
            if selector.layer is not None and info.layer != selector.layer:
                continue
            if selector.kind is not None and info.kind != selector.kind:
                continue
            if selector.color is not None and info.color != selector.color:
                continue
            if selector.handles is not None and info.handle not in selector.handles:
                continue
            if selector.window is not None:
                if info.bbox is None:
                    continue
                wx1, wy1, wx2, wy2 = selector.window
                bx1, by1, bx2, by2 = info.bbox
                if bx2 < wx1 or bx1 > wx2 or by2 < wy1 or by1 > wy2:
                    continue
            out.append((op, info))
        return out

    def query(
        self, selector: EntityFilter | None = None, *, limit: int | None = None
    ) -> list[EntityInfo]:
        if limit is not None and limit <= 0:
            raise InvalidParameter("limit doit être strictement positif", limit=limit)
        infos = [info for _, info in self._matching(selector)]
        return infos if limit is None else infos[:limit]

    def count(self, selector: EntityFilter | None = None) -> int:
        return len(self._matching(selector))

    def extents(self) -> tuple[float, float, float, float] | None:
        boxes = [i.bbox for _, i in self._entities.values() if i.bbox is not None]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
