"""Real SDK entry, persistent SQLite and shared executor; no network or models."""

import copy
import json
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
from plugin.plugins.neko_rimworld.game.protocol import SessionDescriptor
from plugin.plugins.neko_rimworld.game.query_tools import make_game_tools
from plugin.plugins.neko_rimworld.message_contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.reports import ReportStore
from plugin.plugins.neko_rimworld.tool_contracts import ToolSetup

from .test_agent import settings
from .test_plugin import make_plugin
from .test_semantic_tools import public_source


def call(name, **args):
    return AssistantTurn("offline", "offline", "tool_calls", "", (ToolCall(name, name, json.dumps(args)),))


class Script:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def complete(self, messages, tools, *, timeout):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        return self.turns.pop(0)


async def empty_tools():
    return ToolSetup(())


def host_context(role="neko", turn="host-turn"):
    return {"lanlan_name": role, "conversation_id": turn, "run_id": turn}


def configure(plugin, directory, models, provider=empty_tools):
    plugin.data_path = lambda name: directory / name
    plugin._agent_settings = settings()
    plugin._reports = ReportStore(directory / "reports")
    plugin._bridge = SimpleNamespace(
        session=SessionDescriptor("colony-A", "load-1", 10, 1), stop=lambda: True, running=True, connection_key="connection"
    )
    plugin._agent_audit = AgentAuditRegistry()

    async def configured_tools():
        setup = await provider()
        if setup.directory is not None:
            return setup

        async def unused_query(_):
            raise AssertionError("Persistence fixture must not issue an unasked game query")

        assert not setup.tools, "Caller capabilities must belong to the prepared session"
        return make_game_tools(public_source(), unused_query)

    plugin._agent_runtime = AgentRuntime(

        tools_provider=configured_tools,
        model_factory=lambda _: models.pop(0),
        audit_registry=plugin._agent_audit,
        logger=plugin.logger,
        report_sink=plugin._report_service.save,

    )

    async def export(**kwargs):
        return {"success": True}

    plugin.export_push = export


@pytest.fixture
async def plugin(tmp_path):
    value, _ = make_plugin({})
    yield value
    service = getattr(value, "_workspace_service", None)
    if service is not None:
        await service.close()


@pytest.mark.asyncio
async def test_host_entry_persists_messages_reports_and_sdk_delivery(plugin, tmp_path):
    first = Script([call("submit_response", answer="Initial conclusion.", detail="Full detail.")])
    second = Script([call("submit_response", answer="Follow-up conclusion.")])
    configure(plugin, tmp_path, [first, second])
    a = (await plugin.rimworld_consult(question="  Original question  ", _ctx=host_context())).value
    b = (await plugin.rimworld_consult(question="Follow up", _ctx=host_context())).value
    assert a["workspace"]["workspace_id"] == b["workspace"]["workspace_id"] == "colony-A"
    assert a["workspace"]["turn_id"] != b["workspace"]["turn_id"]
    messages = second.requests[0]["messages"]
    assert any(m.get("content") == "  Original question  " for m in messages)
    assert {"recall", "frontdesk_board", "submit_response"} <= {t["function"]["name"] for t in second.requests[0]["tools"]}
    assert "Full detail." in plugin._reports.read(a["report"]["report_id"])["full_text"]
    events = await plugin._workspace_service.call("events", "colony-A")
    assert any(e["kind"] == "agent.delivery" and e["payload"]["utterance_seq"] == int(a["workspace"]["turn_id"]) and e["payload"]["summary"] == a["summary"] for e in events)

@pytest.mark.asyncio
async def test_delivery_failure_reopens_completed_response_without_model_replay(plugin, tmp_path):
    import sqlite3

    model = Script([call("submit_response", answer="Retained response.", detail="Retained detail.")])
    configure(plugin, tmp_path, [model])
    from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService
    service = FrontdeskService(tmp_path / "workspaces" / "events.sqlite", plugin._agent_runtime, game_session=lambda: plugin._bridge.session)
    plugin._workspace_service = service
    original = service.call
    async def fail_delivery(method, *args, **kwargs):
        if method == "record" and args[4] == "agent.delivery":
            raise sqlite3.OperationalError("disk full")
        return await original(method, *args, **kwargs)
    service.call = fail_delivery
    failed = await plugin.rimworld_consult(question="question", _ctx=host_context())
    assert failed.error.code == "delivery_unavailable"
    await service.close()
    plugin._workspace_service = None
    recovered = await plugin.rimworld_consult(question="question", _ctx=host_context())
    assert recovered.value["answer"] == "Retained response.\n\nRetained detail."
    assert len(model.requests) == 1
    events = await plugin._workspace_service.call("events", "colony-A")
    assert events[-1]["kind"] == "agent.delivery" and events[-1]["payload"]["summary"] == recovered.value["summary"]

@pytest.mark.asyncio
async def test_actual_executor_records_each_stage_before_next_model_request(plugin, tmp_path):
    model = Script([call("frontdesk_board", action="read"), call("submit_response", answer="Done.")])
    configure(plugin, tmp_path, [model])
    result = await plugin.rimworld_consult(question="Read board", _ctx=host_context())
    assert result.is_ok()
    events = await plugin._workspace_service.call("events", "colony-A")
    progress = [e["payload"] for e in events if e["kind"] == "agent.progress"]
    assert [e["stage"] for e in progress if "message" in e] == ["assistant", "tool", "assistant", "tool"]
    assert len([e for e in progress if e.get("stage") == "model_request"]) == 2
    board_result = next(e for e in progress if e.get("stage") == "tool")
    assert board_result["tool_call_id"] == "frontdesk_board"
    assert board_result["tool_result"]["data"]["entries"] == []

@pytest.mark.asyncio
async def test_same_words_new_request_do_not_replay_undelivered_answer(plugin, tmp_path):
    import sqlite3

    from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService

    first = Script([call("submit_response", answer="First answer.")])
    second = Script([call("submit_response", answer="Fresh answer.")])
    configure(plugin, tmp_path, [first, second])
    service = FrontdeskService(tmp_path / "workspaces" / "events.sqlite", plugin._agent_runtime, game_session=lambda: plugin._bridge.session)
    plugin._workspace_service = service
    original = service.call
    async def fail_delivery(method, *args, **kwargs):
        if method == "record" and args[4] == "agent.delivery":
            raise sqlite3.OperationalError("disk full")
        return await original(method, *args, **kwargs)
    service.call = fail_delivery
    failed = await plugin.rimworld_consult(question="same", _ctx=host_context(turn="first"))
    assert failed.error.code == "delivery_unavailable"
    service.call = original
    result = await plugin.rimworld_consult(question="same", _ctx=host_context(turn="second"))
    assert result.value["answer"] == "Fresh answer."
    assert len(first.requests) == len(second.requests) == 1

@pytest.mark.asyncio
async def test_concurrent_same_request_replays_prepared_delivery_once(plugin, tmp_path):
    import asyncio

    first_started, release = asyncio.Event(), asyncio.Event()
    class BlockingScript(Script):
        async def complete(self, messages, tools, *, timeout):
            first_started.set()
            await release.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = BlockingScript([call("submit_response", answer="One response.")])
    configure(plugin, tmp_path, [model])
    exports = []
    async def export(**kwargs):
        exports.append(kwargs)
        return {"success": True}
    plugin.export_push = export
    first = asyncio.create_task(plugin.rimworld_consult(question="same", _ctx=host_context(turn="same-run")))
    await asyncio.wait_for(first_started.wait(), 2)
    second = asyncio.create_task(plugin.rimworld_consult(question="same", _ctx=host_context(turn="same-run")))
    release.set()
    a, b = await asyncio.gather(first, second)
    assert a.value == b.value and len(model.requests) == 1 and len(exports) == 1
    events = await plugin._workspace_service.call("events", "colony-A")
    assert sum(e["kind"] == "agent.delivery" for e in events) == 1


@pytest.mark.asyncio
async def test_same_request_after_load_epoch_change_does_not_replay_old_world(plugin, tmp_path):
    first = Script([call("submit_response", answer="Old world.")])
    second = Script([call("submit_response", answer="New world.")])
    configure(plugin, tmp_path, [first, second])
    old = await plugin.rimworld_consult(question="same", _ctx=host_context(turn="request"))
    plugin._bridge.session = SessionDescriptor("colony-A", "load-2", 20, 2)
    new = await plugin.rimworld_consult(question="same", _ctx=host_context(turn="request"))
    assert old.value["answer"] == "Old world." and new.value["answer"] == "New world."
    assert len(first.requests) == len(second.requests) == 1

@pytest.mark.asyncio
async def test_concurrent_same_request_recovers_failed_delivery_without_second_model(plugin, tmp_path):
    import asyncio
    import sqlite3

    from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService

    started, release = asyncio.Event(), asyncio.Event()
    class BlockingScript(Script):
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await release.wait()
            return await super().complete(messages, tools, timeout=timeout)
    model = BlockingScript([call("submit_response", answer="Retained.")])
    configure(plugin, tmp_path, [model])
    service = FrontdeskService(tmp_path / "workspaces" / "events.sqlite", plugin._agent_runtime, game_session=lambda: plugin._bridge.session)
    plugin._workspace_service = service
    original = service.call
    failed = False
    async def fail_once(method, *args, **kwargs):
        nonlocal failed
        if method == "record" and args[4] == "agent.delivery" and not failed:
            failed = True
            raise sqlite3.OperationalError("disk unavailable")
        return await original(method, *args, **kwargs)
    service.call = fail_once
    first = asyncio.create_task(plugin.rimworld_consult(question="same", _ctx=host_context(turn="request")))
    await asyncio.wait_for(started.wait(), 2)
    second = asyncio.create_task(plugin.rimworld_consult(question="same", _ctx=host_context(turn="request")))
    release.set()
    a, b = await asyncio.gather(first, second)
    assert a.error.code == "delivery_unavailable" and b.value["answer"] == "Retained."
    assert len(model.requests) == 1

@pytest.mark.asyncio
async def test_colony_change_while_waiting_for_lock_rejects_original_request(plugin, tmp_path):
    import asyncio

    model = Script([call("submit_response", answer="Unexpected.")])
    configure(plugin, tmp_path, [model])
    lock = asyncio.Lock()
    await lock.acquire()
    plugin._consultations._colony_locks = {"colony-A": lock}
    task = asyncio.create_task(plugin.rimworld_consult(question="same", _ctx=host_context(turn="request")))
    await asyncio.sleep(0)
    plugin._bridge.session = SessionDescriptor("colony-B", "load-2", 20, 2)
    lock.release()
    result = await task
    assert result.error.code == "world_changed" and not model.requests
    assert plugin._workspace_service is None
