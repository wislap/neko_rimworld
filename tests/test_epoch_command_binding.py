import asyncio
import threading
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService
from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError
from plugin.plugins.neko_rimworld.workspace.board import review_marks
from plugin.plugins.neko_rimworld.workspace.journal import Journal

from .test_event_load_sample import snapshot


def strategy(revision):
    return {"entry_id": "s", "expected_revision": revision, "kind": "strategy", "status": "drafting",
            "priority": 1, "parent": None, "source": {}, "evidence": [], "review_at_tick": None,
            "changesets": [], "body": "Plan"}


@pytest.mark.asyncio
async def test_queued_old_submit_is_rejected_after_new_load(tmp_path):
    current = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    await service.call("submit", "c", "old", 1, "planner", strategy(0))
    started, release = threading.Event(), threading.Event()
    def block():
        started.set()
        release.wait(5)
    blocking = asyncio.get_running_loop().run_in_executor(service._executor, block)
    await asyncio.to_thread(started.wait)
    current = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=2)
    load = asyncio.create_task(service.call("record_load", "c", "new", 2))
    old_submit = asyncio.create_task(service.call("submit", "c", "old", 1, "planner", strategy(1)))
    await asyncio.sleep(0)
    release.set()
    await blocking
    await load
    with pytest.raises(ToolExecutionError, match="changed") as error:
        await old_submit
    assert error.value.code == "world_changed"
    assert (await service.call("board", "c"))[0]["revision"] == 1
    await service.call("record", "c", "old", 1, "planner", "planner.progress", {"start_seq": 1})
    await service.call("submit", "c", "new", 2, "planner", strategy(1))
    assert (await service.call("board", "c"))[0]["revision"] == 2
    assert any(e["kind"] == "planner.progress" and e["load_epoch"] == "old"
               for e in await service.call("events", "c"))
    await service.close()


@pytest.mark.asyncio
async def test_queued_old_native_event_cannot_restore_previous_load(tmp_path):
    current = SimpleNamespace(colony_id="colony", load_epoch="old", game_tick=50)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    await service.call("record_load", "colony", "old", 10)
    await service.call("ingest_game_event", "colony", "old", 50, "connection", snapshot("old", 50, 10))
    started, release = threading.Event(), threading.Event()
    def block():
        started.set()
        release.wait(5)
    blocking = asyncio.get_running_loop().run_in_executor(service._executor, block)
    await asyncio.to_thread(started.wait)
    current = SimpleNamespace(colony_id="colony", load_epoch="new", game_tick=100)
    load = asyncio.create_task(service.call("observe_epoch", "colony", "new", 100))
    new_event = asyncio.create_task(service.call("ingest_game_event", "colony", "new", 100, "connection", snapshot("new", 100, 20)))
    event = snapshot("old", 51, 10)
    event["cursor"] = 2
    stale = asyncio.create_task(service.call("ingest_game_event", "colony", "old", 51, "connection", event))
    await asyncio.sleep(0)
    release.set()
    await blocking
    await load
    await new_event
    with pytest.raises(ToolExecutionError, match="changed") as error:
        await stale
    assert error.value.code == "world_changed"
    assert [e["load_epoch"] for e in await service.call("events_by_kind", "colony", ("colony.load",))] == ["old", "new"]
    await service.close()


@pytest.mark.asyncio
async def test_native_ingest_checks_connection_and_can_record_new_epoch(tmp_path):
    current = SimpleNamespace(colony_id="colony", load_epoch="old", game_tick=50)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    await service.call("record_load", "colony", "old", 10)
    current.load_epoch = "new"
    identity = ["other"]
    with pytest.raises(ToolExecutionError) as error:
        await service.call("ingest_game_event", "colony", "new", 100, "connection", snapshot("new", 100, 20),
                           command_binding_check=lambda: identity[0] == "connection")
    assert error.value.code == "world_changed"
    identity[0] = "connection"
    assert await service.call("ingest_game_event", "colony", "new", 100, "connection", snapshot("new", 100, 20),
                              command_binding_check=lambda: identity[0] == "connection")
    assert [e["load_epoch"] for e in await service.call("events_by_kind", "colony", ("colony.load",))] == ["old", "new"]
    await service.close()


def test_late_old_revision_does_not_erase_load_review(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.submit("c", "old", 1, "planner", strategy(0))
    journal.record_load("c", "new", 2)
    journal.submit("c", "old", 1, "planner", strategy(1))
    assert journal.review_marks("c")["s"] == ["load"]
    journal.submit("c", "new", 2, "planner", strategy(2))
    assert journal.review_marks("c") == {}
    journal.close()


@pytest.mark.asyncio
async def test_old_completion_remains_recordable_without_reverting_observed_epoch(tmp_path):
    current = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=1)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    await service.call("submit", "c", "old", 1, "planner", strategy(0))
    current = SimpleNamespace(colony_id="c", load_epoch="new", game_tick=2)
    await service.call("observe_epoch", "c", "new", 2)
    await service.call("record", "c", "old", 1, "planner", "planner.progress", {"start_seq": 1},
                       command_binding_check=lambda: False)
    assert await service.call("observe_epoch", "c", "new", 2) is None
    assert len(await service.call("events_by_kind", "c", ("colony.load",))) == 1
    with pytest.raises(ToolExecutionError, match="changed") as error:
        await service.call("observe_epoch", "c", "old", 1)
    assert error.value.code == "world_changed"
    with pytest.raises(ToolExecutionError, match="changed") as error:
        await service.call("record", "c", "old", 1, "plugin", "planner.wake", {"text": "late"})
    assert error.value.code == "world_changed"
    await service.close()


@pytest.mark.asyncio
async def test_frontdesk_board_queued_before_connection_change_is_rejected(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1, connection_epoch="A")
    current = SimpleNamespace(**vars(game))
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    utterance = await service.call("record_utterance", "c", "e", 1, "intent")
    tool = service.capabilities(game, utterance, {utterance}, explicit=True)[0]
    original_call = service.call
    started, release = threading.Event(), threading.Event()
    queued = asyncio.Event()
    def block():
        started.set()
        release.wait(5)
    async def queued_call(method, *args, **kwargs):
        if method == "submit":
            asyncio.get_running_loop().run_in_executor(service._executor, block)
            await asyncio.to_thread(started.wait)
            pending = asyncio.create_task(original_call(method, *args, **kwargs))
            await asyncio.sleep(0)
            queued.set()
            return await pending
        return await original_call(method, *args, **kwargs)
    service.call = queued_call
    entry = {"entry_id": "i", "expected_revision": 0, "status": "open", "priority": 1,
             "review_at_tick": None, "body": "intent"}
    task = asyncio.create_task(tool.handler({"action": "write_intent", "entry": entry}))
    await queued.wait()
    current.connection_epoch = "B"
    release.set()
    with pytest.raises(ToolExecutionError, match="changed") as error:
        await task
    assert error.value.code == "world_changed"
    assert await original_call("board", "c") == []
    await service.close()


@pytest.mark.parametrize("epoch", [None, "", 42])
def test_unknown_revision_epoch_keeps_load_review(tmp_path, epoch):
    journal = Journal(tmp_path / "events.sqlite")
    journal.submit("c", "old", 1, "planner", strategy(0))
    journal.record_load("c", "new", 2)
    journal.submit("c", "new", 2, "planner", strategy(1))
    revisions, events = journal._ledger.revisions("c"), journal.events("c")
    revision = next(e for e in events if e["seq"] == revisions[-1]["event_seq"])
    revision["load_epoch"] = epoch
    assert review_marks(revisions, events)["s"] == ["load"]
    events.remove(revision)
    assert review_marks(revisions, events)["s"] == ["load"]
    journal.close()
