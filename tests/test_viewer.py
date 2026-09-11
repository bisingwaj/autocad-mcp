"""Export vers une scène web.

Un DXF en volumes ne se regarde que dans un logiciel de CAO. La scène est ce
qui permet de montrer un projet à quelqu'un qui n'a ni AutoCAD ni licence, donc
elle doit rester fidèle au modèle et sérialisable sans perte.
"""

from __future__ import annotations

import json

import pytest

from autocad_mcp.ops.architecture import Opening
from autocad_mcp.ops.volume import box, slab, wall_volume
from autocad_mcp.units import Defaults
from autocad_mcp.viewer import ACI_RGB, to_scene

CONTOUR = [(0.0, 0.0), (8.0, 0.0), (8.0, 5.0), (0.0, 5.0)]


def plan(defaults: Defaults) -> list:  # type: ignore[no-untyped-def]
    ops = slab(CONTOUR, defaults)
    ops += wall_volume(
        CONTOUR, defaults, thickness=0.2, closed=True,
        openings=[Opening(segment=0, position=0.5, width=1.4, kind="window")],
    )
    ops += box((1.0, 1.0), (2.6, 2.0), height=0.75, layer="furniture")
    return ops


def test_la_scene_ne_retient_que_les_volumes(defaults: Defaults) -> None:
    """Une scène 3D n'a que faire des traits de plan.

    Ils décrivent la même chose en deux dimensions, et les embarquer
    alourdirait la page sans rien montrer de plus.
    """
    from autocad_mcp.ops.architecture import wall_network

    ops = list(plan(defaults)) + wall_network(CONTOUR, defaults, closed=True)
    scene = to_scene(ops)
    assert scene["counts"]["meshes"] == len(
        [op for op in ops if op.kind == "mesh"]
    )


def test_chaque_calque_porte_une_teinte(defaults: Defaults) -> None:
    scene = to_scene(plan(defaults))
    noms = {c["name"] for c in scene["layers"]}
    assert {"WALLS", "STRUCTURE", "FURNITURE"} <= noms
    for calque in scene["layers"]:
        assert calque["color"].startswith("#")
        assert len(calque["color"]) == 7


def test_tout_volume_a_son_calque_declare(defaults: Defaults) -> None:
    """La visionneuse éteint les calques un par un: aucun ne doit manquer."""
    scene = to_scene(plan(defaults))
    declares = {c["name"] for c in scene["layers"]}
    employes = {v["layer"] for v in scene["meshes"]}
    assert employes <= declares


def test_les_facettes_designent_des_sommets_existants(defaults: Defaults) -> None:
    """Un indice hors bornes ferait une page blanche, pas un message."""
    for volume in to_scene(plan(defaults))["meshes"]:
        borne = len(volume["vertices"])
        for facette in volume["faces"]:
            assert all(0 <= i < borne for i in facette)


def test_l_emprise_englobe_tous_les_sommets(defaults: Defaults) -> None:
    """C'est elle qui cadre la caméra: fausse, le bâtiment sort de l'écran."""
    scene = to_scene(plan(defaults))
    mini, maxi = scene["bounds"]["min"], scene["bounds"]["max"]
    for volume in scene["meshes"]:
        for sommet in volume["vertices"]:
            for axe in range(3):
                assert mini[axe] <= sommet[axe] <= maxi[axe]


def test_la_dalle_descend_sous_le_niveau_zero(defaults: Defaults) -> None:
    scene = to_scene(plan(defaults))
    assert scene["bounds"]["min"][2] == pytest.approx(-defaults.slab_thickness)


def test_la_scene_se_serialise_en_json(defaults: Defaults) -> None:
    """Elle est embarquée dans une page: tout doit passer en JSON."""
    scene = to_scene(plan(defaults), unit="m", name="Essai")
    texte = json.dumps(scene)
    relu = json.loads(texte)
    assert relu["name"] == "Essai"
    assert relu["unit"] == "m"
    assert relu["counts"]["faces"] == scene["counts"]["faces"]


def test_une_scene_vide_reste_utilisable(defaults: Defaults) -> None:
    """Une page qui reçoit une scène vide doit s'ouvrir, pas planter."""
    scene = to_scene([])
    assert scene["meshes"] == []
    assert scene["layers"]
    assert scene["bounds"]["min"] == scene["bounds"]["max"]


def test_les_teintes_couvrent_les_index_du_projet() -> None:
    from autocad_mcp.model.layers import STANDARD_LAYERS

    for spec in STANDARD_LAYERS.values():
        assert spec.color in ACI_RGB, f"calque {spec.name}: index {spec.color} sans teinte"
