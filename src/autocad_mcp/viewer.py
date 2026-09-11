"""Export d'un modèle vers une scène lisible par un navigateur.

Un DXF en volumes ne se regarde que dans un logiciel de CAO. Ce module réduit
les mêmes opérations à des sommets, des facettes et des calques, c'est-à-dire
à ce qu'un moteur 3D du web attend, pour qu'un plan se montre à quelqu'un qui
n'a ni AutoCAD ni licence.

Rien n'est recalculé ici: la géométrie vient des opérations produites par la
couche métier, et seules les couleurs sont traduites, de l'index ACI vers la
notation du web.
"""

from __future__ import annotations

from typing import Any

from .model.ops import BYLAYER, AddMesh, EnsureLayer, Operation

__all__ = ["ACI_RGB", "scene_from_document", "to_scene"]

#: Teintes des index ACI utilisés par le projet, en notation du web.
#: L'index 7 s'affiche noir sur fond clair et blanc sur fond sombre: la
#: visionneuse le résout selon son propre fond, d'où la valeur neutre ici.
ACI_RGB: dict[int, str] = {
    0: "#9a958a",
    1: "#d4483b",
    2: "#d9b036",
    3: "#4f9d5d",
    4: "#3f9aa8",
    5: "#3f6fa8",
    6: "#a05398",
    7: "#b8b2a4",
    8: "#7d7a73",
    9: "#a6a29a",
    BYLAYER: "#b8b2a4",
}


def _rgb(index: int) -> str:
    return ACI_RGB.get(index, ACI_RGB[7])


def to_scene(
    operations: list[Operation], *, unit: str = "m", name: str = "plan"
) -> dict[str, Any]:
    """Réduit des opérations à une scène de facettes.

    Seuls les maillages sont retenus: une scène 3D n'a que faire des traits de
    plan, qui décrivent la même chose en deux dimensions. Les calques sont
    rapportés à part avec leur teinte, pour que la visionneuse puisse les
    éteindre un par un.

    Rend un objet prêt à sérialiser en JSON, aux longueurs exprimées dans
    l'unité du document.
    """
    calques: dict[str, str] = {}
    for operation in operations:
        if isinstance(operation, EnsureLayer):
            calques[operation.name] = _rgb(operation.color)

    volumes: list[dict[str, Any]] = []
    mini = [float("inf")] * 3
    maxi = [float("-inf")] * 3

    for operation in operations:
        if not isinstance(operation, AddMesh):
            continue
        sommets = [list(v) for v in operation.vertices]
        for sommet in sommets:
            for axe in range(3):
                mini[axe] = min(mini[axe], sommet[axe])
                maxi[axe] = max(maxi[axe], sommet[axe])
        volumes.append(
            {
                "layer": operation.style.layer,
                "vertices": sommets,
                "faces": [list(f) for f in operation.faces],
            }
        )

    if not volumes:
        mini = maxi = [0.0, 0.0, 0.0]
        calques.setdefault("WALLS", ACI_RGB[7])

    for nom in {v["layer"] for v in volumes}:
        calques.setdefault(nom, ACI_RGB[7])

    return {
        "name": name,
        "unit": unit,
        "layers": [{"name": n, "color": c} for n, c in sorted(calques.items())],
        "meshes": volumes,
        "bounds": {"min": mini, "max": maxi},
        "counts": {
            "meshes": len(volumes),
            "faces": sum(len(v["faces"]) for v in volumes),
        },
    }


def scene_from_document(doc: Any, *, name: str = "plan") -> dict[str, Any]:
    """Relit les volumes d'un document déjà écrit.

    ``to_scene`` part des opérations, ce qui suppose de les avoir sous la main.
    Ici on lit le document lui-même, donc un dessin rouvert ou construit en
    plusieurs lots se montre aussi bien qu'un dessin fraîchement produit.

    Les maillages du format DXF portent leurs sommets et leurs facettes dans
    des structures distinctes; seul le comptage des sommets par facette permet
    de les recomposer.
    """
    from .units import INSUNITS, Unit

    calques: dict[str, str] = {}
    for calque in doc.layers:
        try:
            calques[str(calque.dxf.name)] = _rgb(int(calque.dxf.color))
        except (AttributeError, ValueError, TypeError):
            calques[str(calque.dxf.name)] = ACI_RGB[7]

    volumes: list[dict[str, Any]] = []
    mini = [float("inf")] * 3
    maxi = [float("-inf")] * 3

    for entite in doc.modelspace().query("MESH"):
        sommets = [[float(c) for c in v] for v in entite.vertices]
        if len(sommets) < 3:
            continue
        facettes = [[int(i) for i in f] for f in entite.faces]
        if not facettes:
            continue
        for sommet in sommets:
            for axe in range(3):
                mini[axe] = min(mini[axe], sommet[axe])
                maxi[axe] = max(maxi[axe], sommet[axe])
        volumes.append(
            {
                "layer": str(entite.dxf.layer),
                "vertices": sommets,
                "faces": facettes,
            }
        )

    if not volumes:
        mini = maxi = [0.0, 0.0, 0.0]

    code = int(doc.header.get("$INSUNITS", 6) or 6)
    unite = next((u.value for u, c in INSUNITS.items() if c == code), Unit.METER.value)

    employes = {v["layer"] for v in volumes}
    return {
        "name": name,
        "unit": unite,
        "layers": [
            {"name": n, "color": c} for n, c in sorted(calques.items()) if n in employes
        ]
        or [{"name": "0", "color": ACI_RGB[7]}],
        "meshes": volumes,
        "bounds": {"min": mini, "max": maxi},
        "counts": {
            "meshes": len(volumes),
            "faces": sum(len(v["faces"]) for v in volumes),
        },
    }
