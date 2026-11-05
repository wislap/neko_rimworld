"""Actionable validation feedback never expands tool permissions."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ..agent.runtime.tools import ToolRegistry
from ..message_contracts import ToolCall
from ..tool_contracts import ToolExecutionError, ToolSpec
from ..agent.runtime.frontdesk_service import FrontdeskService


def test_flattened_planner_entry_gets_top_level_repair_hint_without_rewrite():
    called = []
    async def handler(args):
        called.append(args)
        return {}
    registry = ToolRegistry()
    registry.register(ToolSpec("planner_board", "board", {"type": "object", "properties": {
        "action": {"type": "string"}, "entry": {"type": "object", "properties": {
            "body": {"type": "string"}, "entry_id": {"type": "string"}, "status": {"type": "string"}}}},
        "additionalProperties": False}, handler))
    args = {"action": "write", "entry_id": "task", "status": "open", "body": "private value"}
    result = registry.prepare(ToolCall("call", "planner_board", json.dumps(args)))
    assert not result.ok and result.value["error"]["code"] == "invalid_tool_arguments"
    assert result.value["error"]["details"] == {"path": "$", "allowed_fields": ["action", "entry"],
        "allowed_fields_truncated": False, "unsupported_fields": ["body", "entry_id", "status"],
        "unsupported_fields_truncated": False}
    assert "inside the entry object" in result.value["error"]["message"]
    model_result = json.loads(result.as_message()["content"])
    assert model_result["error"]["details"] == result.value["error"]["details"]
    assert "private value" not in json.dumps(model_result) and not called


def test_unknown_field_feedback_is_bounded():
    async def handler(args): return {}
    registry = ToolRegistry()
    registry.register(ToolSpec("sample", "sample", {"type": "object", "properties": {
        "allowed" + str(i).zfill(2) + "x" * 100: {} for i in range(40)},
        "additionalProperties": False}, handler))
    args = {str(i).zfill(2) + "x" * 100: 1 for i in range(40)}
    result = registry.prepare(ToolCall("call", "sample", json.dumps(args)))
    detail = result.value["error"]["details"]
    assert len(detail["unsupported_fields"]) == 32
    assert all(len(field) == 80 for field in detail["unsupported_fields"])
    assert detail["unsupported_fields_truncated"] is True
    assert len(detail["allowed_fields"]) == 32 and detail["allowed_fields_truncated"] is True
    assert all(len(field) == 80 for field in detail["allowed_fields"])


@pytest.mark.asyncio
async def test_frontdesk_feedback_tracks_only_user_sources_delivered_in_this_run(tmp_path):
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    old = await service.call("record_utterance", "colony", "epoch", 1, "old user words")
    pending = await service.call("record_utterance", "colony", "epoch", 1, "pending user words")
    trigger = await service.call("record", "colony", "epoch", 1, "plugin", "frontdesk.trigger",
                                 {"source": "planner", "text": "not user words"})
    delivered = set()
    tool = service.capabilities(game, None, delivered, explicit=True)[0]
    registry = ToolRegistry()
    registry.register(tool)
    entry = {"entry_id": "intent", "expected_revision": 0, "status": "open",
             "priority": 1, "review_at_tick": None, "body": "test"}
    for seq in (None, old, pending, trigger):
        args = {"action": "write_intent", "entry": entry}
        if seq is not None:
            args["source_event_seq"] = seq
        with pytest.raises(ToolExecutionError) as error:
            await tool.handler(args)
        assert error.value.details == {"available_user_utterance_seqs": []}
        assert "old user words" not in str(error.value.details)
    # The scheduler adds this ID only when its original is handed to the inbox.
    delivered.add(pending)
    with pytest.raises(ToolExecutionError) as error:
        await tool.handler({"action": "write_intent", "entry": entry, "source_event_seq": old})
    assert error.value.details == {"available_user_utterance_seqs": [pending]}
    prepared = registry.prepare(ToolCall("invalid-source", "frontdesk_board", json.dumps({
        "action": "write_intent", "entry": entry, "source_event_seq": old})))
    rejected = await registry.execute(prepared)
    assert json.loads(rejected.as_message()["content"])["error"]["details"] == {
        "available_user_utterance_seqs": [pending]}
    result = await tool.handler({"action": "write_intent", "entry": entry, "source_event_seq": pending})
    board = result["entries"]
    assert board[0]["source"] == {"event_seq": pending}
    await service.close()



@pytest.mark.asyncio
async def test_feedback_uses_real_inbox_delivery_boundary(tmp_path):
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.colony_scheduler import ColonyScheduler
    from ..agent.runtime.loop import AgentRuntime
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, settings, turn
    from .test_capability_composition import Script, call

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    entered, proceed = asyncio.Event(), asyncio.Event()
    source = {}
    entry = {"entry_id": "intent", "expected_revision": 0, "status": "open",
             "priority": 1, "review_at_tick": None, "body": "test"}
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            if not self.requests:
                entered.set()
                await proceed.wait()
            if len(self.requests) < 2:
                self.turns[0] = call("frontdesk_board", {"action": "write_intent", "entry": entry,
                                                          "source_event_seq": source["seq"]})
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([turn(""), turn(""), call("update_only", {})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        await scheduler.wake_frontdesk(game, "event", source="game")
        await asyncio.wait_for(entered.wait(), 5)
        await scheduler.wake_frontdesk(game, "actual words")
        events = await service.call("events_by_kind", "colony", ("user.utterance",))
        source["seq"] = events[-1]["seq"]
        proceed.set()
        await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
        errors = [json.loads(m["content"]) for m in model.requests[1]["messages"]
                  if m.get("role") == "tool" and m.get("name") == "frontdesk_board"]
        assert errors[-1]["error"]["details"]["available_user_utterance_seqs"] == []
        assert any(m.get("name") == "frontdesk_inbox" and "actual words" in m["content"]
                   for m in model.requests[1]["messages"])
        results = [json.loads(m["content"]) for m in model.requests[2]["messages"]
                   if m.get("role") == "tool" and m.get("name") == "frontdesk_board"]
        assert results[-1]["ok"] is True
        assert (await service.call("board", "colony"))[0]["source"] == {"event_seq": source["seq"]}
    finally:
        await scheduler.close()
        await service.close()
