"""Comparaison du contrat réellement honoré par chaque backend.

Le backend AutoCAD ne peut pas être exécuté sur la machine de développement.
Sa conformité repose donc sur la symétrie avec le backend `ezdxf`, qui lui est
testé. Cet outil rend l'écart visible au lieu de le laisser se découvrir sous
Windows, une opération à la fois.

Usage: ``uv run python -m autocad_mcp.devtools.contract_diff``
"""

from __future__ import annotations

import inspect
import sys

from ..backends.base import CadBackend


def _implemented(cls: type) -> set[str]:
    """Méthodes du contrat réellement redéfinies par une classe."""
    contract = {
        name
        for name, member in inspect.getmembers(CadBackend, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    contract |= {
        name
        for name, member in inspect.getmembers(CadBackend)
        if isinstance(member, property) and not name.startswith("_")
    }
    own: set[str] = set()
    for name in contract:
        attr = cls.__dict__.get(name)
        if attr is not None:
            own.add(name)
            continue
        # Hérité d'une classe intermédiaire mais pas de CadBackend.
        for base in cls.__mro__[1:]:
            if base is CadBackend:
                break
            if name in base.__dict__:
                own.add(name)
                break
    return own


def main() -> int:
    from ..backends.recording import RecordingBackend

    backends: dict[str, type] = {"recording": RecordingBackend}

    try:
        from ..backends.ezdxf_be import EzdxfBackend

        backends["ezdxf"] = EzdxfBackend
    except ImportError as exc:
        print(f"ezdxf indisponible: {exc}", file=sys.stderr)

    # Import du backend COM: la classe doit se charger même hors Windows.
    try:
        from ..backends.acad_com import AcadComBackend

        backends["autocad"] = AcadComBackend
    except ImportError as exc:
        print(f"ATTENTION acad_com non importable: {exc}", file=sys.stderr)
        print("Ce backend doit rester importable sur macOS.", file=sys.stderr)
        return 2

    abstract = sorted(CadBackend.__abstractmethods__)
    optional = sorted(
        name
        for name in ("undo", "extents", "zoom_extents", "save")
        if hasattr(CadBackend, name)
    )

    print(f"Contrat: {len(abstract)} méthodes obligatoires, {len(optional)} optionnelles\n")

    impl = {name: _implemented(cls) for name, cls in backends.items()}
    names = list(backends)

    header = f"{'méthode':<18}" + "".join(f"{n:>12}" for n in names)
    print(header)
    print("-" * len(header))

    gaps = 0
    for method in abstract + optional:
        marks = []
        present = []
        for n in names:
            has = method in impl[n]
            present.append(has)
            marks.append("oui" if has else "—")
        required = method in abstract
        if required and not all(present):
            gaps += 1
        line = f"{method:<18}" + "".join(f"{m:>12}" for m in marks)
        print(line + ("   <- MANQUANT" if required and not all(present) else ""))

    print()
    if gaps:
        print(f"{gaps} méthode(s) obligatoire(s) non honorée(s) partout.")
        return 1
    print("Contrat honoré par tous les backends chargés.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
