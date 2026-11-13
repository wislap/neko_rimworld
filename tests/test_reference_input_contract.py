"""Input and field contracts exercised by the rescue composition study."""

import copy

import pytest
from jsonschema import ValidationError, validate
from plugin.plugins.neko_rimworld.agent.world.query_catalog import IDENTITY_FIELDS, object_schema
from plugin.plugins.neko_rimworld.agent.world.query_planner import _pawn_entries, _supported, route_object
from plugin.plugins.neko_rimworld.agent.world.query_surface import Surface, SurfaceError
from plugin.plugins.neko_rimworld.agent.world.query_validation import normalize_object


def test_exact_thing_set_preserves_scope_and_does_not_change_single_thing():
    obj = normalize_object(
        {
            "kind": "things",
            "ids": ["medicine1", "medicine2"],
            "map": "Map_7",
            "include_held": False,
            "include_fogged": False,
        }
    )
    assert route_object(obj) == (
        "things",
        {
            "map_id": "Map_7",
            "include_held": False,
            "visible_only": True,
        },
        {"id": {"in": ["medicine1", "medicine2"]}},
    )
    assert route_object(normalize_object({"kind": "thing", "id": "medicine1"})) == (
        "thing",
        {"id": "medicine1"},
        None,
    )
    with pytest.raises(SurfaceError, match="exactly one"):
        normalize_object({"kind": "thing", "ids": ["medicine1", "medicine2"]})
    for selector in ({"definition": "MedicineIndustrial"}, {"category": "Item"}, {"name": "medicine"}):
        with pytest.raises(SurfaceError):
            normalize_object({"kind": "things", "ids": ["medicine1"], **selector})


def test_category_enum_is_native_and_only_applies_to_physical_things():
    directory = {"value_domains": {"Verse.ThingCategory": {"values": ["Item", "Building", "Pawn"]}}}
    original = copy.deepcopy(directory)
    schema = {"type": "object", "properties": {"object": object_schema(directory)}}
    validate({"object": {"kind": "things", "category": "Item"}, "aspects": ["identity"]}, schema)
    with pytest.raises(ValidationError):
        validate({"object": {"kind": "things", "category": "Medicine"}, "aspects": ["identity"]}, schema)
    # thing_defs uses the separate native ThingCategoryDef content-membership argument.
    validate({"object": {"kind": "thing_defs", "category": "Medicine"}, "aspects": ["identity"]}, schema)
    assert directory == original
    assert "things: ids, names" in schema["properties"]["object"]["description"]
    assert "enum" not in object_schema()["properties"]["category"]




def test_identity_positions_and_trait_description_are_capability_gated():
    directory = {
        "roots": [{"from": "pawn", "type": "Pawn"}, {"from": "thing", "type": "Thing"}],
        "field_index": {
            "Pawn": {"id": "String", "status": "Status", "profile": "Profile"},
            "Status": {"position": "Position"},
            "Position": {"x": "Int", "z": "Int", "map_id": "String"},
            "Thing": {"id": "String", "position": "Position"},
            "Profile": {"traits": "Trait[]"},
            "Trait": {"description": "String"},
        },
    }
    surface = Surface(directory)
    assert {"status.position.x", "status.position.z", "status.position.map_id"} <= set(
        _pawn_entries(surface, "identity", 0)[0]["fields"]
    )
    assert {"position.x", "position.z"} <= set(_supported(surface, "thing", IDENTITY_FIELDS["thing"]))
    assert "description" in _pawn_entries(surface, "profile", 5)[-1]["fields"]
    directory["field_index"]["Status"] = {}
    directory["field_index"]["Trait"] = {}
    old = Surface(directory)
    assert _pawn_entries(old, "identity", 0)[0]["fields"] == ["id"]
    assert "description" not in _pawn_entries(old, "profile", 5)[-1]["fields"]


def test_pawn_identity_carries_gender_when_the_schema_has_it():
    # 实测：默认 identity 不带性别时，模型按名字猜代词（Lumi/Vicky 被写成“她”）。
    directory = {
        "roots": [{"from": "pawn", "type": "Pawn"}],
        "field_index": {
            "Pawn": {"id": "String", "profile": "Profile"},
            "Profile": {"gender": "String"},
        },
    }
    assert "profile.gender" in _pawn_entries(Surface(directory), "identity", 0)[0]["fields"]
    directory["field_index"]["Profile"] = {}
    assert "profile.gender" not in _pawn_entries(Surface(directory), "identity", 0)[0]["fields"]
