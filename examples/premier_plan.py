"""Premier essai: dessine une pièce et l'écrit en DXF, sans AutoCAD.

À lancer après l'installation, avant de brancher le moindre éditeur::

    uv run python examples/premier_plan.py

Si ce script produit un fichier, le cœur du serveur fonctionne sur votre
machine. Tout problème ultérieur viendra de la configuration de l'éditeur.
"""

from __future__ import annotations

import sys
from pathlib import Path

from autocad_mcp.backends.ezdxf_be import EzdxfBackend
from autocad_mcp.model.ops import OperationBatch
from autocad_mcp.ops.architecture import Opening, label, wall_network
from autocad_mcp.units import Defaults, Unit


def main() -> int:
    sortie = Path(sys.argv[1] if len(sys.argv) > 1 else "essai.dxf").resolve()
    defauts = Defaults(Unit.METER)

    plan = wall_network(
        [(0.0, 0.0), (8.0, 0.0), (8.0, 5.0), (0.0, 5.0)],
        defauts,
        thickness=0.2,
        closed=True,
        openings=[
            Opening(segment=0, position=0.5, width=0.9, kind="door"),
            Opening(segment=2, position=0.5, width=1.4, kind="window"),
        ],
    )
    plan += label((4.0, 2.5), "ESSAI", defauts)

    backend = EzdxfBackend(unit=Unit.METER)
    backend.connect()
    resultat = backend.execute(OperationBatch(tuple(plan), label="essai"))

    if not resultat.ok:
        print("ÉCHEC:", resultat.failures)
        return 1

    backend.save(str(sortie))
    print(f"{len(resultat.created)} entités créées sur {len(resultat.layers)} calques")
    print(f"Fichier écrit: {sortie}")
    print("Ouvrez-le dans AutoCAD: une pièce de 8 m sur 5, une porte, une fenêtre.")

    # Le rendu est facultatif: il dépend de l'extra « render ».
    try:
        from autocad_mcp.render import render_png
    except ImportError:
        print("(rendu non disponible: relancez `uv sync --extra render`)")
        return 0

    image = sortie.with_suffix(".png")
    image.write_bytes(render_png(backend.document, 1200, 900))
    print(f"Aperçu écrit:   {image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
