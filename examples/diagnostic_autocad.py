"""Diagnostic du pont AutoCAD, à lancer sous Windows avec AutoCAD ouvert.

    uv run python examples/diagnostic_autocad.py

Ce pont a été écrit sur une machine sans AutoCAD: aucune de ses lignes n'a
jamais tourné face au logiciel réel. Ce script est le premier contact, et il
est conçu pour ne rien casser: il lit, il n'écrit pas.

Il valide d'un coup les constantes ActiveX supposées. Autodesk ne publie pas
leurs valeurs numériques, et une constante fausse rend un jeu de sélection
**vide sans lever d'erreur**, ce qui est le défaut le plus difficile à repérer.

Voir docs/windows-checklist.md pour la suite, point par point.
"""

from __future__ import annotations

import json
import platform
import sys


def main() -> int:
    print(f"Plateforme: {platform.system()} {platform.release()}")
    print(f"Python:     {sys.version.split()[0]}\n")

    if sys.platform != "win32":
        print("Ce script exige Windows. Sur une autre plateforme, utilisez le mode DXF:")
        print("    uv run python examples/premier_plan.py")
        return 2

    from autocad_mcp.backends.acad_com import AcadComBackend
    from autocad_mcp.errors import CadError

    backend = AcadComBackend()

    try:
        backend.connect()
    except CadError as exc:
        print(f"ÉCHEC de connexion [{exc.code}]: {exc.message}")
        if exc.details:
            print(json.dumps(exc.details, indent=2, default=str))
        print("\nVérifiez: AutoCAD complet (pas LT) lancé, avec un dessin ouvert,")
        print("et `uv sync --extra windows` passé.")
        return 1

    print("Connexion établie.\n")
    infos = backend.diagnostics()

    suspectes = [
        nom
        for nom, resolu in infos.get("constants_differ_from_fallback", {}).items()
        if resolu
    ]
    print(f"AutoCAD:    {infos.get('autocad_version')}")
    print(f"Constantes: {len(infos.get('constants', {}))} vérifiées")

    if suspectes:
        print(f"\n{len(suspectes)} constante(s) DIFFÈRENT de la valeur supposée:")
        for nom in suspectes:
            print(f"  {nom} = {infos['constants'][nom]}")
        print("\nCe sont des hypothèses fausses. Reportez les vraies valeurs")
        print("dans _FALLBACK_CONSTANTS de backends/acad_com.py.")
    else:
        print("Aucun écart: toutes les valeurs supposées sont confirmées.")

    print("\nLecture du document, sans rien y écrire:")
    try:
        doc = backend.document_info()
        print(f"  entités: {doc.get('entity_count')}  calques: {len(doc.get('layers', []))}")
        print(f"  unité:   {doc.get('unit')}")
    except CadError as exc:
        print(f"  ÉCHEC [{exc.code}]: {exc.message}")
        return 1
    finally:
        backend.close()

    print("\nDétail complet:")
    print(json.dumps(infos, indent=2, default=str, ensure_ascii=False))
    print("\nLecture confirmée. Passez aux phases suivantes de")
    print("docs/windows-checklist.md, qui exercent l'écriture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
