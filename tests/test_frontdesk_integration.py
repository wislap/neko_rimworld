"""The frontdesk journal is the durable source for session and panel views."""

from types import SimpleNamespace

import pytest

from ..workspace.runtime import FrontdeskService
from ..workspace.views import WorkspaceViews


class Runtime:
    def __init__(self):
        self.sessions = []

    async def execute(self, session, settings):
        self.sessions.append(session)
        session.messages.append({"role": "assistant", "content": "answer"})
        return {"status": "ok", "answer": "complete", "observations": [{"observation_id": "o", "response": {"value": 7}}]}


@pytest.mark.asyncio
async def test_frontdesk_reopens_with_original_words_and_colony_isolation(tmp_path):
    path = tmp_path / "events.sqlite"
    game = SimpleNamespace(colony_id="colony-a", load_epoch="epoch", game_tick=12)
    runtime = Runtime()
    service = FrontdeskService(path, runtime, game_session=lambda: game)
    first = await service.consult(game, "  exact words\n  ", object())
    await service.close()
    service = FrontdeskService(path, runtime, game_session=lambda: game)
    await service.consult(game, "next", object())
    assert runtime.sessions[-1].messages[1]["content"] == "  exact words\n  "
    assert runtime.sessions[-1].messages[2]["name"] == "historical_narrative"
    assert '"text":"answer"' in runtime.sessions[-1].messages[2]["content"]
    game.colony_id = "colony-b"
    await service.consult(game, "different colony", object())
    assert [m["content"] for m in runtime.sessions[-1].messages if m["role"] == "user" and m.get("name") != "colony_board"] == ["different colony"]
    await service.close()
    views = WorkspaceViews(path)
    turn = views.read("turn", workspace_id="colony-a", turn_id=first["workspace"]["turn_id"])
    assert turn["question"] == "  exact words\n  " and turn["result"]["answer"] == "complete"
    original = views.read("observation", workspace_id="colony-a", turn_id=first["workspace"]["turn_id"], observation_id="o")
    assert original["observation"]["response"]["value"] == 7
    assert views.read("list", search="exact words")["total"] == 1

@pytest.mark.asyncio
async def test_cancelled_tool_calls_are_paired_and_saved(tmp_path):
    import asyncio

    class CancelRuntime:
        async def execute(self, session, settings):
            session.messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "look", "arguments": "{}"}}]})
            raise asyncio.CancelledError

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=3)
    service = FrontdeskService(tmp_path / "events.sqlite", CancelRuntime(), game_session=lambda: game)
    with pytest.raises(asyncio.CancelledError):
        await service.consult(game, "question", object())
    events = await service.call("events", "colony")
    assert events[-1]["payload"]["result"]["status"] == "cancelled"
    assert events[-1]["payload"]["messages"][-1]["tool_call_id"] == "x"
    await service.close()


@pytest.mark.asyncio
async def test_failed_store_keeps_result_and_retries_before_next_run(tmp_path):
    import sqlite3

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=3)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    original = service.call
    attempts = 0
    async def fail_once(method, *args, **kwargs):
        nonlocal attempts
        if method == "record_run":
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("disk unavailable")
        return await original(method, *args, **kwargs)
    service.call = fail_once
    first = await service.consult(game, "first", object())
    assert first["answer"] == "complete" and not first["workspace"]["history_saved"]
    await service.consult(game, "next", object())
    assert len(runtime.sessions) == 2 and attempts == 3
    assert runtime.sessions[-1].messages[1]["content"] == "first"
    await service.close()

@pytest.mark.asyncio
async def test_request_and_local_input_limits_stop_without_extra_model_calls():
    from dataclasses import replace

    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, ScriptedModel, settings, turn

    for policy, expected_calls, error in [
        (replace(settings(), max_model_calls=1), 1, "agent_request_budget"),
        (replace(settings(), max_input_tokens=1), 0, "agent_input_budget"),
    ]:
        model = ScriptedModel([turn("unfinished")])
        runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(), model_factory=lambda _: model, tools_provider=semantic_tool_provider())
        result = await runtime.run("question", policy)
        assert len(model.requests) == expected_calls
        assert result["audit"]["error_code"] == error

@pytest.mark.asyncio
async def test_old_open_intent_and_user_words_are_attached(tmp_path):
    import json

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.consult(game, "Preserve my colony", object())
    capability = service.capabilities(game, 1)[0]
    await capability.handler({"action": "write_intent", "entry": {"entry_id": "intent", "expected_revision": 0, "status": "open", "priority": 5, "review_at_tick": None, "body": "Preserve colony"}})
    game.game_tick = 200_000
    await service.consult(game, "What now?", object())
    attached = next(m["content"] for m in runtime.sessions[-1].messages if "Persistent colony board" in m.get("content", ""))
    context = json.loads(attached.split(": ", 1)[1])
    assert context["pinned_utterances"][0]["text"] == "Preserve my colony"
    assert context["board"][0]["entry_id"] == "intent"
    await service.close()


@pytest.mark.asyncio
async def test_restart_marks_unfinished_utterance_without_rerunning(tmp_path):
    from ..workspace.journal import Journal

    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    journal.record_utterance("colony", "epoch", 4, "unfinished")
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=5)
    runtime = Runtime()
    service = FrontdeskService(path, runtime, game_session=lambda: game)
    await service._recover_interrupted("colony")
    event = (await service.call("events", "colony"))[-1]
    assert event["payload"]["result"]["reason"] == "process_interrupted"
    assert not runtime.sessions
    await service.close()

@pytest.mark.asyncio
async def test_same_colony_serializes_and_later_run_sees_first(tmp_path):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    class BlockingRuntime(Runtime):
        async def execute(self, session, settings):
            self.sessions.append(session)
            if len(self.sessions) == 1:
                started.set()
                await release.wait()
            session.messages.append({"role": "assistant", "content": "done"})
            return {"status": "ok", "answer": "done", "observations": []}
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=3)
    runtime = BlockingRuntime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    first = asyncio.create_task(service.consult(game, "one", object()))
    await started.wait()
    second = asyncio.create_task(service.consult(game, "two", object()))
    await asyncio.sleep(0)
    assert len(runtime.sessions) == 1
    release.set()
    await asyncio.gather(first, second)
    assert any(m.get("content") == "one" for m in runtime.sessions[1].messages)
    assert any(m.get("content") == "done" for m in runtime.sessions[1].messages)
    await service.close()

@pytest.mark.asyncio
async def test_interrupted_progress_recovers_original_tool_and_observation(tmp_path):
    import json

    from ..workspace.journal import Journal
    from ..workspace.session_view import recall

    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    seq = journal.record_utterance("colony", "epoch", 4, "question")
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "function": {"name": "look", "arguments": "{}"}}]}
    journal.record("colony", "epoch", 4, "frontdesk", "agent.progress", {"utterance_seq": seq, "stage": "assistant", "message": call})
    journal.record("colony", "epoch", 4, "frontdesk", "agent.progress", {"utterance_seq": seq, "stage": "tool", "message": {"role": "tool", "tool_call_id": "c", "content": "pruned"}, "tool_call_id": "c", "tool_result": {"full": "original"}, "observation": {"observation_id": "o", "raw": 42}})
    journal.close()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=5)
    service = FrontdeskService(path, Runtime(), game_session=lambda: game)
    await service._recover_interrupted("colony")
    events = await service.call("events", "colony")
    run = events[-1]
    assert run["payload"]["result"]["observations"][0]["raw"] == 42
    raw = json.loads(recall([run], run["seq"], "c", progress=await service.call("progress", "colony", seq)))
    assert raw == {"tool_result": {"full": "original"}, "observation": {"observation_id": "o", "raw": 42}}
    await service.close()

@pytest.mark.asyncio
async def test_journal_work_is_serial_and_does_not_block_event_loop(tmp_path):
    import asyncio
    import threading

    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", Runtime(), game_session=lambda: game)
    await service.call("events", "colony")
    entered, release = threading.Event(), threading.Event()
    original = service.journal.events
    def slow(colony):
        entered.set()
        assert release.wait(2)
        return original(colony)
    service.journal.events = slow
    pending = asyncio.create_task(service.call("events", "colony"))
    await asyncio.to_thread(entered.wait, 1)
    marker = asyncio.Event()
    asyncio.get_running_loop().call_soon(marker.set)
    await asyncio.wait_for(marker.wait(), 0.2)
    release.set()
    await pending
    await service.close()

@pytest.mark.asyncio
async def test_retry_attempt_obeys_same_model_call_budget(monkeypatch):
    from dataclasses import replace

    from ..agent.model import model_requests
    from ..agent.model.model_client import ModelClientError
    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from .consultation_setup import semantic_tool_provider
    from .test_agent import FakeLogger, ScriptedModel, settings

    monkeypatch.setattr(model_requests, "MODEL_RETRY_DELAYS_SECONDS", (0, 0))
    model = ScriptedModel([ModelClientError("model_transport", "temporary")])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(), model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    result = await runtime.run("question", replace(settings(), max_model_calls=1))
    assert len(model.requests) == 1
    assert result["audit"]["error_code"] == "agent_request_budget"

@pytest.mark.asyncio
async def test_snapshot_progress_is_durable_before_first_model_call(tmp_path):
    import asyncio
    import shutil
    from dataclasses import replace

    from ..agent.runtime.audit import AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from ..game.situation import SituationProvider
    from ..workspace.journal import Journal
    from .consultation_setup import consultation_setup
    from .test_runtime_repair import LOG, settings
    from .test_situation_context import native

    started = asyncio.Event()
    class WaitingModel:
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await asyncio.Event().wait()
    game = SimpleNamespace(colony_id="2" * 32, load_epoch="3" * 32, game_tick=10)
    async def query(_):
        return native(10)
    async def provider():
        return replace(consultation_setup(), situation_provider=SituationProvider(query, lambda: game))
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=LOG, model_factory=lambda _: WaitingModel(), tools_provider=provider)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    task = asyncio.create_task(service.consult(game, "question", settings()))
    await asyncio.wait_for(started.wait(), 2)
    events = await service.call("events", game.colony_id)
    snapshot = next(e for e in events if e["kind"] == "agent.progress" and e["payload"]["stage"] == "situation")
    assert snapshot["payload"]["observation"]["response"]["game_tick"] == 10
    copy_path = tmp_path / "crash.sqlite"
    shutil.copyfile(tmp_path / "events.sqlite", copy_path)
    journal = Journal(copy_path)
    assert journal.interrupted_events(game.colony_id)[-1]["payload"]["stage"] == "situation"
    journal.close()
    recovered = FrontdeskService(copy_path, Runtime(), game_session=lambda: game)
    await recovered._recover_interrupted(game.colony_id)
    recovered_run = (await recovered.call("events", game.colony_id))[-1]
    assert recovered_run["payload"]["result"]["observations"][0]["response"]["game_tick"] == 10
    await recovered.close()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await service.close()

@pytest.mark.asyncio
async def test_close_twice_cancels_active_run_and_releases_executor(tmp_path):
    import asyncio

    started = asyncio.Event()
    class WaitingRuntime:
        async def execute(self, session, settings):
            started.set()
            await asyncio.Event().wait()
    game = SimpleNamespace(colony_id="colony", load_epoch="epoch", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", WaitingRuntime(), game_session=lambda: game)
    task = asyncio.create_task(service.consult(game, "question", object()))
    await started.wait()
    await service.close()
    await service.close()
    assert task.cancelled() and not service._tasks and service._executor._shutdown
