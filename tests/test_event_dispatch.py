import pytest

from ..workspace.journal import Journal
from .test_attention_journal import CHOICES, native


def test_heartbeat_persists_without_dispatch_but_daily_still_dispatches(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    quiet = journal.ingest_game_event("c", "e", 1, "conn", native(1, 1, "heartbeat", {}))
    assert quiet and quiet.event_seq
    assert not quiet.planner_wake and not quiet.frontdesk_wake
    daily = journal.ingest_game_event("c", "e", 60000, "conn", native(2, 60000, "heartbeat", {}))
    assert daily.planner_wake and not daily.frontdesk_wake
    duplicate = journal.ingest_game_event("c", "e", 60000, "conn", native(2, 60000, "heartbeat", {}))
    assert not duplicate and not duplicate.planner_wake and not duplicate.frontdesk_wake
    journal.close()


def test_ingestion_returns_committed_interrupt_and_choice_dispatch(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    signal = native(1, 10, "letter", {"def": "ThreatBig", "is_choice_letter": True})
    receipt = journal.ingest_game_event("c", "e", 10, "conn", signal)
    assert receipt.planner_wake and receipt.frontdesk_wake
    assert journal.event("c", receipt.event_seq)["payload"]["event"] == signal
    journal.close()


def test_ingestion_failure_does_not_commit_raw_cursor_or_dispatch(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    before = journal.events("c")
    original = journal._ledger.append
    def fail(colony, epoch, tick, source, kind, payload):
        if kind == "planner.wake":
            raise RuntimeError("injected transactional failure")
        return original(colony, epoch, tick, source, kind, payload)
    monkeypatch.setattr(journal._ledger, "append", fail)
    with pytest.raises(RuntimeError, match="transactional"):
        journal.ingest_game_event("c", "e", 60000, "conn", native(1, 60000, "heartbeat", {}))
    assert journal.events("c") == before
    assert journal.game_event_cursor("c", "e", "conn") == 0
    journal.close()


def test_finished_metadata_excludes_a_completion_after_captured_watermark(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    start = journal.record("c", "e", 1, "planner", "planner.started", {"wake_seqs": [1]})
    highwater = journal.event_highwater("c")
    journal.finish_planner("c", "e", 2, start, 1,
                           {"terminal": {"kind": "yield", "handoff": "done"}}, {})
    assert journal.planner_finished_metadata("c", through_seq=highwater) == []
    assert journal.planner_finished_metadata("c")[0]["terminal_kind"] == "yield"
    journal.close()
