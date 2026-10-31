import copy
import json

import pytest

from ..agent.runtime.prompt import build_system_prompt
from ..agent.world.query_surface import Surface, SurfaceError
from ..agent.world.semantic_surface import plan_look
from ..workspace.journal import Journal
from .test_semantic_tools import catalog
from .test_wake_policy import native


FIELDS = {
    "PauseWindows": {"status": "String", "sampled_tick": "Int", "count": "Int", "truncated": "Boolean", "windows": "[PauseWindow]"},
    "PauseWindow": {"window_id": "Int", "type": "String", "stack_index": "Int", "opened_tick": "Int", "opening_source": "PauseWindowSource"},
    "PauseWindowSource": {"status": "String", "frames": "[String]"},
}


def directory_with_windows():
    directory = copy.deepcopy(catalog()["public_query"])
    directory["field_index"]["Game"]["pause_windows"] = "PauseWindows"
    directory["field_index"].update(copy.deepcopy(FIELDS))
    return directory


def test_fresh_pause_facet_requests_complete_bounded_fields_and_old_catalog_rejects():
    surface = Surface(directory_with_windows())
    plan = plan_look(surface, {"kind": "game"}, ["pause_windows"], 0)
    nested = plan.request["select"]["pause_windows"]["select"]
    assert set(nested) == {"status", "sampled_tick", "count", "truncated", "windows"}
    window = nested["windows"]["select"]
    assert set(window) == {"window_id", "type", "stack_index", "opened_tick", "opening_source"}
    assert set(window["opening_source"]["select"]) == {"status", "frames"}
    with pytest.raises(SurfaceError):
        plan_look(Surface(catalog()["public_query"]), {"kind": "game"}, ["pause_windows"], 0)


@pytest.mark.parametrize("status,count,truncated,windows", [
    ("observed", 0, False, []),
    ("observed", 2, False, [{"window_id": 4, "type": "A", "stack_index": 0, "opened_tick": None,
                              "opening_source": {"status": "unknown", "frames": []}},
                             {"window_id": 5, "type": "B", "stack_index": 1, "opened_tick": 3,
                              "opening_source": {"status": "observed", "frames": ["A.Open"]}}]),
    ("partial", 20, True, [{"window_id": 4, "type": "A", "stack_index": 0, "opened_tick": None,
                             "opening_source": {"status": "unknown", "frames": []}}]),
    ("unavailable", None, False, []),
])
def test_pause_event_preserves_raw_scope_and_frontdesk_trigger(tmp_path, status, count, truncated, windows):
    journal = Journal(tmp_path / "events.sqlite")
    pause = {"status": status, "sampled_tick": 10 if status != "unavailable" else None,
             "count": count, "truncated": truncated, "windows": windows}
    event = native(1, 10, "pause_changed", {"source": "window", "windows_force_paused": True,
                   "force_paused": True, "pause_windows": pause})
    journal.ingest_game_event("colony", "epoch", 10, "conn", event)
    rows = journal.events("colony")
    raw = next(e for e in rows if e["kind"] == "game.event")
    assert raw["payload"]["connection"] == "conn" and raw["load_epoch"] == "epoch"
    assert raw["payload"]["event"]["data"]["pause_windows"] == pause
    trigger = next(e for e in rows if e["kind"] == "frontdesk.trigger")
    assert json.loads(trigger["payload"]["text"])["event"]["data"]["pause_windows"] == pause
    journal.close()


def test_autoplay_prompts_distinguish_window_evidence_and_authority():
    for role in ("planner", "frontdesk"):
        text = build_system_prompt(autoplay=True, role=role)
        assert "pause_windows" in text and "not authorization" in text
        assert "force pause" in text
    assert "pause_windows" not in build_system_prompt()


@pytest.mark.asyncio
async def test_public_look_exposes_fresh_windows_only_on_supported_catalog():
    from ..agent.runtime.tools import ToolRegistry
    from ..agent.world.semantic_tools import semantic_interaction_factory
    from ..game.observation import Observation
    from ..game.query_tools import make_game_tools
    from ..message_contracts import ToolCall
    from .test_semantic_tools import Session, _update

    source = catalog()
    source["public_query"] = directory_with_windows()
    calls = []
    pause = {"status":"partial", "sampled_tick":123, "count":20, "truncated":True,
             "windows":[{"window_id":4, "type":"Dialog", "stack_index":1, "opened_tick":None,
                         "opening_source":{"status":"unknown", "frames":[]}},
                        {"window_id":5, "type":"Other", "stack_index":2, "opened_tick":120,
                         "opening_source":{"status":"observed", "frames":["Fixture.Open"]}}]}
    async def query(request):
        calls.append(request)
        return Observation({"observation_id":"raw-1", "received_at":"now", "request":request,
            "response":{"type":"query_result", "request_id":"request", "connection_epoch":"conn",
                "colony_id":"colony", "load_epoch":"epoch", "game_tick":123, "snapshot_sequence":4,
                "payload":{"status":"ok", "issues":[], "data":{"results":[{"rows":[{"pause_windows":pause}],
                    "page":{"offset":0,"returned":1,"has_more":False,"next_offset":None}}]}}}})
    session = Session()
    session.colony_id = "colony"
    reader = semantic_interaction_factory(make_game_tools(source, query), session, _update)
    registry = ToolRegistry()
    registry.register(next(tool for tool in reader.tools() if tool.name == "look"))
    schema = registry.schemas()[0]["function"]["parameters"]
    assert "pause_windows" in schema["properties"]["aspects"]["items"]["enum"]
    result = await registry.execute(registry.prepare(ToolCall("id", "look", json.dumps(
        {"object":{"kind":"game"}, "aspects":["pause_windows"]}))))
    assert result.ok, result.value
    assert calls and calls[0]["operation"] == "query"
    assert '"status":"partial"' in result.as_message()["content"]
    assert '"window_id":4' in result.as_message()["content"]
    assert '"status":"unknown"' in result.as_message()["content"]
    assert '"window_id":5' in result.as_message()["content"] and "Fixture.Open" in result.as_message()["content"]
    assert session.observations[0]["response"]["game_tick"] == 123
    old = semantic_interaction_factory(make_game_tools(catalog(), query), Session(), _update)
    old_schema = next(t for t in old.tools() if t.name == "look").parameters
    assert "pause_windows" not in old_schema["properties"]["aspects"]["items"]["enum"]


@pytest.mark.parametrize("pause", [
    {"status":"observed", "sampled_tick":10, "count":2, "truncated":False, "windows":[
        {"window_id":1, "type":"A", "stack_index":0, "opened_tick":8,
         "opening_source":{"status":"observed", "frames":["A.Open"]}},
        {"window_id":2, "type":"B", "stack_index":1, "opened_tick":None,
         "opening_source":{"status":"unknown", "frames":[]}}]},
    {"status":"unavailable", "sampled_tick":None, "count":None, "truncated":False, "windows":[]},
])
def test_render_preserves_multiple_and_unavailable_window_facts(pause):
    from ..agent.world.semantic_surface import render
    plan = plan_look(Surface(directory_with_windows()), {"kind":"game"}, ["pause_windows"], 0)
    raw = {"status":"ok", "data":{"results":[{"rows":[{"pause_windows":pause}],
            "scope":{}, "state":"complete", "fields":"complete", "listing":"complete"}]}}
    result = render(plan, raw)
    assert result["facts"][0]["facts"]["pause_windows"]["pause_windows"] == pause


def test_look_spec_accepts_wrapped_and_compiled_old_and_new_directories():
    from ..agent.world.look_tools import look_spec
    from ..game.query_tools import make_game_tools
    from .test_semantic_tools import public_source
    async def query(_):
        raise AssertionError("not called")
    wrapped_old = public_source()
    assert "pause_windows" not in look_spec(wrapped_old)["parameters"]["properties"]["aspects"]["items"]["enum"]
    wrapped_new = catalog()
    wrapped_new["public_query"] = directory_with_windows()
    assert "pause_windows" in look_spec(wrapped_new)["parameters"]["properties"]["aspects"]["items"]["enum"]
    compiled = make_game_tools(wrapped_new, query).directory
    assert "pause_windows" in look_spec(compiled)["parameters"]["properties"]["aspects"]["items"]["enum"]
    missing = copy.deepcopy(wrapped_new)
    missing["public_query"].pop("roots")
    assert "pause_windows" not in look_spec(missing)["parameters"]["properties"]["aspects"]["items"]["enum"]
