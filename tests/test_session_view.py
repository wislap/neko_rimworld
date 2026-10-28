"""Frontdesk session is a view over the event log: window cuts, placeholders, recall, pinned intents and runs."""

import json
import sqlite3

import pytest

from plugin.plugins.neko_rimworld.serialization import canonical
from plugin.plugins.neko_rimworld.workspace.journal import Journal
from plugin.plugins.neko_rimworld.workspace.session_view import (
    frontdesk_messages,
    pinned_utterances,
    recall,
)

COLONY = "colony-1"
E0 = "0" * 32
WINDOW = 180_000
NOW = 250_000
OLD_TICK = 10
RECENT_TICK = 200_000
FOOD = '{"food":12}'


def row(entry_id, kind, status, *, expected=0, source=None):
    return {
        "entry_id": entry_id,
        "expected_revision": expected,
        "kind": kind,
        "status": status,
        "priority": 1,
        "parent": None,
        "source": {} if source is None else source,
        "evidence": [],
        "review_at_tick": None,
        "changesets": [],
        "body": "正文",
    }


def assistant(calls=(), content=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for call_id, name in calls
        ]
    return message


def tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def seed(journal):
    """An old turn with a tool call, a recent turn with a plain reply, and an open intent on the old turn."""
    old = journal.record_utterance(COLONY, E0, OLD_TICK, "建个冷库")
    journal.submit(COLONY, E0, 11, "frontdesk", row("intent-1", "intent", "open", source={"event_seq": old}))
    old_run = journal.record_run(
        COLONY, E0, 12, "frontdesk", old,
        [assistant([("c1", "query_colony")]), tool("c1", FOOD), assistant(content="好的")],
    )
    recent = journal.record_utterance(COLONY, E0, RECENT_TICK, "看看食物")
    recent_run = journal.record_run(COLONY, E0, RECENT_TICK + 1, "frontdesk", recent, [assistant(content="食物12")])
    return old, old_run, recent, recent_run


@pytest.fixture
def journal(tmp_path):
    value = Journal(tmp_path / "ledger.sqlite")
    yield value
    value.close()


def count_events(journal):
    return journal._ledger.db.execute("SELECT count(*) FROM events").fetchone()[0]


def test_window_keeps_recent_turns_and_drops_older_ones_whole(journal):
    seed(journal)
    events = journal.events(COLONY)
    assert frontdesk_messages(events, now_tick=NOW) == [
        {"role": "user", "content": "看看食物"},
        {"role": "assistant", "content": "食物12"},
    ]
    everything = frontdesk_messages(events, now_tick=NOW, window_ticks=NOW + 1)
    assert [message["role"] for message in everything] == [
        "user", "assistant", "tool", "assistant", "user", "assistant",
    ]
    assert everything[0] == {"role": "user", "content": "建个冷库"}


def test_tool_results_in_window_are_placeholders_with_their_recall_address(journal):
    old, old_run, _, _ = seed(journal)
    events = journal.events(COLONY)
    tool_message = frontdesk_messages(events, now_tick=NOW, window_ticks=NOW + 1)[2]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "c1"
    assert tool_message["content"] == canonical({
        "recall": {"event_seq": old_run, "tool_call_id": "c1"},
        "tool": "query_colony",
        "game_tick": 12,
        "note": "original retained; recall by event_seq and tool_call_id",
    })
    assert FOOD not in json.dumps(tool_message, ensure_ascii=False)
    assert recall(events, old_run, "c1") == FOOD


def test_recall_returns_originals_and_refuses_anything_else(journal):
    old, old_run, recent, _ = seed(journal)
    events = journal.events(COLONY)
    assert recall(events, old) == "建个冷库"
    assert recall(events, recent) == "看看食物"
    assert recall(events, old_run, "c1") == FOOD
    for seq, call_id in ((old_run, "nope"), (old, "c1"), (999, None), (old_run, None)):
        with pytest.raises(KeyError):
            recall(events, seq, call_id)


def test_pinned_utterances_are_only_the_open_out_of_window_intents(journal):
    old, _, recent, _ = seed(journal)
    events = journal.events(COLONY)
    assert pinned_utterances(events, journal.board(COLONY), now_tick=NOW) == [{"event_seq": old, "text": "建个冷库"}]
    assert pinned_utterances(events, journal.board(COLONY), now_tick=NOW, window_ticks=NOW + 1) == []

    journal.submit(COLONY, E0, 30, "frontdesk", row("intent-2", "intent", "open", source={"event_seq": recent}))
    assert pinned_utterances(events, journal.board(COLONY), now_tick=NOW) == [{"event_seq": old, "text": "建个冷库"}]

    journal.submit(COLONY, E0, 31, "frontdesk", row("intent-1", "intent", "closed", expected=1,
                                                   source={"event_seq": old}))
    assert pinned_utterances(journal.events(COLONY), journal.board(COLONY), now_tick=NOW) == []


def test_run_is_recorded_as_one_event_with_its_messages(journal):
    old, _, _, _ = seed(journal)
    messages = [assistant([("c9", "query_colony")]), tool("c9", FOOD)]
    seq = journal.record_run(COLONY, E0, 40, "frontdesk", old, messages)
    event = journal.events(COLONY)[-1]
    assert event["seq"] == seq
    assert (event["kind"], event["source"], event["game_tick"]) == ("agent.run", "frontdesk", 40)
    assert event["payload"] == {"utterance_seq": old, "messages": messages}


@pytest.mark.parametrize(
    ("role", "seq_of", "messages", "message"),
    [
        ("planner", "old", [assistant(content="x")], "Only the frontdesk session"),
        ("frontdesk", "run", [assistant(content="x")], "answers a user utterance"),
        ("frontdesk", "missing", [assistant(content="x")], "answers a user utterance"),
        ("frontdesk", "old", [{"role": "system", "content": "x"}], "only assistant and tool"),
        ("frontdesk", "old", [{"role": "user", "content": "x"}], "only assistant and tool"),
        ("frontdesk", "old", [], "non-empty list"),
        ("frontdesk", "old", [tool("c1", "x")], "answers a tool call"),
        ("frontdesk", "old", [assistant([("c1", "f")]), assistant(content="x")], "answered before"),
        ("frontdesk", "old", [assistant([("c1", "f")]), tool("c2", "x")], "answers a tool call"),
        ("frontdesk", "old", [assistant([("c1", "f")])], "answered by a tool message"),
        ("frontdesk", "old", [assistant([("c1", "f")]), tool("c1", "x"), tool("c1", "y")], "answers a tool call"),
        ("frontdesk", "old", [assistant([("c1", "f"), ("c1", "g")]), tool("c1", "x")], "unique"),
        ("frontdesk", "old", [assistant([("c1", "")]), tool("c1", "x")], "function name"),
        ("frontdesk", "old", [assistant([("", "f")]), tool("", "x")], "function name"),
        ("frontdesk", "old", [assistant([("c1", "f")]), tool("c1", None)], "answers a tool call"),
    ],
)
def test_malformed_runs_are_refused_and_write_nothing(journal, role, seq_of, messages, message):
    old, _, _, run = seed(journal)
    seq = {"old": old, "run": run, "missing": 999}[seq_of]
    before = count_events(journal)
    with pytest.raises(ValueError, match=message):
        journal.record_run(COLONY, E0, 40, role, seq, messages)
    assert count_events(journal) == before


def test_utterance_of_another_colony_cannot_be_answered(journal):
    seed(journal)
    other = journal.record_utterance("colony-2", E0, 1, "别的殖民地")
    before = count_events(journal)
    with pytest.raises(ValueError, match="answers a user utterance"):
        journal.record_run(COLONY, E0, 40, "frontdesk", other, [assistant(content="x")])
    assert count_events(journal) == before


def test_derived_views_replay_from_raw_rows_into_a_fresh_file(tmp_path):
    source = Journal(tmp_path / "source.sqlite")
    _, old_run, _, _ = seed(source)
    raw_events = [dict(row) for row in source._ledger.db.execute("SELECT * FROM events ORDER BY seq")]
    raw_revisions = [dict(row) for row in source._ledger.db.execute("SELECT * FROM board_revisions")]
    expected = (
        frontdesk_messages(source.events(COLONY), now_tick=NOW),
        pinned_utterances(source.events(COLONY), source.board(COLONY), now_tick=NOW),
        recall(source.events(COLONY), old_run, "c1"),
    )
    source.close()

    replica = Journal(tmp_path / "replica.sqlite")
    for event in raw_events:
        replica._ledger.db.execute("INSERT INTO events VALUES (:seq,:colony_id,:load_epoch,:game_tick,:wall_time,"
                                   ":source,:kind,:payload)", event)
    for revision in reversed(raw_revisions):
        replica._ledger.db.execute("INSERT INTO board_revisions VALUES (:entry_id,:revision,:colony_id,:event_seq,"
                                   ":kind,:author,:status,:priority,:parent,:source,:evidence,:review_at_tick,"
                                   ":changesets,:body)", revision)
    rebuilt = (
        frontdesk_messages(replica.events(COLONY), now_tick=NOW),
        pinned_utterances(replica.events(COLONY), replica.board(COLONY), now_tick=NOW),
        recall(replica.events(COLONY), old_run, "c1"),
    )
    replica.close()
    assert rebuilt == expected
    assert expected[2] == FOOD


def test_each_colony_sees_only_its_own_turns_and_runs(journal):
    old, old_run, _, _ = seed(journal)
    other = journal.record_utterance("colony-2", E0, RECENT_TICK, "另一个殖民地")
    journal.record_run("colony-2", E0, RECENT_TICK, "frontdesk", other, [assistant(content="收到")])
    journal.submit("colony-2", E0, 1, "frontdesk", row("intent-9", "intent", "open", source={"event_seq": other}))

    mine = journal.events(COLONY)
    theirs = journal.events("colony-2")
    assert {"另一个殖民地", "收到"}.isdisjoint(
        message["content"] for message in frontdesk_messages(mine, now_tick=NOW, window_ticks=NOW + 1)
    )
    assert [message["content"] for message in frontdesk_messages(theirs, now_tick=NOW)] == ["另一个殖民地", "收到"]
    assert pinned_utterances(mine, journal.board(COLONY), now_tick=NOW) == [{"event_seq": old, "text": "建个冷库"}]
    assert pinned_utterances(theirs, journal.board("colony-2"), now_tick=NOW) == []
    with pytest.raises(KeyError):
        recall(mine, other)
    with pytest.raises(KeyError):
        recall(theirs, old_run, "c1")


def test_run_rows_are_sealed(journal):
    seed(journal)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        journal._ledger.db.execute("UPDATE events SET payload='{}' WHERE kind='agent.run'")
