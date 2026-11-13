"""Planner terminal retries preserve one original outcome after a lost acknowledgement."""
import asyncio
import copy
import threading

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService
from plugin.plugins.neko_rimworld.workspace.journal import Journal


def completion(start, cursor):
    return ("c", "old", 10, start, cursor,
            {"status": "ok", "terminal": {"kind": "yield", "handoff": "Continue from the saved board."}},
            {"run_id": "original-run"})


async def test_cancel_after_terminal_commit_then_retry_keeps_one_outcome(tmp_path, monkeypatch):
    committed, acknowledge = threading.Event(), threading.Event()
    original = Journal.finish_planner
    first = True

    def commit_before_ack(journal, *args):
        nonlocal first
        seq = original(journal, *args)
        if first:
            first = False
            committed.set()
            if not acknowledge.wait(3):
                raise TimeoutError("test acknowledgement was not released")
        return seq

    monkeypatch.setattr(Journal, "finish_planner", commit_before_ack)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: None)
    try:
        start = await service.call("record", "c", "old", 1, "planner", "planner.started", {})
        await service.call("record", "c", "old", 100, "plugin", "clock.observation", {})
        args = completion(start, start)
        task = asyncio.create_task(service.call("finish_planner", *args))
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        acknowledge.set()
        await service.call("record", "c", "old", 200, "plugin", "clock.observation", {})
        retried = await service.call("finish_planner", *args)
        events = await service.call("events", "c")
        terminals = [event for event in events if event["kind"] == "planner.finished"]
        assert len(terminals) == 1
        assert retried == terminals[0]["seq"]
        assert terminals[0]["game_tick"] == 100 and terminals[0]["payload"]["context_tick"] == 10
        assert len([event for event in events if event["kind"] == "planner.yield"]) == 1
        assert len([event for event in events if event["kind"] == "planner.consumed"]) == 1
    finally:
        acknowledge.set()
        await service.close()


def test_retry_after_reload_keeps_old_scope_and_rejects_conflicting_outcome(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        start = journal.record("c", "old", 1, "planner", "planner.started", {})
        args = completion(start, start)
        saved = journal.finish_planner(*args)
        journal.record_load("c", "new", 11)
        assert journal.finish_planner(*args) == saved
        terminal = journal.event("c", saved)
        assert terminal["load_epoch"] == "old"
        before = journal.event_highwater("c")
        conflicting = list(copy.deepcopy(args))
        conflicting[5]["terminal"]["handoff"] = "Different outcome."
        with pytest.raises(ValueError, match="conflict|different|ambiguous"):
            journal.finish_planner(*conflicting)
        assert journal.event_highwater("c") == before
    finally:
        journal.close()


@pytest.mark.parametrize("field", ["epoch", "tick", "cursor", "audit", "outcome"])
def test_same_start_cannot_replace_scope_cursor_usage_or_result(tmp_path, field):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        start = journal.record("c", "old", 1, "planner", "planner.started", {})
        args = completion(start, start)
        journal.finish_planner(*args)
        before = journal.event_highwater("c")
        changed = list(copy.deepcopy(args))
        if field == "epoch":
            changed[1] = "new"
        elif field == "tick":
            changed[2] += 1
        elif field == "cursor":
            changed[4] += 1
        elif field == "audit":
            changed[6]["usage"] = {"prompt_tokens": 42}
        else:
            changed[5]["status"] = "failed"
        with pytest.raises(ValueError, match="conflict"):
            journal.finish_planner(*changed)
        assert journal.event_highwater("c") == before
    finally:
        journal.close()


def test_idempotent_retry_compares_json_objects_without_key_order(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        start = journal.record("c", "old", 1, "planner", "planner.started", {})
        args = list(completion(start, start))
        saved = journal.finish_planner(*args)
        args[5] = dict(reversed(list(args[5].items())))
        assert journal.finish_planner(*args) == saved
    finally:
        journal.close()
