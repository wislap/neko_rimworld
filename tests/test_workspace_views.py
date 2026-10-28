"""Read-only event-log projections retain evidence and isolate colonies."""

import hashlib
import sqlite3

import pytest

from ..serialization import canonical
from ..workspace.ledger import EventLedger
from ..workspace.views import WorkspaceViews


def append(ledger, colony, kind, payload, tick=42):
    return ledger.append(colony, "epoch", tick, "user" if kind == "user.utterance" else "frontdesk", kind, payload)


def test_empty_and_unknown_schema_do_not_modify_files(tmp_path):
    path = tmp_path / "missing" / "work.sqlite"
    assert WorkspaceViews(path).read("list")["workspaces"] == []
    assert not path.parent.exists()
    path = tmp_path / "unknown.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE other (value TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported event ledger"):
        WorkspaceViews(path).read("list")
    assert path.read_bytes() == before


def test_list_and_work_search_pagination_and_colony_isolation(tmp_path):
    path = tmp_path / "work.sqlite"
    ledger = EventLedger(path)
    a1 = append(ledger, "a", "user.utterance", {"text": "Earlier 食物 ABC 100%"})
    append(ledger, "a", "agent.run", {"utterance_seq": a1, "messages": [], "result": {"status": "ok", "agent_text": "answer"}, "audit": None})
    a2 = append(ledger, "a", "user.utterance", {"text": "Later medicine"})
    b = append(ledger, "b", "user.utterance", {"text": "Newer 食物"})
    ledger.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    views = WorkspaceViews(path)
    assert views.read("list", limit=1)["workspaces"][0]["colony_id"] == "b"
    preferred = views.read("list", current_colony_id="a", limit=1)
    assert preferred["workspaces"][0]["colony_id"] == "a"
    assert preferred["total"] == 2 and preferred["next_offset"] == 1
    assert views.read("list", current_colony_id="a", offset=1, limit=1)["workspaces"][0]["colony_id"] == "b"
    assert views.read("list", search="食物 abc")["workspaces"][0]["colony_id"] == "a"
    assert views.read("list", search="100%", colony_only=True, current_colony_id="b")["total"] == 0
    assert views.read("list", search="FRONTDESK")["total"] == 2
    work = views.read("work", workspace_id="a", limit=1)
    assert work["total"] == 2 and work["turns"][0]["turn_id"] == str(a2) and work["next_offset"] == 1
    older = views.read("work", workspace_id="a", offset=1, limit=1)["turns"][0]
    assert older["turn_id"] == str(a1) and older["status"] == "ok" and older["created_at"]
    assert views.read("work", workspace_id="a", search="食物 abc")["total"] == 1
    with pytest.raises(ValueError, match="exchange"):
        views.read("turn", workspace_id="a", turn_id=str(b))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_raw_evidence_delivery_audit_and_incomplete_turn(tmp_path):
    path = tmp_path / "work.sqlite"
    ledger = EventLedger(path)
    seq = append(ledger, "a", "user.utterance", {"text": "原文"})
    obs = {"observation_id": "one", "request": {"operation": "look"}, "response": {"payload": {"status": "partial", "data": {"flag": False, "value": None}}}}
    result = {"status": "ok", "observations": [obs], "report": {"report_id": "r"}}
    append(ledger, "a", "agent.run", {"utterance_seq": seq, "messages": [], "result": result, "audit": {"events": [{"n": 1}, {"n": 2}]}})
    delivery = {"utterance_seq": seq, "summary": "summary", "delivery": {}, "report": result["report"], "meaning": "prepared"}
    append(ledger, "a", "agent.delivery", delivery)
    incomplete = append(ledger, "a", "user.utterance", {"text": "pending"})
    append(ledger, "a", "agent.run", {"utterance_seq": incomplete, "messages": [], "result": None, "audit": None})
    ledger.close()
    views = WorkspaceViews(path)
    turn = views.read("turn", workspace_id="a", turn_id=str(seq))
    assert turn["question"] == "原文" and turn["result"] == result and turn["handoff"] == delivery
    assert turn["history_saved"] and turn["audit_available"] and turn["game_tick"] == 42
    evidence = views.read("observation", workspace_id="a", turn_id=str(seq), observation_id="one")
    assert evidence["observation"] == obs and evidence["historical"] and not evidence["current_observation"]
    assert evidence["sha256"] == hashlib.sha256(canonical(obs).encode()).hexdigest() == turn["evidence"][0]["sha256"]
    audit = views.read("audit", workspace_id="a", turn_id=str(seq), offset=1, limit=1)
    assert audit["events"] == [{"n": 2}] and audit["events_page"]["total"] == 2
    pending = views.read("turn", workspace_id="a", turn_id=str(incomplete))
    assert pending["status"] == "running" and pending["result"] is None and pending["handoff"] is None
    with pytest.raises(ValueError, match="audit"):
        views.read("audit", workspace_id="a", turn_id=str(incomplete))


def test_list_and_work_do_not_decode_unrelated_run_payloads(tmp_path, monkeypatch):
    path = tmp_path / "work.sqlite"
    ledger = EventLedger(path)
    selected = append(ledger, "a", "user.utterance", {"text": "chosen"})
    append(ledger, "a", "agent.run", {"utterance_seq": selected, "messages": [{"large": "secret model input"}], "result": {"status": "ok"}, "audit": {"secret": "audit"}})
    other = append(ledger, "b", "user.utterance", {"text": "other"})
    append(ledger, "b", "agent.run", {"utterance_seq": other, "messages": [{"large": "secret model input"}], "result": {"status": "ok"}, "audit": {"secret": "audit"}})
    ledger.close()
    import json
    original = json.loads
    def guarded(value, *args, **kwargs):
        assert "secret model input" not in value and "audit" not in value
        return original(value, *args, **kwargs)
    monkeypatch.setattr(json, "loads", guarded)
    views = WorkspaceViews(path)
    assert views.read("list", colony_only=True, current_colony_id="a")["total"] == 1
    assert views.read("work", workspace_id="a", limit=1)["turns"][0]["status"] == "ok"


@pytest.mark.parametrize("kwargs", [{"offset": True}, {"offset": 2**63}, {"limit": 0}, {"colony_only": 1}, {"search": None}, {"turn_id": "01"}])
def test_invalid_inputs_are_rejected(tmp_path, kwargs):
    path = tmp_path / "work.sqlite"
    ledger = EventLedger(path)
    append(ledger, "a", "user.utterance", {"text": "hi"})
    ledger.close()
    with pytest.raises(ValueError):
        WorkspaceViews(path).read("turn", workspace_id="a", **kwargs)


def test_event_queries_use_indexes_for_kind_and_utterance_reference(tmp_path):
    path = tmp_path / "work.sqlite"
    ledger = EventLedger(path)
    seq = append(ledger, "a", "user.utterance", {"text": "chosen"})
    append(ledger, "a", "agent.run", {"utterance_seq": seq, "messages": [], "result": None, "audit": None})
    ledger.close()
    with sqlite3.connect(path) as db:
        for kind in ("agent.run", "agent.delivery"):
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM events WHERE colony_id=? AND kind=? "
                "AND json_extract(payload,'$.utterance_seq')=? ORDER BY seq DESC LIMIT 1",
                ("a", kind, seq),
            ).fetchall()
            details = " ".join(row[3] for row in plan)
            assert "SEARCH" in details and "events_utterance_reference" in details
            assert "<expr>=?" in details
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT colony_id, MAX(seq) FROM events WHERE kind=? GROUP BY colony_id",
            ("user.utterance",),
        ).fetchall()
        assert any("SEARCH" in row[3] and "events_kind_colony_sequence" in row[3] for row in plan)
