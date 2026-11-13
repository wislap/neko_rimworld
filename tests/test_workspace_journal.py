"""Event log and board revisions are append-only, fixed in shape, author-owned and rebuildable from the log."""

import sqlite3

import pytest
from plugin.plugins.neko_rimworld.workspace.board import (
    AuthorViolation,
    BoardError,
    RevisionConflict,
    current_board,
    review_marks,
)
from plugin.plugins.neko_rimworld.workspace.field_takeovers import open_takeovers
from plugin.plugins.neko_rimworld.workspace.journal import Journal

COLONY = "colony-1"
# Mod load epochs are GUID strings (Protocol.cs load_epoch).
E0 = "0" * 32
E1 = "1" * 32
TAKEOVER = {"object": "warehouse-1", "field": "storage", "value_read": "freezer", "ai_value": "shelf", "changeset_id": "cs-1"}


def row(entry_id, kind, status, *, expected=0, source=None, parent=None, evidence=(), changesets=(), body="正文"):
    return {
        "entry_id": entry_id,
        "expected_revision": expected,
        "kind": kind,
        "status": status,
        "priority": 1,
        "parent": parent,
        "source": {} if source is None else source,
        "evidence": list(evidence),
        "review_at_tick": None,
        "changesets": list(changesets),
        "body": body,
    }


def seed(journal):
    """Utterance, intent, strategy, a committed task, a takeover, then a load: one of each kind."""
    utterance = journal.record_utterance(COLONY, E0, 5, "建个冷库")
    journal.submit(COLONY, E0, 6, "frontdesk", row("intent-1", "intent", "open", source={"event_seq": utterance}))
    journal.submit(COLONY, E0, 7, "planner", row("strategy-1", "strategy", "drafting"))
    journal.submit(
        COLONY,
        E0,
        8,
        "planner",
        row("task-1", "task", "committed", parent="intent-1", source={"entry_id": "intent-1"}, changesets=["cs-1"]),
    )
    takeover = journal.record_takeover(COLONY, E0, 9, TAKEOVER)
    journal.record_load(COLONY, E1, 10)
    return utterance, takeover


@pytest.fixture
def journal(tmp_path):
    value = Journal(tmp_path / "ledger.sqlite")
    yield value
    value.close()


def test_triggers_reject_update_and_delete_on_both_tables(journal):
    seed(journal)
    before = journal._ledger.db.execute("SELECT count(*) FROM events").fetchone()[0]
    for statement in (
        "UPDATE events SET kind='changed'",
        "DELETE FROM events",
        "UPDATE board_revisions SET body='changed'",
        "DELETE FROM board_revisions",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            journal._ledger.db.execute(statement)
    assert journal._ledger.db.execute("SELECT count(*) FROM events").fetchone()[0] == before
    assert journal.board(COLONY)[0]["body"] == "正文"


def test_derived_views_replay_from_raw_rows_into_a_fresh_file(tmp_path):
    source = Journal(tmp_path / "source.sqlite")
    seed(source)
    raw_events = [dict(row) for row in source._ledger.db.execute("SELECT * FROM events ORDER BY seq")]
    raw_revisions = [dict(row) for row in source._ledger.db.execute("SELECT * FROM board_revisions")]
    expected = (source.board(COLONY), source.takeovers(COLONY), source.review_marks(COLONY))
    source.close()

    replica = Journal(tmp_path / "replica.sqlite")
    for event in raw_events:
        replica._ledger.db.execute("INSERT INTO events VALUES (:seq,:colony_id,:load_epoch,:game_tick,:wall_time,"
                                   ":source,:kind,:payload)", event)
    for revision in reversed(raw_revisions):
        replica._ledger.db.execute("INSERT INTO board_revisions VALUES (:entry_id,:revision,:colony_id,:event_seq,"
                                   ":kind,:author,:status,:priority,:parent,:source,:evidence,:review_at_tick,"
                                   ":changesets,:body)", revision)
    rebuilt = (replica.board(COLONY), replica.takeovers(COLONY), replica.review_marks(COLONY))
    tables = {
        name
        for (name,) in replica._ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not name.startswith("sqlite_")
    }
    replica.close()

    assert rebuilt == expected
    assert tables == {"events", "board_revisions"}
    assert [entry["entry_id"] for entry in expected[0]] == ["intent-1", "strategy-1", "task-1"]
    assert [event["kind"] for event in expected[1]] == ["user.takeover"]
    assert expected[2] == {"intent-1": ["load"], "strategy-1": ["load"], "task-1": ["load", "takeover"]}


def test_derived_views_are_pure_functions_of_the_rows(journal):
    seed(journal)
    events, revisions = journal.events(COLONY), journal._ledger.revisions(COLONY)
    assert [event["load_epoch"] for event in events] == [E0] * 5 + [E1]
    assert current_board(list(reversed(revisions))) == journal.board(COLONY)
    assert open_takeovers(events) == journal.takeovers(COLONY)
    before_load = [event for event in events if event["kind"] != "colony.load"]
    assert review_marks(revisions, before_load) == {"task-1": ["takeover"]}
    assert review_marks(revisions, events) == journal.review_marks(COLONY)


def test_returned_takeover_leaves_the_list_and_cannot_be_returned_twice(journal):
    utterance, takeover = seed(journal)
    release = journal.record_return(COLONY, E1, 11, takeover_seq=takeover, utterance_seq=utterance)
    assert journal.takeovers(COLONY) == []
    assert release > takeover
    with pytest.raises(BoardError, match="already released"):
        journal.record_return(COLONY, E1, 12, takeover_seq=takeover, utterance_seq=utterance)


def test_new_revision_clears_only_the_marks_it_addresses(journal):
    seed(journal)
    journal.submit(
        COLONY,
        E1,
        12,
        "planner",
        row("task-1", "task", "verified", expected=1, parent="intent-1", source={"entry_id": "intent-1"},
            changesets=["cs-1"], evidence=["e-9"]),
    )
    assert journal.review_marks(COLONY) == {"intent-1": ["load"], "strategy-1": ["load"]}


def test_stale_revision_is_rejected_and_writes_nothing(journal):
    seed(journal)
    events_before = journal._ledger.db.execute("SELECT count(*) FROM events").fetchone()[0]
    with pytest.raises(RevisionConflict):
        journal.submit(COLONY, E1, 13, "planner", row("task-1", "task", "decided", expected=0,
                                                     parent="intent-1", source={"entry_id": "intent-1"}))
    assert journal._ledger.db.execute("SELECT count(*) FROM events").fetchone()[0] == events_before
    assert journal.board(COLONY)[2]["revision"] == 1
    journal.submit(COLONY, E1, 14, "planner", row("task-1", "task", "decided", expected=1,
                                                 parent="intent-1", source={"entry_id": "intent-1"}))
    with pytest.raises(RevisionConflict):
        journal.submit(COLONY, E1, 15, "planner", row("task-1", "task", "closed", expected=1,
                                                     parent="intent-1", source={"entry_id": "intent-1"}))


def test_only_the_author_writes_an_entry(journal):
    utterance, _ = seed(journal)
    rejected = [
        ("planner", row("intent-1", "intent", "open", expected=1, source={"event_seq": utterance})),
        ("frontdesk", row("task-1", "task", "decided", expected=1, parent="intent-1",
                          source={"entry_id": "intent-1"})),
        ("frontdesk", row("intent-2", "intent", "decided", source={"event_seq": utterance})),
        ("frontdesk", row("strategy-2", "strategy", "open")),
    ]
    for actor, submission in rejected:
        with pytest.raises(AuthorViolation):
            journal.submit(COLONY, E1, 13, actor, submission)
    with pytest.raises(AuthorViolation, match="may not set status"):
        journal.submit(COLONY, E1, 13, "frontdesk", row("intent-1", "intent", "verified", expected=1,
                                                       source={"event_seq": utterance}, evidence=["e-1"]))
    assert [entry["revision"] for entry in journal.board(COLONY)] == [1, 1, 1]


def test_skeleton_and_reference_rules_reject_bad_submissions(journal):
    _, takeover = seed(journal)
    bad = [
        (row("task-2", "task", "committed", parent="intent-1", source={"entry_id": "intent-1"}), "names its changesets"),
        (row("task-2", "task", "verified", parent="intent-1", source={"entry_id": "intent-1"}), "verification evidence"),
        (row("task-2", "task", "open", parent="missing", source={"entry_id": "intent-1"}), "strategy or intent"),
        (row("intent-2", "intent", "open", source={"event_seq": takeover}), "cites a user utterance"),
    ]
    for submission, message in bad:
        with pytest.raises(BoardError, match=message):
            journal.submit(COLONY, E1, 13, "planner" if submission["kind"] == "task" else "frontdesk", submission)
    with pytest.raises(BoardError, match="fixed skeleton"):
        journal.submit(COLONY, E1, 13, "planner", {**row("task-3", "task", "open"), "extra": 1})
    assert [entry["entry_id"] for entry in journal.board(COLONY)] == ["intent-1", "strategy-1", "task-1"]


def test_generic_events_are_recorded_and_reserved_kinds_are_refused(journal):
    seq = journal.record(COLONY, E0, 3, "plugin", "game.push", {"tick": 3})
    assert journal.events(COLONY)[0]["seq"] == seq
    with pytest.raises(ValueError, match="written by their own operation"):
        journal.record(COLONY, E0, 3, "plugin", "user.takeover", TAKEOVER)
    with pytest.raises(ValueError, match="A takeover records"):
        journal.record_takeover(COLONY, E0, 3, {"object": "x"})


def test_failed_revision_insert_rolls_back_its_event(journal, monkeypatch):
    seed(journal)
    count = "SELECT count(*) FROM events"
    events_before = journal._ledger.db.execute(count).fetchone()[0]

    def fail(*_args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(journal._ledger, "add_revision", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        journal.submit(COLONY, E1, 13, "planner", row("strategy-2", "strategy", "open"))
    assert journal._ledger.db.execute(count).fetchone()[0] == events_before
    assert [entry["entry_id"] for entry in journal.board(COLONY)] == ["intent-1", "strategy-1", "task-1"]


def test_entries_of_one_colony_are_invisible_to_another(journal):
    seed(journal)
    with pytest.raises(BoardError, match="another colony"):
        journal.submit("colony-2", E0, 1, "planner", row("strategy-1", "strategy", "open"))
    with pytest.raises(BoardError, match="another colony"):
        journal.submit("colony-2", E0, 1, "planner", row("strategy-1", "strategy", "open", expected=1))
    journal.submit("colony-2", E0, 1, "planner", row("strategy-9", "strategy", "open"))
    with pytest.raises(BoardError, match="strategy or intent on this board"):
        journal.submit("colony-2", E0, 2, "planner",
                       row("task-9", "task", "open", parent="strategy-9", source={"entry_id": "intent-1"}))
    assert [entry["entry_id"] for entry in journal.board("colony-2")] == ["strategy-9"]
    assert journal.board(COLONY)[1]["revision"] == 1


def test_a_task_cannot_hang_under_or_implement_another_task(journal):
    seed(journal)
    for parent, target in (("task-1", "intent-1"), ("intent-1", "task-1")):
        with pytest.raises(BoardError, match="strategy or intent"):
            journal.submit(COLONY, E1, 13, "planner",
                           row("task-2", "task", "open", parent=parent, source={"entry_id": target}))


@pytest.mark.parametrize(
    "change",
    [{"status": ["open"]}, {"status": {"x": 1}}, {"priority": 2**63}, {"review_at_tick": 2**64}, {"priority": True}],
)
def test_malformed_agent_json_is_a_board_error(journal, change):
    with pytest.raises(BoardError):
        journal.submit(COLONY, E0, 1, "planner", {**row("strategy-1", "strategy", "open"), **change})


def test_existing_file_without_sealing_triggers_is_refused_unchanged(tmp_path):
    path = tmp_path / "ledger.sqlite"
    Journal(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER sealed_event_delete")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="append-only triggers"):
        Journal(path)
    assert path.read_bytes() == before
