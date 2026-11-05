import builtins
import io
import xml.etree.ElementTree as ET

import pytest

from plugin.plugins.neko_rimworld.agent.world.construction_geometry import (
    ConstructionPlanError,
    compile_plan,
    inspect_layout,
)
from plugin.plugins.neko_rimworld.agent.world.spatial_rendering import (
    SpatialRenderingUnavailable,
    build_render_spec,
    render_scene_png,
    render_scene_svg,
)


@pytest.fixture
def scene():
    observation = {
        "format": "construction_observation.v1",
        "map_id": "map-1",
        "bounds": {"min_x": 10, "min_z": 20, "width": 3, "height": 2},
        "cells": [{"x": 10, "z": 20, "known": True, "terrain": "Soil", "walkable": True}],
        "objects": [
            {
                "id": "native-1",
                "definition": "Wall",
                "stage": "built",
                "occupied_rect": {"min_x": 11, "max_x": 11, "min_z": 21, "max_z": 21},
            }
        ],
        "sample": {"tick": 123, "map_id": "map-1"},
        "coverage": {"cells_complete": False, "objects_complete": True},
    }
    catalog = [{"definition": "Wall", "size": {"x": 1, "z": 1}, "layer": "building", "passability": "Impassable"}]
    plan = compile_plan(
        {
            "map_id": "map-1",
            "origin": {"x": 10, "z": 20},
            "plan_id": "p1",
            "revision": 2,
            "elements": [{"id": '<bad>&"', "definition": "Wall", "position": {"x": 0, "z": 0}}],
        },
        catalog,
    )
    return observation, plan


def test_spec_keeps_native_scope_sample_hash_and_world_coordinates(scene):
    obs, plan = scene
    check = inspect_layout(plan, obs)
    spec = build_render_spec(obs, plan, check)
    assert spec["sample"] == obs["sample"]
    assert spec["coverage"] == obs["coverage"]
    assert len(spec["cells"]) == 6
    assert sum(c.get("known") is not True for c in spec["cells"]) == 5
    assert spec["elements"][0]["cells"] == [{"x": 10, "z": 20}]
    assert spec["objects"][0]["cells"] == [{"x": 11, "z": 21}]
    assert len(spec["source_sha256"]) == 64
    assert spec["plan_id"] == "p1" and spec["revision"] == 2
    assert build_render_spec(obs, plan, check) == spec


def test_svg_is_deterministic_escaped_and_has_no_external_assets(scene):
    obs, plan = scene
    svg = render_scene_svg(obs, plan)
    ET.fromstring(svg)
    assert svg == render_scene_svg(obs, plan)
    assert "&lt;bad&gt;&amp;" in svg
    assert "<bad>" not in svg
    assert "href=" not in svg


def test_png_uses_host_pillow_is_valid_and_deterministic(scene):
    from PIL import Image

    obs, plan = scene
    png = render_scene_png(obs, plan)
    assert png == render_scene_png(obs, plan)
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.size == (560, 104)
        assert image.getpixel((33, 33)) != (255, 255, 255)


def test_wrong_map_duplicate_cells_and_stale_check_rejected(scene):
    obs, plan = scene
    plan["map_id"] = "other"
    with pytest.raises(ConstructionPlanError, match="map"):
        build_render_spec(obs, plan)
    plan["map_id"] = "map-1"
    with pytest.raises(ConstructionPlanError, match="different plan"):
        build_render_spec(obs, plan, {"plan_sha256": "stale"})
    obs["cells"].append(obs["cells"][0])
    with pytest.raises(ConstructionPlanError, match="unique"):
        build_render_spec(obs)


def test_observed_check_cannot_be_reused_against_a_new_sample(scene):
    obs, plan = scene
    check = inspect_layout(plan, obs)
    obs["sample"]["tick"] += 1
    with pytest.raises(ConstructionPlanError, match="different observation"):
        build_render_spec(obs, plan, check)


def test_plugin_only_installation_retains_svg_and_reports_png_backend(scene, monkeypatch):
    original_import = builtins.__import__

    def import_without_pillow(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("Pillow absent in plugin-only installation")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_pillow)
    obs, plan = scene
    assert render_scene_svg(obs, plan).startswith("<svg")
    with pytest.raises(SpatialRenderingUnavailable, match="Pillow"):
        render_scene_png(obs, plan)
