import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..workspace.runtime import FrontdeskService
from .test_frontdesk_integration import Runtime


@pytest.mark.asyncio
async def test_epoch_change_records_load_once_and_warns_on_later_runs(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=500)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.consult(game, "first", object())
    game.load_epoch, game.game_tick = "new", 100
    await service.consult(game, "second", object())
    await service.consult(game, "third", object())
    loads = await service.call("events_by_kind", "c", ("colony.load",))
    assert len(loads) == 1 and loads[0]["game_tick"] == 100
    assert any(m.get("name") == "colony_load" and "tick 100" in m["content"] for m in runtime.sessions[-1].messages)
    await service.close()


@pytest.mark.asyncio
async def test_evidence_placeholder_and_recall_preserve_original(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=20)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    seq = await service.call("record_utterance", "c", "e", 10, "question")
    call = {"id": "call", "type": "function", "function": {"name": "look", "arguments": "{}"}}
    obs = {"observation_id": "evidence-1", "request": {"target": "pawn-1"}, "response": {"game_tick": 9, "value": 42}}
    await service.call("record", "c", "e", 10, "frontdesk", "agent.progress", {"utterance_seq": seq, "tool_call_id": "call", "tool_result": {"value": 42}, "observation": obs})
    await service.call("record_run", "c", "e", 10, "frontdesk", seq, [{"role": "assistant", "content": None, "tool_calls": [call]}, {"role": "tool", "tool_call_id": "call", "content": "42"}])
    await service.consult(game, "next", object())
    tool = next(m for m in runtime.sessions[-1].messages if m["role"] == "tool")
    assert '"evidence_id":"evidence-1"' in tool["content"]
    assert '"game_tick":9' in tool["content"] and '"target":"pawn-1"' in tool["content"]
    recall = next(spec for spec in runtime.sessions[-1].capabilities if spec.name == "recall")
    assert (await recall.handler({"evidence_id": "evidence-1"}))["original"] == obs
    await service.close()


@pytest.mark.asyncio
async def test_summary_precedes_run_and_preserves_original_words_and_tool_pairs(tmp_path):
    from ..agent.context import narrative
    from ..message_contracts import AssistantTurn
    from .test_resumable_run import Script, runtime_for
    from .test_runtime_repair import settings, submit

    model = Script([AssistantTurn("summary", "mock", "stop", "Discussed planning."), submit("Completed answer.")])
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=20)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime_for(model), game_session=lambda: game)
    await service.call("set_context_policy", "c", "e", 1, {"narrative_threshold": 1})
    seq = await service.call("record_utterance", "c", "e", 10, "  原话  ")
    call = {"id": "call", "type": "function", "function": {"name": "look", "arguments": "{}"}}
    run = await service.call("record_run", "c", "e", 10, "frontdesk", seq, [{"role": "assistant", "content": "Prior narrative.", "tool_calls": [call]}, {"role": "tool", "tool_call_id": "call", "content": "secret fact"}, {"role": "assistant", "content": "Prior answer."}])
    await service.consult(game, "new question", settings())
    assert len(model.requests) == 2
    assert model.requests[0]["tools"] == []
    assert "secret fact" not in str(model.requests[0]) and "  原话  " not in str(model.requests[0])
    messages = model.requests[1]["messages"]
    assert any(m.get("name") == "historical_narrative" for m in messages)
    assert {"role": "user", "content": "  原话  "} in messages
    assert any(m.get("tool_call_id") == "call" for m in messages)
    assert not any(m.get("content") == "Prior answer." for m in messages)
    summaries = await service.call("events_by_kind", "c", (narrative.SUMMARY_EVENT,))
    assert summaries[0]["payload"]["source_run_seqs"] == [run]
    assert (await service.call("event", "c", seq))["payload"]["text"] == "  原话  "
    await service.close()


@pytest.mark.asyncio
async def test_summary_counts_against_run_model_budget(tmp_path):
    from dataclasses import replace

    from ..agent.context import narrative
    from ..message_contracts import AssistantTurn
    from .test_resumable_run import Script, runtime_for
    from .test_runtime_repair import settings

    model = Script([AssistantTurn("summary", "mock", "stop", "Discussion.")])
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=20)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime_for(model), game_session=lambda: game)
    await service.call("set_context_policy", "c", "e", 1, {"narrative_threshold": 1})
    seq = await service.call("record_utterance", "c", "e", 10, "original")
    await service.call("record_run", "c", "e", 10, "frontdesk", seq, [{"role": "assistant", "content": "History."}])
    result = await service.consult(game, "next", replace(settings(), max_model_calls=1))
    assert len(model.requests) == 1
    assert result["audit"]["error_code"] == "agent_request_budget"
    assert len(await service.call("events_by_kind", "c", (narrative.SUMMARY_EVENT,))) == 1
    await service.close()


@pytest.mark.asyncio
async def test_policy_replays_and_old_epoch_summary_stays_historical(tmp_path):
    from ..agent.context.narrative import SUMMARY_EVENT

    game = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=500)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    await service.call("set_context_policy", "c", "old", 500, {"window_ticks": 42})
    await service.call("record", "c", "old", 500, "frontdesk", SUMMARY_EVENT, {"text": "Earlier discussion.", "covered_through_seq": 0, "source_run_seqs": [], "previous_summary_seq": None})
    game.load_epoch, game.game_tick = "new", 100
    await service.consult(game, "next", object())
    messages = runtime.sessions[-1].messages
    assert any(m.get("name") == "historical_narrative" and "epoch old at tick 500" in m["content"] for m in messages)
    assert any(m.get("name") == "colony_load" for m in messages)
    assert (await service.call("context_policy", "c")) == {"window_ticks": 42, "narrative_threshold": 60_000}
    with pytest.raises(ValueError):
        await service.call("set_context_policy", "c", "new", 100, {"window_ticks": 0})
    await service.close()


@pytest.mark.asyncio
async def test_old_unsummarized_narration_survives_user_word_window(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=200_000)
    runtime = Runtime()
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    seq = await service.call("record_utterance", "c", "e", 1, "old user words")
    run = await service.call("record_run", "c", "e", 1, "frontdesk", seq, [{"role": "assistant", "content": "Earlier discussion."}])
    await service.consult(game, "next", object())
    messages = runtime.sessions[-1].messages
    assert not any(m.get("content") == "old user words" for m in messages)
    assert any(m.get("name") == "historical_narrative" and "Earlier discussion." in m["content"] and str(run) in m["content"] for m in messages)
    await service.close()


@pytest.mark.asyncio
async def test_real_submission_summary_has_source_but_omits_long_history_from_next_request(tmp_path):
    from ..agent.context.narrative import SUMMARY_EVENT, select_narrative
    from ..message_contracts import AssistantTurn
    from ..serialization import canonical
    from .test_resumable_run import Script, runtime_for
    from .test_runtime_repair import settings, submit

    model = Script([AssistantTurn("summary", "mock", "stop", "Earlier topics."), submit("New complete answer.")])
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=20)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime_for(model), game_session=lambda: game)
    await service.call("set_context_policy", "c", "e", 1, {"narrative_threshold": 20})
    utterance = await service.call("record_utterance", "c", "e", 10, "private exact user words")
    args = json.dumps({"answer": "historic factual answer", "detail": "long prior report " * 100}, ensure_ascii=False)
    call = {"id": "submit-1", "type": "function", "function": {"name": "submit_response", "arguments": args}}
    messages = [{"role": "assistant", "content": None, "tool_calls": [call]}, {"role": "tool", "tool_call_id": "submit-1", "content": "raw observation secret"}]
    run = await service.call("record_run", "c", "e", 10, "frontdesk", utterance, messages)
    events = await service.call("context_events", "c", 20, [], 180_000, 0)
    source = select_narrative(events, None)
    result = await service.consult(game, "new question", settings())
    assert result["answer"] == "New complete answer."
    summary_input = str(model.requests[0])
    assert "historic factual answer" in summary_input and "long prior report" in summary_input
    assert "private exact user words" not in summary_input and "raw observation secret" not in summary_input
    actual = model.requests[1]["messages"]
    actual_text = str(actual)
    assert "long prior report" not in actual_text and "historic factual answer" not in actual_text
    assistant_call = next(m for m in actual if any(c["id"] == "submit-1" for c in m.get("tool_calls", [])))
    placeholder = json.loads(assistant_call["tool_calls"][0]["function"]["arguments"])
    assert placeholder["historical_call"] == {"event_seq": run, "tool_call_id": "submit-1"}
    assert any(m.get("tool_call_id") == "submit-1" for m in actual)
    original = await service.call("event", "c", run)
    assert original["payload"]["messages"] == messages
    from ..workspace.session_view import recall
    assert recall([original], run, "submit-1", part="arguments") == args
    capability = next(spec for spec in service.capabilities(game, utterance) if spec.name == "recall")
    assert (await capability.handler({"event_seq": run, "tool_call_id": "submit-1", "part": "arguments"}))["original"] == args
    summary = await service.call("latest_event", "c", SUMMARY_EVENT)
    assert summary["payload"]["covered_through_seq"] == run
    assert summary["payload"]["source_run_seqs"] == [run]
    assert summary["payload"]["source_sha256"] == hashlib.sha256(canonical(source).encode()).hexdigest()
    await service.close()


def test_evidence_query_uses_index_and_context_sql_omits_tool_payloads(tmp_path):
    from ..workspace.journal import Journal

    journal = Journal(tmp_path / "events.sqlite")
    seq = journal.record_utterance("c", "e", 1, "q")
    call = {"id": "x", "type": "function", "function": {"name": "look", "arguments": '{"secret":"large query"}'}}
    run = journal.record_run("c", "e", 1, "frontdesk", seq, [{"role": "assistant", "content": None, "tool_calls": [call]}, {"role": "tool", "tool_call_id": "x", "content": "large raw result"}], result={"large": "raw observation"}, audit={"large": "raw audit"})
    context = journal.context_events("c", 1, [])
    projected = next(e for e in context if e["seq"] == run)
    assert "large query" not in str(projected) and "large raw result" not in str(projected)
    assert "raw observation" not in str(projected) and "raw audit" not in str(projected)
    plan = journal._ledger.db.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE colony_id=? AND kind='agent.progress' AND json_extract(payload,'$.observation.observation_id')=? ORDER BY seq DESC LIMIT 1", ("c", "id")).fetchall()
    assert any("events_evidence_reference" in row["detail"] for row in plan)
    journal.close()


@pytest.mark.asyncio
async def test_successive_summaries_link_previous_without_repeating_old_source_ids(tmp_path):
    from ..agent.context.narrative import SUMMARY_EVENT
    from ..message_contracts import AssistantTurn
    from .test_resumable_run import Script, runtime_for
    from .test_runtime_repair import settings, submit

    model = Script([AssistantTurn("s1", "mock", "stop", "First summary."), submit("First complete answer."),
                    AssistantTurn("s2", "mock", "stop", "Second summary."), submit("Second complete answer.")])
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", runtime_for(model), game_session=lambda: game)
    await service.call("set_context_policy", "c", "e", 1, {"narrative_threshold": 1})
    u = await service.call("record_utterance", "c", "e", 1, "old")
    first_run = await service.call("record_run", "c", "e", 1, "frontdesk", u, [{"role": "assistant", "content": "Original discussion."}])
    await service.consult(game, "next", settings())
    first_summary = await service.call("latest_event", "c", SUMMARY_EVENT)
    await service.consult(game, "another", settings())
    second_summary = await service.call("latest_event", "c", SUMMARY_EVENT)
    assert second_summary["payload"]["previous_summary_seq"] == first_summary["seq"]
    assert first_run not in second_summary["payload"]["source_run_seqs"]
    assert second_summary["payload"]["covered_through_seq"] > first_summary["payload"]["covered_through_seq"]
    assert "covered_run_seqs" not in second_summary["payload"]
    await service.close()


def test_known_d2_schema_adds_only_index_and_unknown_schema_stays_untouched(tmp_path):
    from ..workspace.journal import Journal
    from ..workspace.views import WorkspaceViews

    schema = (Path(__file__).parent / "fixtures/event-ledger-d2-schema.sql").read_text(encoding="utf-8")
    path = tmp_path / "known.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute("INSERT INTO events VALUES (1,'c','e',10,1.0,'user','user.utterance',?)", ('{"text":"original","request_id":null}',))
        db.execute("INSERT INTO board_revisions VALUES ('i',1,'c',1,'intent','frontdesk','open',1,NULL,?, ?, NULL, ?, 'body')", ('{"event_seq":1}', '[]', '[]'))
    def rows():
        with sqlite3.connect(path) as db:
            return (db.execute("SELECT * FROM events ORDER BY seq").fetchall(), db.execute("SELECT * FROM board_revisions ORDER BY entry_id,revision").fetchall())
    before = rows()
    assert WorkspaceViews(path).read("list")["total"] == 1
    journal = Journal(path)
    assert rows() == before
    assert journal._ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='events_evidence_reference'").fetchone()
    journal.close()
    assert rows() == before

    unknown = tmp_path / "unknown.sqlite"
    with sqlite3.connect(unknown) as db:
        db.executescript(schema)
        db.execute("DROP INDEX events_utterance_reference")
    original = hashlib.sha256(unknown.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        Journal(unknown)
    assert hashlib.sha256(unknown.read_bytes()).hexdigest() == original
    tampered = tmp_path / "tampered.sqlite"
    with sqlite3.connect(tampered) as db:
        db.executescript(schema)
        db.executescript("DROP TRIGGER sealed_event_update; CREATE TRIGGER sealed_event_update BEFORE UPDATE ON events BEGIN SELECT 1; END;")
    original = hashlib.sha256(tampered.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        Journal(tampered)
    assert hashlib.sha256(tampered.read_bytes()).hexdigest() == original


def test_old_empty_runs_do_not_accumulate_but_recent_and_original_remain(tmp_path):
    from ..workspace.journal import Journal

    journal = Journal(tmp_path / "events.sqlite")
    old = journal.record_utterance("c", "e", 1, "old")
    empty = journal.record_run("c", "e", 1, "frontdesk", old, [])
    malformed_call = {"id": "x", "type": "function", "function": {"name": "submit_response", "arguments": "{"}}
    failed = journal.record_run("c", "e", 1, "frontdesk", old, [{"role": "assistant", "content": None, "tool_calls": [malformed_call]}, {"role": "tool", "tool_call_id": "x", "content": "failed"}])
    narrated = journal.record_run("c", "e", 1, "frontdesk", old, [{"role": "assistant", "content": "retained narrative"}])
    recent_u = journal.record_utterance("c", "e", 200_000, "recent")
    recent = journal.record_run("c", "e", 200_000, "frontdesk", recent_u, [])
    selected = {event["seq"] for event in journal.context_events("c", 200_000, [])}
    assert empty not in selected and failed not in selected
    assert narrated in selected and recent in selected
    assert journal.event("c", failed)["payload"]["messages"][1]["content"] == "failed"
    journal.close()


def test_narrative_cache_scans_only_new_sequences_and_evicts_on_summary(tmp_path):
    from ..workspace.journal import Journal

    journal = Journal(tmp_path / "events.sqlite")
    ledger = journal._ledger
    u = journal.record_utterance("c", "e", 1, "old words")
    for _ in range(150):
        journal.record_run("c", "e", 1, "frontdesk", u, [])
    narrated = journal.record_run("c", "e", 1, "frontdesk", u, [{"role": "assistant", "content": "keep narrative"}])
    statements = []
    ledger.db.set_trace_callback(statements.append)
    first = journal.context_events("c", 200_000, [])
    assert [e["seq"] for e in first] == [narrated]
    first_scan = next(s for s in statements if "json_each(events.payload" in s)
    assert "seq>0" in first_scan
    statements.clear()
    second = journal.context_events("c", 200_000, [])
    assert second == first
    second_scan = next(s for s in statements if "json_each(events.payload" in s)
    assert f"seq>{narrated}" in second_scan
    new = journal.record_run("c", "e", 1, "frontdesk", u, [])
    statements.clear()
    assert journal.context_events("c", 200_000, []) == first
    assert f"seq>{narrated}" in next(s for s in statements if "json_each(events.payload" in s)
    assert new > narrated
    assert journal.context_events("c", 200_000, [], after_run_seq=narrated) == []
    assert ledger._narrative_cache["c"]["runs"] == {}
    # A rollback or larger window still selects recent historical pairings by tick,
    # including empty runs, without restoring covered narrative.
    rolled_back = journal.context_events("c", 2, [], window_ticks=180_000, after_run_seq=narrated)
    assert {u, narrated, new} <= {e["seq"] for e in rolled_back}
    assert next(e for e in rolled_back if e["seq"] == narrated)["payload"]["messages"][0]["content"] is None
    plan = ledger.db.execute("EXPLAIN QUERY PLAN SELECT seq FROM events WHERE kind=? AND colony_id=? AND game_tick>=?", ("agent.run", "c", 1)).fetchall()
    assert any("events_kind_colony_tick" in row["detail"] for row in plan)
    journal.close()
    reopened = Journal(tmp_path / "events.sqlite")
    assert narrated in {e["seq"] for e in reopened.context_events("c", 200_000, [])}
    reopened.close()


def test_evidence_only_intermediate_schema_adds_tick_index_without_rewriting_rows(tmp_path):
    from ..workspace.journal import Journal

    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    journal.record_utterance("c", "e", 1, "original")
    journal.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX events_kind_colony_tick")
        before = db.execute("SELECT * FROM events").fetchall()
    journal = Journal(path)
    assert journal.events("c")[0]["payload"]["text"] == "original"
    assert journal._ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='events_kind_colony_tick'").fetchone()
    journal.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM events").fetchall() == before
