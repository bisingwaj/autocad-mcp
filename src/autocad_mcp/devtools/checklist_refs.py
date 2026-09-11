"""Vérifie que la checklist de portage et le backend COM se désignent juste.

``docs/windows-checklist.md`` est le seul filet du backend AutoCAD: il est écrit
sur une machine sans AutoCAD, et c'est lui qu'on déroulera ligne par ligne
devant le logiciel. Deux façons pour lui de pourrir en silence:

* le code cite un identifiant ``W-xx`` qui n'existe pas, ou plus, dans le
  document — le jour de l'essai, le renvoi ne mène nulle part ;
* une entrée cite ``acad_com.py:982`` alors que le fichier a grossi depuis, et
  la ligne désigne désormais autre chose — pire qu'une absence de référence,
  parce qu'on la suit.

Aucune de ces deux dérives ne se voit à la relecture, et aucune n'est attrapée
par les tests: elles portent sur la cohérence entre un document et du code.

Ce que l'outil sait faire
-------------------------
1. Tout ``W-xx`` cité dans ``acad_com.py`` a une entrée dans la checklist.
2. Les identifiants de la checklist sont uniques, et le compte annoncé en tête
   du document est exact.
3. Toute référence ``acad_com.py:N`` tombe dans le fichier.
4. Pour **chaque** fonction nommée dans la ligne ``- **Où** :`` d'une entrée, au
   moins une de ses références de ligne tombe dans cette fonction. C'est le
   contrôle qui attrape les numéros périmés.

Le point 4 ne lit que la ligne ``Où``, et non l'entrée entière: une entrée cite
volontiers d'autres fonctions dans sa prose, et les prendre pour un emplacement
produirait des faux positifs. Une entrée peut par ailleurs porter plusieurs
références, dont une seule désigne la fonction et les autres une constante ou un
passage voisin: c'est pourquoi **une seule suffit** à satisfaire le contrôle.

``--fix`` recale les références du point 4 sur la ligne ``def`` de la fonction
nommée. Il ne touche qu'à **une** référence par fonction, la plus proche de sa
position actuelle, et seulement si aucune ne tombe déjà dedans: les autres
désignent délibérément une constante ou un appel précis. Les plages du genre
``1072-1075`` ne sont jamais réécrites, l'outil ne sait pas retrouver le passage
qu'elles couvrent.

Usage::

    uv run python -m autocad_mcp.devtools.checklist_refs
    uv run python -m autocad_mcp.devtools.checklist_refs --fix
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Racine du dépôt, déduite de l'emplacement de ce fichier.
_ROOT = Path(__file__).resolve().parents[3]

BACKEND = _ROOT / "src" / "autocad_mcp" / "backends" / "acad_com.py"
CHECKLIST = _ROOT / "docs" / "windows-checklist.md"

#: Identifiant d'entrée, tel qu'il est cité dans le code comme dans le document.
_ID = re.compile(r"\bW-(\d{2,3})\b")

#: Titre d'entrée: ``### W-42 — quelque chose``.
_HEADING = re.compile(r"^###\s+W-(\d{2,3})\s*(?:—|-)?\s*(.*)$")

#: Référence de ligne dans le backend, éventuellement une plage.
_LINE_REF = re.compile(r"acad_com\.py:(\d+)(-\d+)?")

#: Ligne qui dit où se trouve le code, seule lue pour localiser une entrée.
_WHERE = re.compile(r"^\s*-\s+\*\*Où\*\*\s*:")

#: Nom de fonction cité entre accents graves dans une entrée.
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")

#: Nombre d'entrées annoncé en tête du document.
_ANNOUNCED = re.compile(r"\*\*(\d+)\s+entrées\*\*")


@dataclass(slots=True)
class Entry:
    """Une entrée de la checklist et ce qu'elle désigne dans le code."""

    ident: str
    title: str
    #: Ligne du titre dans le document, pour un message actionnable.
    heading_line: int
    #: Ligne du document, numéro cité, plage éventuelle.
    refs: list[tuple[int, int, str]] = field(default_factory=list)
    #: Noms cités entre accents graves **dans la ligne « Où »** seulement,
    #: dans leur ordre d'apparition: le premier est l'emplacement principal.
    located: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Span:
    """Portée d'une fonction ou d'une méthode du backend."""

    name: str
    start: int
    end: int


def _spans(source: str) -> dict[str, list[Span]]:
    """Portée de chaque fonction du backend, par nom.

    Un même nom peut apparaître plusieurs fois — deux classes, une fonction
    imbriquée — d'où la liste. Le contrôle est satisfait dès qu'une des portées
    convient, ce qui évite de refuser une référence juste pour cause d'homonymie.
    """
    found: dict[str, list[Span]] = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        found.setdefault(node.name, []).append(Span(node.name, node.lineno, end))
    return found


def _parse_checklist(text: str) -> tuple[list[Entry], int | None]:
    """Lit les entrées du document, avec leurs références et leurs mentions."""
    entries: list[Entry] = []
    current: Entry | None = None
    for number, line in enumerate(text.splitlines(), 1):
        heading = _HEADING.match(line)
        if heading is not None:
            current = Entry(
                ident=f"W-{heading.group(1)}",
                title=heading.group(2).strip(),
                heading_line=number,
            )
            entries.append(current)
            continue
        if current is None:
            continue
        if line.startswith("## "):
            # Un titre de phase clôt l'entrée en cours.
            current = None
            continue
        if _WHERE.match(line):
            for name in _BACKTICKED.findall(line):
                if name not in current.located:
                    current.located.append(name)
        for match in _LINE_REF.finditer(line):
            current.refs.append((number, int(match.group(1)), match.group(2) or ""))

    announced = _ANNOUNCED.search(text)
    return entries, int(announced.group(1)) if announced else None


def _cited_in_code(source: str) -> dict[str, list[int]]:
    """Identifiants ``W-xx`` cités dans le backend, et où."""
    cited: dict[str, list[int]] = {}
    for number, line in enumerate(source.splitlines(), 1):
        for match in _ID.finditer(line):
            cited.setdefault(f"W-{match.group(1)}", []).append(number)
    return cited


def _located_functions(entry: Entry, spans: dict[str, list[Span]]) -> list[str]:
    """Fonctions du backend que la ligne « Où » de l'entrée désigne.

    Dans l'ordre où elles y sont citées: la première est l'emplacement
    principal, et c'est elle qui l'emporte si deux fonctions se disputent la
    même référence à recaler.
    """
    return [name for name in entry.located if name in spans]


def _check(*, fix: bool) -> int:
    source = BACKEND.read_text(encoding="utf-8")
    text = CHECKLIST.read_text(encoding="utf-8")
    total_lines = len(source.splitlines())

    entries, announced = _parse_checklist(text)
    by_id = {entry.ident: entry for entry in entries}
    spans = _spans(source)
    cited = _cited_in_code(source)

    problems: list[str] = []
    warnings: list[str] = []
    repairs: list[tuple[int, int, int]] = []

    # 1. Les renvois du code résolvent-ils ?
    for ident, lines in sorted(cited.items()):
        if ident not in by_id:
            where = ", ".join(f"acad_com.py:{n}" for n in lines)
            problems.append(f"{ident} cité dans le code ({where}) n'existe pas dans la checklist")

    # 2. Identifiants uniques, compte annoncé exact.
    seen: set[str] = set()
    for entry in entries:
        if entry.ident in seen:
            problems.append(
                f"{entry.ident} est défini deux fois "
                f"(windows-checklist.md:{entry.heading_line})"
            )
        seen.add(entry.ident)
    if announced is not None and announced != len(entries):
        problems.append(
            f"l'en-tête annonce {announced} entrées, le document en contient {len(entries)}"
        )

    # 3. Toute référence tombe-t-elle dans le fichier ?
    for entry in entries:
        for doc_line, cited_line, _suffix in entry.refs:
            if cited_line < 1 or cited_line > total_lines:
                problems.append(
                    f"{entry.ident} renvoie à acad_com.py:{cited_line}, hors du fichier "
                    f"({total_lines} lignes) — windows-checklist.md:{doc_line}"
                )

    # 4. Chaque fonction nommée est-elle réellement désignée par une référence ?
    for entry in entries:
        for name in _located_functions(entry, spans):
            candidates = spans[name]
            inside = [
                ref
                for ref in entry.refs
                if any(span.start <= ref[1] <= span.end for span in candidates)
            ]
            if inside:
                continue

            target = candidates[0]
            # La dérive des numéros est monotone: le fichier ne fait que
            # grossir. La référence à recaler est donc la plus proche de la
            # position actuelle de la fonction, et les autres désignent
            # délibérément une constante ou un passage voisin.
            movable = [ref for ref in entry.refs if not ref[2]]
            if not movable:
                warnings.append(
                    f"{entry.ident} ne désigne `{name}` (lignes {target.start}-{target.end}) "
                    "que par une plage, non recalable automatiquement "
                    f"— windows-checklist.md:{entry.heading_line}"
                )
                continue
            doc_line, cited_line, _ = min(movable, key=lambda ref: abs(ref[1] - target.start))
            if fix:
                if any(existing[:2] == (doc_line, cited_line) for existing in repairs):
                    # Deux fonctions nommées se disputent la même référence:
                    # la première citée dans la ligne « Où » a déjà gagné.
                    continue
                repairs.append((doc_line, cited_line, target.start))
            else:
                warnings.append(
                    f"{entry.ident} renvoie à acad_com.py:{cited_line}, hors de "
                    f"`{name}` qui commence ligne {target.start} "
                    f"— windows-checklist.md:{doc_line}"
                )

    if fix and repairs:
        document = text.splitlines(keepends=True)
        for doc_line, old_line, new_line in repairs:
            index = doc_line - 1
            document[index] = document[index].replace(
                f"acad_com.py:{old_line}", f"acad_com.py:{new_line}"
            )
        CHECKLIST.write_text("".join(document), encoding="utf-8")
        for doc_line, old_line, new_line in repairs:
            print(f"  recalé windows-checklist.md:{doc_line}  {old_line} -> {new_line}")
        print(f"\n{len(repairs)} référence(s) recalée(s). Relancer sans --fix pour contrôler.")
        return 0

    print(f"Checklist : {len(entries)} entrées, {sum(len(e.refs) for e in entries)} références.")
    print(f"Backend   : {total_lines} lignes, {len(cited)} identifiants cités.\n")

    unreferenced = sorted(set(by_id) - set(cited))
    if unreferenced:
        # Pas un défaut: beaucoup d'entrées décrivent un geste de vérification
        # sans qu'aucun commentaire du code n'ait à s'y reporter.
        print(f"{len(unreferenced)} entrée(s) qu'aucun commentaire du code ne cite.\n")

    for line in warnings:
        print(f"ATTENTION {line}")
    for line in problems:
        print(f"ERREUR    {line}")

    if problems:
        print(f"\n{len(problems)} renvoi(s) cassé(s).")
        return 1
    if warnings:
        print(f"\n{len(warnings)} référence(s) de ligne à recaler: relancer avec --fix.")
        return 1
    print("Tous les renvois résolvent et toutes les lignes désignent la bonne construction.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="recale les références de ligne sur la fonction que l'entrée nomme",
    )
    args = parser.parse_args(argv)
    if not BACKEND.exists() or not CHECKLIST.exists():
        print("backend ou checklist introuvable", file=sys.stderr)
        return 2
    return _check(fix=args.fix)


if __name__ == "__main__":
    raise SystemExit(main())
