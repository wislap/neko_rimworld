"""Completion uses an epoch-local append clock and an immutable retry context."""
import copy

import pytest

from ..workspace import run_records
from ..workspace.attention_policy import load_policy
from ..workspace.cadence import project
from ..workspace.cadence_events import event_view
from ..workspace.journal import Journal


def observed(journal, epoch, tick):
    return journal._ledger.append("c", epoch, tick, "mod", "game.event", {"sample_tick":tick})


def completion(journal):
    journal.record_autoplay_activation("c", "old", 1, {"goal":"manage configs", "constraints":"observe limits", "goal_edited":True, "constraints_edited":False})
    start = journal.record("c", "old", 1, "planner", "planner.started", {})
    return ("c", "old", 10, start, start,
            {"status":"ok", "terminal":{"kind":"yield", "handoff":"saved outcome"}}, {"run_id":"stable-run"})


def test_append_clock_can_advance_without_changing_completion_identity(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        args = completion(journal)
        observed(journal, "old", 100)
        seq = journal.finish_planner(*args)
        saved = journal.event("c", seq)
        assert saved["game_tick"] == 100 and saved["payload"]["context_tick"] == 10
        observed(journal, "old", 200)
        assert journal.finish_planner(*copy.deepcopy(args)) == seq
        rows = journal.events("c")
        assert sum(e["kind"] == "planner.finished" for e in rows) == 1
        assert sum(e["kind"] == "planner.yield" for e in rows) == 1
        assert sum(e["kind"] == "planner.consumed" for e in rows) == 1
        changed = list(copy.deepcopy(args))
        changed[2] = 11
        with pytest.raises(ValueError, match="context"):
            journal.finish_planner(*changed)
    finally:
        journal.close()


def test_old_result_is_saved_after_load_without_scheduling_old_work(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        args = completion(journal)
        observed(journal, "old", 120001)
        journal.record_load("c", "new", 5)
        seq = journal.finish_planner(*args)
        assert journal.event("c", seq)["load_epoch"] == "old"
        assert journal.event("c", seq)["game_tick"] == 120001
        assert not any(e["kind"] == "cadence.review_due" for e in journal.events("c"))
        observed(journal, "new", 500)
        assert journal.finish_planner(*args) == seq
        before = journal.event_highwater("c")
        changed = list(args)
        changed[1] = "new"
        with pytest.raises(ValueError, match="scope"):
            journal.finish_planner(*changed)
        assert journal.event_highwater("c") == before
    finally:
        journal.close()


def test_business_clock_does_not_change_raw_fact_or_progress_ticks(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        journal.record_autoplay_activation("c", "old", 1, {"goal":"manage configs", "constraints":"observe limits", "goal_edited":True, "constraints_edited":False})
        observed(journal, "old", 100)
        progress = journal.record("c", "old", 2, "planner", "planner.progress", {"sample_tick":2})
        started = journal.record("c", "old", 3, "planner", "planner.started", {})
        assert journal.event("c", progress)["game_tick"] == 2
        assert journal.event("c", started)["game_tick"] == 100
        assert event_view(journal.events("c")) == journal._ledger.cadence_events("c")
        policy, _ = load_policy()
        project(journal._ledger.cadence_events("c"), epoch="old", now_tick=100, policy=policy)
        corrupted = journal._ledger.cadence_events("c")
        corrupted[-1] = {**corrupted[-1], "game_tick":0}
        with pytest.raises(ValueError, match="ordered"):
            project(corrupted, epoch="old", now_tick=100, policy=policy)
    finally:
        journal.close()


def test_cadence_projection_failure_rolls_back_completion_group(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        args = completion(journal)
        before = journal.event_highwater("c")
        def fail(*args):
            raise RuntimeError("injected projection failure")
        monkeypatch.setattr(run_records, "append_cadence", fail)
        with pytest.raises(RuntimeError, match="injected"):
            journal.finish_planner(*args)
        assert journal.event_highwater("c") == before
    finally:
        journal.close()


def test_first_completion_cannot_claim_a_foreign_start_scope(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        args = list(completion(journal))
        args[1] = "new"
        before = journal.event_highwater("c")
        with pytest.raises(ValueError, match="scope"):
            journal.finish_planner(*args)
        assert journal.event_highwater("c") == before
    finally:
        journal.close()


def test_existing_completion_without_context_field_remains_idempotent(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        args = completion(journal)
        _, epoch, tick, start, cursor, result, audit = args
        journal._ledger.append("c", epoch, tick, "planner", "planner.yield", {"start_seq":start, "handoff":"saved outcome"})
        seq = journal._ledger.append("c", epoch, tick, "planner", "planner.finished", {"start_seq":start, "result":result, "audit":audit})
        journal._ledger.append("c", epoch, tick, "planner", "planner.consumed", {"through_seq":cursor})
        observed(journal, "old", 200)
        assert journal.finish_planner(*args) == seq
        assert "context_tick" not in journal.event("c", seq)["payload"]
    finally:
        journal.close()
