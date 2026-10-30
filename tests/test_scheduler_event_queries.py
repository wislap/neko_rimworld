"""Targeted scheduler reads retain event ordering and recovery data."""
from plugin.plugins.neko_rimworld.workspace.ledger import EventLedger
from plugin.plugins.neko_rimworld.workspace.session_view import anchor_seq


def test_targeted_events_and_metadata(tmp_path):
    ledger = EventLedger(tmp_path / "events.sqlite")
    def append(colony, epoch, kind, payload, tick=1):
        return ledger.append(colony, epoch, tick, "plugin", kind, payload)
    first = append("a", "old", "frontdesk.wake", {"trigger_seq": 9})
    append("b", "new", "frontdesk.wake", {"trigger_seq": 9})
    last = append("a", "new", "frontdesk.wake", {"utterance_seq": 10}, 5)
    append("a", "new", "planner.finished", {"start_seq": 7, "result": {"terminal": {"kind": "yield"}, "audit": "x" * 10000}})
    append("a", "old", "agent.run", {"trigger_seq": 9, "result": {"status": "ok", "terminal": {"kind": "reply"}, "detail": [True, False, None, {"nested": [0, {"flag": True, "empty": None}]}]}, "audit": "x" * 10000}, 8)
    append("a", "new", "agent.run", {"trigger_seq": 9, "result": {"status": "failed"}})
    for value in (None, False, 0):
        append("a", "new", "frontdesk.wake", {"trigger_seq": value, "utterance_seq": 9})
        append("a", "new", "agent.run", {"trigger_seq": value, "utterance_seq": 9, "result": {"status": "ok", "marker": "wrong"}})
    final = append("a", "new", "unrelated", {"blob": "x" * 10000})
    all_events = ledger.events("a")
    assert ledger.event_highwater("a") == final
    assert ledger.event_highwater("missing") == 0
    assert ledger.events_by_kind("a", ["frontdesk.wake"]) == [e for e in all_events if e["kind"] == "frontdesk.wake"]
    assert [e["seq"] for e in ledger.events_by_kind("a", ["frontdesk.wake"], after_seq=first, load_epoch="new", anchor=10)] == [last]
    expected_wakes = [e for e in all_events if e["kind"] == "frontdesk.wake" and anchor_seq(e) == 9]
    assert ledger.events_by_kind("a", ["frontdesk.wake"], anchor=9) == expected_wakes
    assert [e["seq"] for e in expected_wakes] == [first]
    assert ledger.event_ticks("a", [first, last, final, 2]) == {first: 1, last: 5, final: 1}
    assert ledger.planner_finished_metadata("a") == [{"seq": last + 1, "start_seq": 7, "terminal_kind": "yield"}]
    result = ledger.successful_run("a", 9)
    original = next(e for e in all_events if e["kind"] == "agent.run" and e["payload"]["result"]["status"] == "ok")
    assert result == {**original, "payload": {"result": original["payload"]["result"]}}
    assert ledger.successful_run("a", 9, through_seq=original["seq"] - 1) is None
    assert ledger.successful_run("a", 9, through_seq=original["seq"]) == result
    assert ledger.successful_run("a", 999) is None
    ledger.close()


def test_kind_queries_preserve_order_and_do_not_decode_unrelated_audits(tmp_path, monkeypatch):
    from plugin.plugins.neko_rimworld.workspace import ledger as ledger_module

    ledger = EventLedger(tmp_path / "events.sqlite")
    ledger.append("a", "old", 1, "plugin", "wake", {"trigger_seq": 7})
    ledger.append("a", "new", 2, "plugin", "agent.run", {"audit": "x" * 1_000_000})
    ledger.append("a", "new", 3, "plugin", "start", {"trigger_seq": 7})
    ledger.append("a", "new", 4, "plugin", "wake", {"trigger_seq": 8})
    ledger.append("b", "new", 5, "plugin", "wake", {"trigger_seq": 7})
    original = ledger.events("a")
    decode = ledger_module._event
    decoded_kinds = []
    def checked(row):
        decoded_kinds.append(row["kind"])
        assert row["kind"] != "agent.run"
        return decode(row)
    monkeypatch.setattr(ledger_module, "_event", checked)
    assert ledger.events_by_kind("a", []) == []
    assert ledger.events_by_kind("a", ["wake", "start", "wake"]) == [e for e in original if e["kind"] in {"wake", "start"}]
    assert ledger.events_by_kind("a", ["wake", "start"], after_seq=1, load_epoch="new", anchor=7) == [original[2]]
    assert ledger.events_by_kind("a", ["missing"] * 1200 + ["wake"]) == [e for e in original if e["kind"] == "wake"]
    assert set(decoded_kinds) == {"wake", "start"}
    ledger.close()


def test_interrupted_events_match_correlated_query_with_cross_epoch_and_null_anchors(tmp_path):
    from plugin.plugins.neko_rimworld.workspace.ledger import _event

    ledger = EventLedger(tmp_path / "events.sqlite")
    def append(colony, epoch, kind, payload):
        return ledger.append(colony, epoch, 1, "plugin", kind, payload)
    first = append("a", "old", "user.utterance", {"text": "first"})
    trigger = append("a", "old", "frontdesk.trigger", {"text": "trigger"})
    append("a", "new", "agent.progress", {"utterance_seq": first, "value": "completed"})
    append("a", "new", "agent.progress", {"trigger_seq": trigger, "value": "completed across epoch"})
    append("a", "new", "agent.progress", {"trigger_seq": None, "value": "no anchor"})
    append("a", "new", "agent.progress", {"trigger_seq": None, "utterance_seq": first})
    append("a", "new", "agent.progress", {"trigger_seq": "1", "value": "text anchor"})
    append("a", "new", "agent.progress", {"trigger_seq": 0, "value": "zero anchor"})
    append("a", "old", "agent.run", {"utterance_seq": first, "audit": "large" * 10000})
    append("a", "new", "agent.run", {"utterance_seq": first})
    append("a", "new", "agent.run", {"trigger_seq": trigger})
    append("a", "new", "agent.run", {"trigger_seq": None})
    append("a", "new", "agent.run", {"trigger_seq": 0})
    append("b", "new", "agent.run", {"trigger_seq": "1"})
    sql = ("SELECT e.* FROM events e WHERE e.colony_id=? AND "
           "(e.kind IN ('user.utterance', 'frontdesk.trigger') OR e.kind='agent.progress') AND NOT EXISTS "
           "(SELECT 1 FROM events r WHERE r.colony_id=e.colony_id AND r.kind='agent.run' AND "
           "coalesce(json_extract(r.payload,'$.trigger_seq'),json_extract(r.payload,'$.utterance_seq'))="
           "CASE WHEN e.kind IN ('user.utterance', 'frontdesk.trigger') THEN e.seq ELSE "
           "coalesce(json_extract(e.payload,'$.trigger_seq'),json_extract(e.payload,'$.utterance_seq')) END) ORDER BY e.seq")
    expected = [_event(row) for row in ledger.db.execute(sql, ("a",))]
    actual = ledger.interrupted_events("a")
    assert actual == expected
    assert [e["payload"]["value"] for e in actual] == ["no anchor", "text anchor"]
    assert ledger.interrupted_events("missing") == []
    ledger.close()
