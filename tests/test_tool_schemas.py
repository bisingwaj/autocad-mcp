"""Vérification du catalogue d'outils MCP et de son exécution.

Ce fichier couvre deux choses qui ne se relisent pas à l'œil.

* **La forme du catalogue.** Unicité des noms en tête, parce que l'ancien
  catalogue déclarait ``delete_entities_by_color`` deux fois et que la seconde
  déclaration masquait la première sans bruit. Puis la complétude des schémas:
  chaque paramètre décrit, chaque objet clos, chaque angle en degrés.
* **La vérité des réponses.** Un échec doit ressortir comme un échec, un lot
  partiellement exécuté doit rapporter les deux côtés, et l'index d'un élément
  fautif doit désigner la liste fournie par le modèle, pas la liste aplatie des
  opérations internes.
"""

from __future__ import annotations

import json
import math
from typing import Any

import anyio
import pytest

from autocad_mcp.backends.ezdxf_be import EzdxfBackend
from autocad_mcp.backends.recording import RecordingBackend
from autocad_mcp.config import Config
from autocad_mcp.errors import ConfirmationRequired, InvalidParameter, UnsupportedOperation
from autocad_mcp.tools import handlers as handlers_module
from autocad_mcp.tools.handlers import ToolHandlers
from autocad_mcp.tools.schemas import MAX_QUERY_LIMIT, ToolSpec, build_catalog, tool_names
from autocad_mcp.units import Unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config(backend="ezdxf", unit=Unit.METER, query_limit=25)


@pytest.fixture
def catalog(config: Config) -> tuple[ToolSpec, ...]:
    return build_catalog(config)


@pytest.fixture
def backend() -> EzdxfBackend:
    return EzdxfBackend(unit=Unit.METER)


@pytest.fixture
def tools(config: Config, backend: EzdxfBackend) -> ToolHandlers:
    return ToolHandlers(config, backend)


def call(tools: ToolHandlers, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Exécute un appel d'outil depuis un test synchrone."""
    return anyio.run(tools.call, name, arguments)


def _walk(schema: Any) -> list[dict[str, Any]]:
    """Tous les sous-schémas d'un schéma, y compris les branches d'union."""
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        found.append(schema)
        for key in ("properties", "items", "oneOf", "anyOf", "allOf"):
            value = schema.get(key)
            if isinstance(value, dict):
                for sub in (value.values() if key == "properties" else [value]):
                    found.extend(_walk(sub))
            elif isinstance(value, list):
                for sub in value:
                    found.extend(_walk(sub))
    return found


# ---------------------------------------------------------------------------
# Forme du catalogue
# ---------------------------------------------------------------------------


def test_tool_names_are_unique(catalog: tuple[ToolSpec, ...]) -> None:
    """Le test qui manquait: deux outils du même nom, l'un masque l'autre.

    C'est arrivé: ``delete_entities_by_color`` figurait deux fois dans le
    catalogue historique. Un client MCP garde la dernière déclaration, le modèle
    appelle en croyant viser la première, et rien ne signale l'écart.
    """
    names = [spec.name for spec in catalog]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"noms d'outils déclarés plusieurs fois: {duplicates}"
    assert len(set(names)) == len(names)


def test_tool_names_helper_agrees_with_catalog(config: Config) -> None:
    assert tool_names(config) == tuple(spec.name for spec in build_catalog(config))


def test_catalog_covers_the_expected_capabilities(catalog: tuple[ToolSpec, ...]) -> None:
    """Peu d'outils, mais tous les gestes couverts.

    Le catalogue reste une liste qu'on lit d'un regard: lire, mesurer, vérifier,
    voir, écrire, éditer, et une passerelle. Chaque ajout doit se justifier par
    un geste que rien ne couvrait: ``measure`` remplace à lui seul cinq outils
    d'interrogation, et ``place_blocks`` fait pour la bibliothèque ce que
    ``build_structure`` fait pour les murs.
    """
    assert {spec.name for spec in catalog} == {
        "get_drawing_info",
        "query_entities",
        "measure",
        "check_plan",
        "render_view",
        "draw",
        "build_structure",
        "place_blocks",
        "delete_entities",
        "set_entity_color",
        "undo_last_batch",
        "run_cad_command",
    }


def test_every_tool_has_a_handler(config: Config, catalog: tuple[ToolSpec, ...]) -> None:
    """Un outil annoncé sans route est une promesse non tenue, et l'inverse aussi."""
    router = ToolHandlers(config, RecordingBackend(unit=Unit.METER))
    assert set(router.handled) == {spec.name for spec in catalog}


def test_schemas_are_closed_objects(catalog: tuple[ToolSpec, ...]) -> None:
    """``additionalProperties`` à false partout: un champ mal nommé doit être refusé."""
    for spec in catalog:
        for node in _walk(spec.input_schema):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, (
                    f"{spec.name}: objet ouvert {node.get('title', node)}"
                )


def test_every_parameter_is_documented(catalog: tuple[ToolSpec, ...]) -> None:
    """Type et description obligatoires. Un paramètre sans description est un piège."""
    for spec in catalog:
        for node in _walk(spec.input_schema):
            for name, prop in (node.get("properties") or {}).items():
                assert "type" in prop or "const" in prop or "enum" in prop, (
                    f"{spec.name}.{name} n'a pas de type"
                )
                assert prop.get("description", "").strip(), (
                    f"{spec.name}.{name} n'a pas de description"
                )


def test_required_parameters_exist(catalog: tuple[ToolSpec, ...]) -> None:
    for spec in catalog:
        for node in _walk(spec.input_schema):
            declared = set(node.get("properties") or {})
            for name in node.get("required") or ():
                assert name in declared, f"{spec.name}: requis inconnu {name}"


def test_lengths_declare_their_unit(catalog: tuple[ToolSpec, ...]) -> None:
    """Une longueur sans unité est un bug: invisible dans un dessin en millimètres."""
    lengths = {"radius", "thickness", "height", "width", "scale"}
    for spec in catalog:
        for node in _walk(spec.input_schema):
            for name, prop in (node.get("properties") or {}).items():
                if name in lengths and prop.get("type") == "number":
                    text = prop["description"].lower()
                    assert "unité" in text or "échelle" in text, (
                        f"{spec.name}.{name} ne dit pas son unité"
                    )


def test_angles_are_declared_in_degrees(catalog: tuple[ToolSpec, ...]) -> None:
    """Le modèle parle degrés, le modèle interne parle radians.

    La frontière est ici: tout champ angulaire du schéma porte le suffixe
    ``_deg`` et le dit dans sa description.
    """
    seen = 0
    for spec in catalog:
        for node in _walk(spec.input_schema):
            for name, prop in (node.get("properties") or {}).items():
                if "angle" in name or "rotation" in name:
                    seen += 1
                    assert name.endswith("_deg"), f"{spec.name}.{name} ne dit pas son unité"
                    assert "degré" in prop["description"].lower()
    assert seen >= 5, "les champs angulaires ont disparu du catalogue"


def test_descriptions_say_when_to_use(catalog: tuple[ToolSpec, ...]) -> None:
    """Une description qui dit seulement ce que fait l'outil ne suffit pas."""
    for spec in catalog:
        assert len(spec.description) > 150, f"{spec.name}: description trop courte"
        assert "à utiliser" in spec.description.lower() or "à appeler" in spec.description.lower(), (
            f"{spec.name} ne dit pas QUAND l'utiliser"
        )


def test_destructive_tools_are_explicit(catalog: tuple[ToolSpec, ...]) -> None:
    """Une opération irréversible l'annonce, et exige une confirmation."""
    by_name = {spec.name: spec for spec in catalog}

    delete = by_name["delete_entities"]
    assert delete.destructive is True
    assert "irréversible" in delete.description.lower()
    assert "confirm_delete_all" in delete.input_schema["properties"]

    undo = by_name["undo_last_batch"]
    assert undo.destructive is True
    assert "ne sont pas restaurées" in undo.description


def test_read_only_tools_are_flagged(catalog: tuple[ToolSpec, ...]) -> None:
    by_name = {spec.name: spec for spec in catalog}
    for name in ("get_drawing_info", "query_entities", "render_view"):
        assert by_name[name].read_only is True
    for name in ("draw", "build_structure", "delete_entities", "set_entity_color"):
        assert by_name[name].read_only is False


def test_query_limit_is_bounded(config: Config, catalog: tuple[ToolSpec, ...]) -> None:
    """L'inspection ne peut pas déverser un dessin entier."""
    schema = next(spec for spec in catalog if spec.name == "query_entities").input_schema
    limit = schema["properties"]["limit"]
    assert limit["default"] == config.query_limit
    assert limit["maximum"] == MAX_QUERY_LIMIT
    assert limit["minimum"] == 1


def test_batch_tools_take_a_list(catalog: tuple[ToolSpec, ...]) -> None:
    """Un outil de lot vaut mieux que vingt appels successifs."""
    by_name = {spec.name: spec for spec in catalog}
    for name, field in (("draw", "operations"), ("build_structure", "elements")):
        prop = by_name[name].input_schema["properties"][field]
        assert prop["type"] == "array"
        assert prop["minItems"] == 1
        assert prop["maxItems"] >= 100
        assert len(prop["items"]["oneOf"]) >= 7


def test_schemas_are_json_serialisable(catalog: tuple[ToolSpec, ...]) -> None:
    for spec in catalog:
        json.loads(json.dumps(spec.input_schema))


def test_schemas_are_valid_json_schema(catalog: tuple[ToolSpec, ...]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for spec in catalog:
        jsonschema.Draft202012Validator.check_schema(spec.input_schema)


def test_draw_payload_validates_against_its_schema(catalog: tuple[ToolSpec, ...]) -> None:
    """Un appel réaliste doit passer le schéma, sinon le schéma est faux."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = next(spec for spec in catalog if spec.name == "draw").input_schema
    jsonschema.validate(
        {
            "operations": [
                {"op": "rectangle", "corner1": [0, 0], "corner2": [4, 3], "layer": "WALLS"},
                {"op": "circle", "center": [2, 1.5], "radius": 0.5, "color": "red"},
                {"op": "arc", "center": [0, 0], "radius": 1, "start_angle_deg": 0,
                 "end_angle_deg": 90},
                {"op": "text", "position": [2, 1.5], "content": "OK", "halign": "center"},
            ],
            "label": "essai",
        },
        schema,
    )


def test_unknown_field_is_refused_by_the_schema(catalog: tuple[ToolSpec, ...]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = next(spec for spec in catalog if spec.name == "draw").input_schema
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"operations": [{"op": "circle", "center": [0, 0], "radius": 1, "colour": "red"}]},
            schema,
        )


# ---------------------------------------------------------------------------
# Routage et vérité des réponses
# ---------------------------------------------------------------------------


def test_unknown_tool_is_an_error_not_a_success(tools: ToolHandlers) -> None:
    result = call(tools, "create_line", {})
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code
    assert "create_line" in result.payload["error"]
    assert "draw" in result.payload["details"]["supported"]


def test_backend_is_not_connected_before_the_first_call(config: Config) -> None:
    """Démarrer sans AutoCAD ouvert doit rester possible.

    Les handlers n'instancient rien tant qu'aucun outil n'est appelé; le
    catalogue, lui, est disponible immédiatement.
    """
    router = ToolHandlers(config)
    assert router.connected is False
    assert len(router.catalog) == len(build_catalog(config))


def test_get_drawing_info_reports_unit_and_layers(tools: ToolHandlers) -> None:
    result = call(tools, "get_drawing_info")
    assert result.is_error is False
    assert result.payload["unit"] == "m"
    assert result.payload["defaults"]["wall_thickness"] == pytest.approx(0.20)
    assert result.payload["limits"]["query_limit_max"] == MAX_QUERY_LIMIT
    assert result.payload["can_render"] is True


def test_draw_executes_a_whole_batch_at_once(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    result = call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "rectangle", "corner1": [0, 0], "corner2": [4, 3]},
                {"op": "circle", "center": [2, 1.5], "radius": 0.5, "color": "red"},
                {"op": "line", "start": [0, 0], "end": [4, 3]},
            ],
            "label": "essai",
        },
    )
    assert result.is_error is False
    assert result.payload["ok"] is True
    assert result.payload["created_count"] == 3
    assert len(result.payload["handles"]) == 3
    assert result.payload["requested"] == 3
    # Les handles annoncés existent vraiment: aucun n'est fabriqué.
    for handle in result.payload["handles"]:
        assert backend.document.entitydb.get(handle) is not None


def test_rectangle_is_one_closed_polyline(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    """Régression historique: quatre segments indépendants, donc non hachurables."""
    call(tools, "draw", {"operations": [{"op": "rectangle", "corner1": [0, 0], "corner2": [4, 3]}]})
    entities = list(backend.document.modelspace())
    assert [e.dxftype() for e in entities] == ["LWPOLYLINE"]
    assert entities[0].closed is True
    assert len(entities[0]) == 4


def test_degrees_reach_the_document_as_degrees(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    """Degrés au schéma, radians dans le modèle, degrés dans le DXF.

    Une double conversion donnerait ici 1,57 degré au lieu de 90.
    """
    call(
        tools,
        "draw",
        {
            "operations": [
                {
                    "op": "arc",
                    "center": [0, 0],
                    "radius": 1,
                    "start_angle_deg": 0,
                    "end_angle_deg": 90,
                },
                {"op": "text", "position": [0, 0], "content": "T", "rotation_deg": 45},
            ]
        },
    )
    arc = backend.document.modelspace().query("ARC")[0]
    assert arc.dxf.start_angle == pytest.approx(0.0)
    assert arc.dxf.end_angle == pytest.approx(90.0)
    text = backend.document.modelspace().query("TEXT")[0]
    assert text.dxf.rotation == pytest.approx(45.0)


def test_label_rotation_is_converted_from_degrees(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    """``architecture.label`` attend des radians: la conversion est faite côté outil."""
    call(
        tools,
        "build_structure",
        {"elements": [{"element": "label", "position": [0, 0], "text": "N", "rotation_deg": 90}]},
    )
    text = backend.document.modelspace().query("TEXT")[0]
    assert text.dxf.rotation == pytest.approx(90.0)
    assert math.isclose(math.radians(90.0), math.pi / 2)


def test_partial_batch_reports_both_sides(tools: ToolHandlers) -> None:
    """Le bug historique: ``success: true`` alors que rien n'était dessiné.

    Un lot partiel rapporte ce qui existe ET ce qui a échoué, et l'index pointe
    l'élément fourni par le modèle, pas une opération interne.
    """
    result = call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [1, 0]},
                {"op": "circle", "center": [0, 0], "radius": -3},
                {"op": "line", "start": [1, 0], "end": [1, 1]},
            ]
        },
    )
    assert result.is_error is True
    assert result.payload["ok"] is False
    assert result.payload["created_count"] == 2
    assert result.payload["failed_count"] == 1
    failure = result.payload["failures"][0]
    assert failure["index"] == 1
    assert failure["code"] == "invalid_geometry"
    assert failure["stage"] == "execute"


def test_malformed_element_is_reported_before_execution(tools: ToolHandlers) -> None:
    """Un élément refusé à la construction ne perd pas les autres."""
    result = call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [1, 0]},
                {"op": "polyline", "points": [[0, 0]]},
            ]
        },
    )
    assert result.is_error is True
    assert result.payload["created_count"] == 1
    failure = result.payload["failures"][0]
    assert failure["index"] == 1
    assert failure["stage"] == "build"
    assert failure["code"] == InvalidParameter.code


def test_batch_without_any_usable_element_is_an_error(tools: ToolHandlers) -> None:
    result = call(tools, "draw", {"operations": [{"op": "circle", "center": [0, 0]}]})
    assert result.is_error is True
    assert result.payload["created_count"] == 0
    assert result.payload["code"] == InvalidParameter.code


def test_oversized_batch_is_refused(tools: ToolHandlers) -> None:
    result = call(
        tools,
        "draw",
        {"operations": [{"op": "line", "start": [0, 0], "end": [1, 1]}] * 501},
    )
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code


def test_build_structure_uses_normalised_layers(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    result = call(
        tools,
        "build_structure",
        {
            "elements": [
                {"element": "room", "corner1": [0, 0], "corner2": [4, 3], "name": "SEJOUR"},
                {
                    "element": "door_in_wall",
                    "wall_start": [0, 0],
                    "wall_end": [4, 0],
                    "position": 0.5,
                },
                {"element": "window", "start": [0, 3], "end": [1.5, 3]},
            ],
            "label": "plan",
        },
    )
    assert result.is_error is False
    layers = {entity.dxf.layer for entity in backend.document.modelspace()}
    assert {"WALLS", "DOORS", "WINDOWS", "ANNOTATION"} <= layers
    assert "WALLS" in result.payload["layers"]


def test_query_entities_is_always_bounded(tools: ToolHandlers) -> None:
    call(
        tools,
        "draw",
        {"operations": [{"op": "line", "start": [0, i], "end": [1, i]} for i in range(30)]},
    )
    result = call(tools, "query_entities", {"limit": 5})
    assert result.payload["total"] == 30
    assert result.payload["returned"] == 5
    assert result.payload["truncated"] is True
    assert "hint" in result.payload
    assert len(result.payload["entities"]) == 5


def test_query_limit_cannot_exceed_the_hard_ceiling(tools: ToolHandlers) -> None:
    call(tools, "draw", {"operations": [{"op": "line", "start": [0, 0], "end": [1, 1]}]})
    result = call(tools, "query_entities", {"limit": 10_000})
    assert result.payload["limit"] == MAX_QUERY_LIMIT


def test_query_entities_filters_by_layer(tools: ToolHandlers) -> None:
    call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [1, 0], "layer": "WALLS"},
                {"op": "line", "start": [0, 1], "end": [1, 1], "layer": "SITE"},
            ]
        },
    )
    result = call(tools, "query_entities", {"layer": "WALLS"})
    assert result.payload["total"] == 1
    assert result.payload["entities"][0]["layer"] == "WALLS"
    assert result.payload["filter"] == {"layer": "WALLS"}


def test_delete_without_filter_demands_confirmation(tools: ToolHandlers) -> None:
    """Sans garde-fou, un filtre vide effacerait le dessin entier."""
    call(tools, "draw", {"operations": [{"op": "line", "start": [0, 0], "end": [1, 1]}]})
    result = call(tools, "delete_entities", {})
    assert result.is_error is True
    assert result.payload["code"] == ConfirmationRequired.code
    assert "confirm_delete_all" in result.payload["details"]["remedy"]


def test_confirmed_delete_all_clears_the_drawing(tools: ToolHandlers) -> None:
    call(
        tools,
        "draw",
        {"operations": [{"op": "line", "start": [0, i], "end": [1, i]} for i in range(4)]},
    )
    result = call(tools, "delete_entities", {"confirm_delete_all": True})
    assert result.is_error is False
    assert result.payload["deleted_count"] == 4
    assert result.payload["irreversible"] is True
    assert call(tools, "query_entities", {}).payload["total"] == 0


def test_delete_by_handles_touches_only_those(tools: ToolHandlers) -> None:
    created = call(
        tools,
        "draw",
        {"operations": [{"op": "line", "start": [0, i], "end": [1, i]} for i in range(3)]},
    ).payload["handles"]
    result = call(tools, "delete_entities", {"handles": created[:2]})
    assert result.payload["deleted_count"] == 2
    assert call(tools, "query_entities", {}).payload["total"] == 1


def test_set_entity_color_reports_unknown_handles(tools: ToolHandlers) -> None:
    handles = call(
        tools, "draw", {"operations": [{"op": "line", "start": [0, 0], "end": [1, 1]}]}
    ).payload["handles"]
    result = call(tools, "set_entity_color", {"handles": [*handles, "DEAD"], "color": "red"})
    assert result.is_error is True
    assert result.payload["updated_count"] == 1
    assert result.payload["failures"][0]["handle"] == "DEAD"
    assert result.payload["failures"][0]["code"] == "entity_not_found"
    assert result.payload["color"] == 1


def test_black_is_aci_seven_not_byblock(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    """Régression historique: ``black`` valait 0, qui est ByBlock, pas noir."""
    handles = call(
        tools, "draw", {"operations": [{"op": "line", "start": [0, 0], "end": [1, 1]}]}
    ).payload["handles"]
    result = call(tools, "set_entity_color", {"handles": handles, "color": "black"})
    assert result.payload["color"] == 7
    assert backend.document.entitydb.get(handles[0]).dxf.color == 7


def test_undo_removes_only_the_last_batch(tools: ToolHandlers) -> None:
    call(tools, "draw", {"operations": [{"op": "line", "start": [0, 0], "end": [1, 0]}]})
    call(tools, "draw", {"operations": [{"op": "line", "start": [0, 1], "end": [1, 1]}]})
    result = call(tools, "undo_last_batch")
    assert result.is_error is False
    assert result.payload["entity_count"] == 1


def test_undo_without_a_batch_is_an_error(tools: ToolHandlers) -> None:
    result = call(tools, "undo_last_batch")
    assert result.is_error is True
    assert result.payload["code"] == "operation_failed"


def test_render_view_returns_an_image(tools: ToolHandlers) -> None:
    """Sans image, le modèle dessine à l'aveugle et croit son propre compte rendu."""
    pytest.importorskip("matplotlib")
    call(
        tools,
        "build_structure",
        {"elements": [{"element": "room", "corner1": [0, 0], "corner2": [4, 3], "name": "SALLE"}]},
    )
    result = call(tools, "render_view", {"width": 400, "height": 300})
    assert result.is_error is False
    assert result.image_png is not None
    assert result.image_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.payload["entity_count"] > 0
    assert result.payload["width"] == 400


def test_render_view_refuses_absurd_sizes(tools: ToolHandlers) -> None:
    result = call(tools, "render_view", {"width": 10})
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code


def test_render_view_says_so_when_the_backend_cannot(config: Config) -> None:
    """Le backend AutoCAD ne rend pas d'image: l'écart est dit, pas masqué."""
    router = ToolHandlers(config, RecordingBackend(unit=Unit.METER))
    result = call(router, "render_view", {})
    assert result.is_error is True
    assert result.payload["code"] == UnsupportedOperation.code


def test_document_unit_wins_over_configuration(backend: EzdxfBackend) -> None:
    """Un mur de vingt centimètres reste vingt centimètres, quelle que soit l'unité."""
    millimetres = EzdxfBackend(unit=Unit.MILLIMETER)
    router = ToolHandlers(Config(backend="ezdxf", unit=Unit.METER), millimetres)
    call(router, "build_structure", {"elements": [{"element": "wall", "start": [0, 0],
                                                   "end": [5000, 0]}]})
    outline = millimetres.document.modelspace().query("LWPOLYLINE")[0]
    ys = sorted({round(point[1], 6) for point in outline.get_points("xy")})
    assert ys[-1] - ys[0] == pytest.approx(200.0)  # 0,20 m exprimés en millimètres


def test_autosave_writes_the_file_when_a_path_is_configured(tmp_path: Any) -> None:
    """Sans écriture, un dessin produit hors AutoCAD ne quitterait jamais la mémoire."""
    target = tmp_path / "plan.dxf"
    settings = Config(backend="ezdxf", unit=Unit.METER, dxf_path=str(target))
    router = ToolHandlers(settings, EzdxfBackend(path=target, unit=Unit.METER))
    result = call(router, "draw", {"operations": [{"op": "line", "start": [0, 0], "end": [1, 1]}]})
    assert result.payload["saved"] is True
    assert target.exists()


def test_handlers_never_raise(tools: ToolHandlers, monkeypatch: pytest.MonkeyPatch) -> None:
    """Une exception non typée devient un échec nommé, jamais un succès."""

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("le moteur a rendu l'âme")

    monkeypatch.setattr(handlers_module.ToolHandlers, "_ready", boom)
    result = call(tools, "get_drawing_info")
    assert result.is_error is True
    assert result.payload["code"] == "operation_failed"
    assert "RuntimeError" in result.payload["error"]
