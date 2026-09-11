"""Les outils ajoutés après la refonte: murs percés, blocs, mesure, validation.

Ce fichier prolonge ``test_tool_schemas.py`` sur les quatre capacités qui
manquaient au catalogue, et il vérifie la même chose qu'ailleurs: **que la
réponse dit la vérité**.

* ``build_structure`` avec l'élément ``wall_network``: la baie doit réellement
  couper le mur, pas s'y superposer, et un rang de mur inexistant doit être
  refusé au lieu d'être ignoré.
* ``place_blocks``: l'occurrence doit exister, la définition doit être créée
  sans qu'on la demande, et le catalogue annoncé dans le schéma doit être celui
  de ``ops.blocks``, pas une copie qui divergera.
* ``measure``: une mesure que le moteur ne publie pas doit revenir **nulle**,
  au sens de « inconnue », et surtout pas zéro. C'est la seule façon de ne pas
  faire recopier un faux métré.
* ``check_plan``: trouver des défauts n'est pas un échec d'appel, et chaque
  défaut doit porter de quoi se corriger.
* ``run_cad_command``: une commande hors liste blanche est refusée, et le
  backend DXF dit qu'il n'a pas d'interpréteur au lieu de faire semblant.
"""

from __future__ import annotations

import math
from typing import Any

import anyio
import pytest

from autocad_mcp.backends.base import EntityInfo
from autocad_mcp.backends.ezdxf_be import EzdxfBackend
from autocad_mcp.backends.recording import RecordingBackend
from autocad_mcp.config import Config
from autocad_mcp.errors import ConfirmationRequired, InvalidParameter, UnsupportedOperation
from autocad_mcp.ops import blocks
from autocad_mcp.tools.handlers import ToolHandlers
from autocad_mcp.tools.schemas import (
    MAX_CHECK_ITEMS,
    MEASURE_MODES,
    ToolSpec,
    build_catalog,
)
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
    return anyio.run(tools.call, name, arguments)


def spec_of(catalog: tuple[ToolSpec, ...], name: str) -> ToolSpec:
    return next(spec for spec in catalog if spec.name == name)


def layers_of(backend: EzdxfBackend) -> set[str]:
    return {entity.dxf.layer for entity in backend.document.modelspace()}


def types_of(backend: EzdxfBackend) -> list[str]:
    return [entity.dxftype() for entity in backend.document.modelspace()]


# ---------------------------------------------------------------------------
# wall_network: les murs se raccordent et les baies percent
# ---------------------------------------------------------------------------


def test_wall_network_without_openings_is_two_closed_rings(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    """Une enceinte pleine, c'est deux anneaux: l'extérieur et l'intérieur.

    Quatre rectangles indépendants donneraient huit polylignes et un
    recouvrement à chaque coin.
    """
    result = call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0], [6, 4], [0, 4]],
                    "closed": True,
                    "thickness": 0.2,
                }
            ]
        },
    )
    assert result.is_error is False
    assert types_of(backend) == ["LWPOLYLINE", "LWPOLYLINE"]
    assert all(entity.closed for entity in backend.document.modelspace())


def test_an_opening_really_cuts_the_wall(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    """La régression visible au rendu: un symbole posé sur un mur resté plein.

    Le mur percé rend deux tronçons de maçonnerie, plus le battant et son arc.
    """
    result = call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0]],
                    "thickness": 0.2,
                    "openings": [
                        {"segment": 0, "position": 0.5, "width": 0.9, "kind": "door"}
                    ],
                }
            ]
        },
    )
    assert result.is_error is False
    walls = [e for e in backend.document.modelspace() if e.dxf.layer == "WALLS"]
    assert len(walls) == 2, "la baie doit couper le mur en deux tronçons"
    assert {"WALLS", "DOORS"} <= layers_of(backend)

    # Aucun tronçon ne couvre la baie: ils s'arrêtent à ses bords.
    xs = sorted(x for wall in walls for x, _ in wall.get_points("xy"))
    assert xs[0] == pytest.approx(0.0)
    assert xs[-1] == pytest.approx(6.0)
    interieurs = sorted({round(x, 6) for x in xs if 0.0 < x < 6.0})
    assert interieurs == [pytest.approx(2.55), pytest.approx(3.45)]


def test_a_window_opening_lands_on_the_windows_layer(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0]],
                    "openings": [
                        {"segment": 0, "position": 0.5, "width": 1.2, "kind": "window"}
                    ],
                }
            ]
        },
    )
    assert "WINDOWS" in layers_of(backend)
    assert "DOORS" not in layers_of(backend)


def test_a_passage_opening_leaves_no_symbol(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    """Une trémie nue perce le mur sans rien y poser."""
    call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0]],
                    "openings": [
                        {"segment": 0, "position": 0.5, "width": 1.0, "kind": "passage"}
                    ],
                }
            ]
        },
    )
    assert layers_of(backend) == {"WALLS"}
    assert len(list(backend.document.modelspace())) == 2


def test_show_symbols_false_keeps_only_the_masonry(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0]],
                    "show_symbols": False,
                    "openings": [{"segment": 0, "position": 0.5, "width": 0.9}],
                }
            ]
        },
    )
    assert layers_of(backend) == {"WALLS"}


def test_opening_outside_the_run_is_refused_with_its_range(tools: ToolHandlers) -> None:
    """Un rang de mur inexistant doit être dit, pas absorbé.

    Sans ce contrôle et avec ``show_symbols`` à faux, la baie serait ignorée en
    silence: le modèle croirait avoir percé un mur resté plein.
    """
    result = call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0], [6, 4]],
                    "show_symbols": False,
                    "openings": [{"segment": 5, "position": 0.5, "width": 0.9}],
                }
            ]
        },
    )
    assert result.is_error is True
    failure = result.payload["failures"][0]
    assert failure["index"] == 0
    assert failure["stage"] == "build"
    assert failure["code"] == InvalidParameter.code
    assert failure["details"]["segments"] == 2
    assert "0 à 1" in failure["details"]["remedy"]


def test_opening_position_outside_zero_one_is_refused(tools: ToolHandlers) -> None:
    result = call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0]],
                    "openings": [{"segment": 0, "position": 1.4, "width": 0.9}],
                }
            ]
        },
    )
    assert result.is_error is True
    assert result.payload["failures"][0]["details"]["field"] == "position"


def test_opening_angle_is_given_in_degrees(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    """Degrés au schéma, radians dans le modèle, degrés dans le DXF.

    Une double conversion donnerait ici un arc de 1,05 degré au lieu de 60.
    """
    call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0]],
                    "openings": [
                        {
                            "segment": 0,
                            "position": 0.5,
                            "width": 0.9,
                            "opening_deg": 60,
                        }
                    ],
                }
            ]
        },
    )
    arc = backend.document.modelspace().query("ARC")[0]
    ouverture = (arc.dxf.end_angle - arc.dxf.start_angle) % 360.0
    assert ouverture == pytest.approx(60.0)


def test_closed_run_has_one_more_segment_than_points_minus_one(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    """Le dernier rang d'une enfilade fermée, c'est le retour au premier point."""
    result = call(
        tools,
        "build_structure",
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0], [6, 4], [0, 4]],
                    "closed": True,
                    "openings": [{"segment": 3, "position": 0.5, "width": 0.9}],
                }
            ]
        },
    )
    assert result.is_error is False
    assert "DOORS" in layers_of(backend)


def test_wall_network_payload_validates_against_its_schema(
    catalog: tuple[ToolSpec, ...],
) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = spec_of(catalog, "build_structure").input_schema
    jsonschema.validate(
        {
            "elements": [
                {
                    "element": "wall_network",
                    "points": [[0, 0], [6, 0], [6, 4], [0, 4]],
                    "closed": True,
                    "thickness": 0.2,
                    "openings": [
                        {
                            "segment": 0,
                            "position": 0.4,
                            "width": 0.9,
                            "kind": "door",
                            "hand": "right",
                            "opening_deg": 90,
                        },
                        {"segment": 1, "position": 0.5, "width": 1.2, "kind": "window"},
                    ],
                }
            ],
            "label": "logement",
        },
        schema,
    )


def test_build_structure_description_points_to_wall_network(
    catalog: tuple[ToolSpec, ...],
) -> None:
    """Le modèle ne lit que la description: c'est là que le choix se joue."""
    description = spec_of(catalog, "build_structure").description
    assert "wall_network" in description
    assert "openings" in description


# ---------------------------------------------------------------------------
# place_blocks
# ---------------------------------------------------------------------------


def test_block_enum_is_read_from_the_library(catalog: tuple[ToolSpec, ...]) -> None:
    """Le schéma énumère la bibliothèque, il ne la recopie pas.

    Une copie diverge au premier symbole ajouté, et le modèle appelle alors une
    clé que le serveur refuse, ou en ignore une qu'il accepterait.
    """
    schema = spec_of(catalog, "place_blocks").input_schema
    enum = schema["properties"]["blocks"]["items"]["properties"]["block"]["enum"]
    assert enum == list(blocks.LIBRARY)
    description = schema["properties"]["blocks"]["items"]["properties"]["block"][
        "description"
    ]
    for key in blocks.LIBRARY:
        assert key in description


def test_place_blocks_inserts_every_occurrence(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    result = call(
        tools,
        "place_blocks",
        {
            "blocks": [
                {"block": "toilet", "at": [1.0, 0.1]},
                {"block": "basin", "at": [2.0, 0.1]},
                {"block": "shower", "at": [3.0, 0.1]},
            ],
            "label": "sanitaires",
        },
    )
    assert result.is_error is False
    assert result.payload["created_count"] == 3
    inserts = backend.document.modelspace().query("INSERT")
    assert len(inserts) == 3
    assert {i.dxf.name for i in inserts} == {"MCP_TOILET", "MCP_BASIN", "MCP_SHOWER"}
    # Les handles annoncés existent vraiment.
    for handle in result.payload["handles"]:
        assert backend.document.entitydb.get(handle) is not None


def test_missing_definitions_are_created_without_being_asked(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    """Le modèle ne doit pas avoir à définir un bloc avant de l'insérer."""
    result = call(tools, "place_blocks", {"blocks": [{"block": "chair", "at": [0, 0]}]})
    assert result.payload["blocks"] == ["MCP_CHAIR"]
    assert "MCP_CHAIR" in backend.document.blocks
    # Une définition n'est pas une entité: elle ne compte pas comme création.
    assert result.payload["created_count"] == 1


def test_a_definition_is_written_once_for_many_occurrences(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    """C'est tout l'intérêt du bloc: corriger une fois, cent occurrences suivent."""
    call(
        tools,
        "place_blocks",
        {"blocks": [{"block": "chair", "at": [i, 0]} for i in range(6)]},
    )
    assert len(list(backend.document.blocks.get("MCP_CHAIR").attdefs())) == 1
    assert len(backend.document.modelspace().query("INSERT")) == 6


def test_each_block_lands_on_the_layer_of_its_nature(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    call(
        tools,
        "place_blocks",
        {
            "blocks": [
                {"block": "toilet", "at": [0, 0]},
                {"block": "outlet", "at": [1, 0]},
                {"block": "table", "at": [2, 0]},
            ]
        },
    )
    assert layers_of(backend) == {"PLUMBING", "ELECTRICAL", "FURNITURE"}


def test_block_rotation_is_given_in_degrees(
    tools: ToolHandlers, backend: EzdxfBackend
) -> None:
    call(
        tools,
        "place_blocks",
        {"blocks": [{"block": "basin", "at": [0, 0], "rotation_deg": 90}]},
    )
    insert = backend.document.modelspace().query("INSERT")[0]
    assert insert.dxf.rotation == pytest.approx(90.0)
    assert math.isclose(math.radians(90.0), math.pi / 2)


def test_the_mark_reaches_the_occurrence(tools: ToolHandlers, backend: EzdxfBackend) -> None:
    """Sans repère valorisé, une occurrence ne dit que sa présence."""
    call(
        tools,
        "place_blocks",
        {"blocks": [{"block": "toilet", "at": [0, 0], "mark": "WC2"}]},
    )
    insert = backend.document.modelspace().query("INSERT")[0]
    assert insert.get_attrib_text(blocks.MARK_TAG) == "WC2"


def test_unknown_block_key_is_refused_with_the_catalogue(tools: ToolHandlers) -> None:
    result = call(
        tools,
        "place_blocks",
        {
            "blocks": [
                {"block": "chair", "at": [0, 0]},
                {"block": "jacuzzi", "at": [1, 0]},
            ]
        },
    )
    assert result.is_error is True
    assert result.payload["created_count"] == 1
    failure = result.payload["failures"][0]
    assert failure["index"] == 1
    assert failure["code"] == InvalidParameter.code
    assert "chair" in failure["details"]["supported"]


def test_negative_scale_is_refused(tools: ToolHandlers) -> None:
    result = call(
        tools, "place_blocks", {"blocks": [{"block": "chair", "at": [0, 0], "scale": 0}]}
    )
    assert result.is_error is True
    assert result.payload["failures"][0]["code"] == InvalidParameter.code


def test_place_blocks_payload_validates_against_its_schema(
    catalog: tuple[ToolSpec, ...],
) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = spec_of(catalog, "place_blocks").input_schema
    jsonschema.validate(
        {
            "blocks": [
                {"block": "toilet", "at": [1.2, 0.1], "rotation_deg": 0, "mark": "WC1"},
                {"block": "bed_double", "at": [4, 2], "rotation_deg": 90, "scale": 1},
            ]
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"blocks": [{"block": "jacuzzi", "at": [0, 0]}]}, schema)


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------


def _draw_a_few(tools: ToolHandlers) -> None:
    call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [4, 0], "layer": "WALLS"},
                {"op": "line", "start": [4, 0], "end": [4, 3], "layer": "WALLS"},
                {"op": "circle", "center": [8, 8], "radius": 0.5, "layer": "SITE"},
            ]
        },
    )


def test_measure_summary_says_what_the_drawing_holds(tools: ToolHandlers) -> None:
    _draw_a_few(tools)
    result = call(tools, "measure", {"mode": "summary"})
    assert result.is_error is False
    summary = result.payload["summary"]
    assert summary["count"] == 3
    assert summary["by_layer"] == {"WALLS": 2, "SITE": 1}
    assert summary["by_type"] == {"LINE": 2, "CIRCLE": 1}
    assert summary["extents"] == [pytest.approx(0.0), pytest.approx(0.0),
                                  pytest.approx(8.5), pytest.approx(8.5)]


def test_a_measure_the_engine_does_not_publish_is_null_not_zero(
    tools: ToolHandlers,
) -> None:
    """Le mensonge qu'on refuse: une surface à zéro sur un dessin de segments.

    Zéro se recopie dans un devis sans se relire. « Inconnu » se remarque. Ici
    le moteur publie bien la longueur des segments, mais aucune aire: une ligne
    n'en a pas. Les deux mesures doivent donc se lire différemment.
    """
    call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [4, 0], "layer": "WALLS"},
                {"op": "line", "start": [4, 0], "end": [4, 3], "layer": "WALLS"},
            ]
        },
    )
    result = call(tools, "measure", {"mode": "totals"})
    assert result.payload["length"]["total"] == pytest.approx(7.0)
    assert result.payload["length"]["complete"] is True

    aire = result.payload["area"]
    assert aire["total"] is None, "zéro voudrait dire « mesuré, et ça fait zéro »"
    assert aire["counted"] == 0
    assert "non nul" in aire["note"]


def test_a_measure_the_engine_publishes_is_counted(
    tools: ToolHandlers, backend: EzdxfBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Et l'inverse: ce qui est publié est compté, et le partiel est dit."""
    entities = [
        EntityInfo("A1", "LINE", "WALLS", 256, (0, 0, 4, 0), {"length": 4.0}),
        EntityInfo("A2", "LINE", "WALLS", 256, (0, 0, 3, 0), {"length": 3.0}),
        EntityInfo("A3", "TEXT", "ANNOTATION", 256, (0, 0, 1, 1), {"text": "SEJOUR"}),
    ]
    monkeypatch.setattr(backend, "query", lambda selector=None, *, limit=None: list(entities))
    monkeypatch.setattr(backend, "count", lambda selector=None: len(entities))

    result = call(tools, "measure", {"mode": "totals"})
    mesure = result.payload["length"]
    assert mesure["total"] == pytest.approx(7.0)
    assert mesure["counted"] == 2
    assert mesure["missing"] == 1
    assert mesure["complete"] is False
    assert "ne publient pas cette mesure" in mesure["note"]


def test_measure_groups_by_layer_and_by_type(tools: ToolHandlers) -> None:
    _draw_a_few(tools)
    by_layer = call(tools, "measure", {"mode": "by_layer"}).payload
    assert {row["layer"]: row["count"] for row in by_layer["groups"]} == {
        "WALLS": 2,
        "SITE": 1,
    }
    by_type = call(tools, "measure", {"mode": "by_type"}).payload
    assert {row["type"]: row["count"] for row in by_type["groups"]} == {
        "LINE": 2,
        "CIRCLE": 1,
    }


def test_measure_quantities_is_one_row_per_layer_and_type(tools: ToolHandlers) -> None:
    _draw_a_few(tools)
    payload = call(tools, "measure", {"mode": "quantities"}).payload
    rows = {(row["layer"], row["type"]): row["count"] for row in payload["rows"]}
    assert rows == {("WALLS", "LINE"): 2, ("SITE", "CIRCLE"): 1}
    assert payload["row_count"] == 2


def test_measure_scope_restricts_to_a_layer(tools: ToolHandlers) -> None:
    _draw_a_few(tools)
    payload = call(tools, "measure", {"mode": "summary", "layer": "WALLS"}).payload
    assert payload["scope"] == {"layer": "WALLS"}
    assert payload["matching"] == 2
    assert payload["summary"]["count"] == 2


def test_measure_nearest_sorts_by_distance(tools: ToolHandlers) -> None:
    _draw_a_few(tools)
    payload = call(tools, "measure", {"mode": "nearest", "point": [0, 0], "limit": 2}).payload
    distances = [entity["distance"] for entity in payload["entities"]]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0)
    assert len(payload["entities"]) == 2


def test_measure_nearest_without_a_point_is_an_error(tools: ToolHandlers) -> None:
    _draw_a_few(tools)
    result = call(tools, "measure", {"mode": "nearest"})
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code
    assert result.payload["details"]["field"] == "point"


def test_measure_in_window_distinguishes_inside_from_crossing(tools: ToolHandlers) -> None:
    """La distinction d'AutoCAD, et elle change tout sur un plan."""
    _draw_a_few(tools)
    inside = call(
        tools, "measure", {"mode": "in_window", "window": [0, 0, 4, 1]}
    ).payload
    crossing = call(
        tools,
        "measure",
        {"mode": "in_window", "window": [0, 0, 4, 1], "window_mode": "crossing"},
    ).payload
    assert inside["matched"] == 1
    assert crossing["matched"] == 2


def test_measure_bounds_its_entity_lists(tools: ToolHandlers) -> None:
    call(
        tools,
        "draw",
        {"operations": [{"op": "line", "start": [0, i], "end": [1, i]} for i in range(40)]},
    )
    payload = call(
        tools, "measure", {"mode": "in_window", "window": [-1, -1, 2, 50], "limit": 5}
    ).payload
    assert payload["matched"] == 40
    assert len(payload["entities"]) == 5
    assert payload["entities_truncated"] is True
    assert payload["entities_total"] == 40


def test_measure_refuses_an_unknown_mode(tools: ToolHandlers) -> None:
    result = call(tools, "measure", {"mode": "volume"})
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code
    assert set(result.payload["details"]["supported"]) == set(MEASURE_MODES)


def test_measure_is_declared_read_only(catalog: tuple[ToolSpec, ...]) -> None:
    spec = spec_of(catalog, "measure")
    assert spec.read_only is True
    assert spec.idempotent is True


# ---------------------------------------------------------------------------
# check_plan
# ---------------------------------------------------------------------------


def test_an_open_contour_is_reported_with_its_remedy(tools: ToolHandlers) -> None:
    result = call(
        tools,
        "check_plan",
        {
            "contours": [
                {
                    "points": [[0, 0], [4, 0], [4, 3], [0, 3], [0, 0.05]],
                    "ref": "sejour",
                }
            ]
        },
    )
    # Un plan fautif n'est pas un appel fautif: la vérification a bien eu lieu.
    assert result.is_error is False
    assert result.payload["ok"] is False
    problem = result.payload["problems"][0]
    assert problem["kind"] == "open_contour"
    assert problem["severity"] == "error"
    assert problem["refs"] == ["sejour"]
    assert problem["location"] == [pytest.approx(0.0), pytest.approx(0.025)]
    assert "Fermer la polyligne" in problem["remedy"]


def test_crossing_walls_are_located(tools: ToolHandlers) -> None:
    result = call(
        tools,
        "check_plan",
        {
            "segments": [
                {"start": [0, 0], "end": [4, 0], "ref": "mur sud"},
                {"start": [2, -1], "end": [2, 1], "ref": "refend"},
            ]
        },
    )
    problem = next(p for p in result.payload["problems"] if p["kind"] == "crossing_walls")
    assert problem["location"] == [pytest.approx(2.0), pytest.approx(0.0)]
    assert problem["refs"] == ["mur sud", "refend"]


def test_a_hairline_gap_between_endpoints_is_found(tools: ToolHandlers) -> None:
    """Le défaut qui fait échouer une hachure sans rien montrer à l'écran."""
    result = call(
        tools,
        "check_plan",
        {
            "segments": [
                {"start": [0, 0], "end": [4, 0], "ref": "mur sud"},
                {"start": [4.00005, 0], "end": [4.00005, 3], "ref": "mur est"},
            ]
        },
    )
    problem = next(p for p in result.payload["problems"] if p["kind"] == "gap")
    assert problem["severity"] == "error"
    assert problem["details"]["gap"] == pytest.approx(5e-5)


def test_duplicates_are_found_in_the_document_itself(tools: ToolHandlers) -> None:
    """Les doublons, eux, se voient sans qu'on fournisse la géométrie."""
    call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [4, 0], "layer": "WALLS"},
                {"op": "line", "start": [0, 0], "end": [4, 0], "layer": "WALLS"},
            ]
        },
    )
    result = call(tools, "check_plan", {})
    problem = next(p for p in result.payload["problems"] if p["kind"] == "duplicate")
    assert problem["severity"] == "warning"
    assert len(problem["refs"]) == 2
    assert result.payload["checked"]["entities"] == 2


def test_a_residue_smaller_than_the_threshold_is_reported(tools: ToolHandlers) -> None:
    call(
        tools,
        "draw",
        {
            "operations": [
                {"op": "line", "start": [0, 0], "end": [4, 0]},
                {"op": "line", "start": [1, 1], "end": [1.0005, 1]},
            ]
        },
    )
    result = call(tools, "check_plan", {"minimum_size": 0.01})
    problem = next(p for p in result.payload["problems"] if p["kind"] == "tiny_entity")
    assert problem["details"]["size"] == pytest.approx(0.0005)
    assert result.payload["minimum_size"] == pytest.approx(0.01)


def test_a_clean_plan_reports_ok_and_says_with_which_thresholds(
    tools: ToolHandlers,
) -> None:
    result = call(
        tools,
        "check_plan",
        {
            "contours": [{"points": [[0, 0], [4, 0], [4, 3], [0, 3]], "closed": True}],
            "segments": [{"start": [0, 0], "end": [4, 0]}],
        },
    )
    assert result.payload["ok"] is True
    assert result.payload["problems"] == []
    # Un rapport vert sur zéro objet examiné ne voudrait rien dire.
    assert result.payload["checked"] == {"entities": 0, "contours": 1, "segments": 1}
    assert result.payload["tolerance"] == pytest.approx(1e-4)


def test_check_plan_says_what_it_could_not_check(tools: ToolHandlers) -> None:
    """Le moteur publie des boîtes, pas des sommets: l'aveu vaut mieux que le silence."""
    result = call(tools, "check_plan", {})
    assert "contours" in result.payload["hint"]
    assert "segments" in result.payload["hint"]


def test_check_plan_refuses_an_oversized_submission(tools: ToolHandlers) -> None:
    result = call(
        tools,
        "check_plan",
        {"segments": [{"start": [0, 0], "end": [1, 1]}] * (MAX_CHECK_ITEMS + 1)},
    )
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code
    assert result.payload["details"]["maximum"] == MAX_CHECK_ITEMS


def test_check_plan_payload_validates_against_its_schema(
    catalog: tuple[ToolSpec, ...],
) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = spec_of(catalog, "check_plan").input_schema
    jsonschema.validate(
        {
            "layer": "WALLS",
            "contours": [{"points": [[0, 0], [4, 0], [4, 3]], "ref": "sejour"}],
            "segments": [{"start": [0, 0], "end": [4, 0], "ref": "mur sud"}],
            "tolerance": 0.001,
            "minimum_size": 0.01,
        },
        schema,
    )


# ---------------------------------------------------------------------------
# run_cad_command
# ---------------------------------------------------------------------------


def test_a_command_outside_the_whitelist_is_refused_with_the_list(
    tools: ToolHandlers, config: Config
) -> None:
    """Une chaîne libre passée à un logiciel de CAO, c'est du code arbitraire."""
    result = call(
        tools, "run_cad_command", {"command": "SCRIPT", "confirm_command": True}
    )
    assert result.is_error is True
    assert result.payload["code"] == InvalidParameter.code
    assert result.payload["details"]["allowed"] == sorted(config.allowed_commands)
    assert result.payload["details"]["got"] == "SCRIPT"


def test_the_whitelist_is_checked_before_the_confirmation(tools: ToolHandlers) -> None:
    """Une commande interdite est interdite, confirmée ou non."""
    result = call(tools, "run_cad_command", {"command": "DEL", "confirm_command": False})
    assert result.payload["code"] == InvalidParameter.code


def test_an_allowed_command_still_demands_a_confirmation(tools: ToolHandlers) -> None:
    result = call(tools, "run_cad_command", {"command": "OFFSET"})
    assert result.is_error is True
    assert result.payload["code"] == ConfirmationRequired.code
    assert "confirm_command" in result.payload["details"]["remedy"]


def test_the_dxf_backend_says_it_has_no_command_interpreter(tools: ToolHandlers) -> None:
    """Pas de faux succès: le backend DXF n'a pas de ligne de commande."""
    result = call(
        tools,
        "run_cad_command",
        {"command": "offset", "arguments": ["0.2"], "confirm_command": True},
    )
    assert result.is_error is True
    assert result.payload["code"] == UnsupportedOperation.code


def test_get_drawing_info_warns_before_the_model_tries(
    tools: ToolHandlers, config: Config
) -> None:
    payload = call(tools, "get_drawing_info").payload
    assert payload["can_run_commands"] is False
    assert payload["allowed_commands"] == sorted(config.allowed_commands)
    assert payload["blocks_library"] == list(blocks.LIBRARY)


def test_run_cad_command_is_declared_destructive(catalog: tuple[ToolSpec, ...]) -> None:
    spec = spec_of(catalog, "run_cad_command")
    assert spec.destructive is True
    assert spec.read_only is False
    assert "IRRÉVERSIBLE" in spec.description
    assert "DXF" in spec.description
    assert "confirm_command" in spec.input_schema["required"]


def test_the_command_enum_follows_the_configuration() -> None:
    """La liste blanche est une configuration, pas une constante du catalogue."""
    settings = Config(
        backend="ezdxf", unit=Unit.METER, allowed_commands=frozenset({"OFFSET"})
    )
    schema = spec_of(build_catalog(settings), "run_cad_command").input_schema
    assert schema["properties"]["command"]["enum"] == ["OFFSET"]


# ---------------------------------------------------------------------------
# Le catalogue entier reste cohérent
# ---------------------------------------------------------------------------


def test_every_new_tool_is_routed(config: Config) -> None:
    router = ToolHandlers(config, RecordingBackend(unit=Unit.METER))
    assert {"measure", "check_plan", "place_blocks", "run_cad_command"} <= set(
        router.handled
    )
