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
