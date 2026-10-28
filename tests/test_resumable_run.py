"""One run continues a frontdesk session: history, a new utterance, and only the run's own messages recorded."""

import copy
import json
from dataclasses import replace

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditLedger, AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
from plugin.plugins.neko_rimworld.agent.runtime.session import AgentRunSession
from plugin.plugins.neko_rimworld.game.situation import SituationProvider
from plugin.plugins.neko_rimworld.message_contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError, ToolSpec
from plugin.plugins.neko_rimworld.workspace.journal import Journal
from plugin.plugins.neko_rimworld.workspace.session_view import frontdesk_messages

from .consultation_setup import consultation_setup, semantic_tool_provider
from .test_runtime_repair import LOG, settings, submit
from .test_situation_context import native

COLONY = "colony-1"
E0 = "0" * 32
TICK = 100


class Script:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def complete(self, messages, tools, *, timeout):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        return self.turns.pop(0)


def failed(identifier, patch):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": identifier, "type": "function",
            "function": {"name": "submit_response", "arguments": json.dumps(patch)}}]},
        {"role": "tool", "tool_call_id": identifier,
         "content": json.dumps({"tool": "submit_response", "error": {"code": "invalid_submission"}})},
    ]


def runtime_for(model, *, provider=None):
    return AgentRuntime(
        tools_provider=provider or semantic_tool_provider(),
        audit_registry=AgentAuditRegistry(),
        logger=LOG,
        model_factory=lambda _: model,
    )


def test_repair_prunes_only_failed_pairs_of_this_run_and_leaves_history_untouched():
    history = [
        {"role": "user", "content": "Compare routes."},
        *failed("history-a", {"answer": "Earlier proposal."}),
        *failed("history-b", {"answer": "Earlier proposal two."}),
        {"role": "assistant", "content": "Earlier answer."},
    ]
    session = AgentRunSession.create(ledger=AgentAuditLedger("Compare routes."), question="Compare routes.",
                                     history=tuple(history))
    session.messages += [*failed("first", {"answer": "Old draft"}), *failed("latest", {"answer": "Latest draft"})]
    session.submission.answer = "Latest draft"
    session.submission.errors = {"answer": {"code": "invalid_submission"}}
    before = copy.deepcopy(session.messages)
    view = session.request_messages("repair")[:-1]

    prefix = session.run_start + 1
    assert view[:prefix] == before[:prefix]
    assert not any(m.get("tool_call_id") == "first" for m in view)
    assert any(m.get("tool_call_id") == "history-a" for m in view)
    assert any(m.get("tool_call_id") == "history-b" for m in view)
    assert session.messages == before


@pytest.mark.asyncio
async def test_second_run_continues_the_first_through_the_journal(tmp_path):
    journal = Journal(tmp_path / "ledger.sqlite")
    try:
        first_seq = journal.record_utterance(COLONY, E0, TICK, "Compare routes.")
        first = AgentRunSession.create(ledger=AgentAuditLedger("Compare routes."), question="Compare routes.")
        first_model = Script([submit("First answer.")])
        first_result = await runtime_for(first_model).execute(first, settings())
        assert first_result["status"] == "ok"
        journal.record_run(COLONY, E0, TICK, "frontdesk", first_seq, first.run_messages())

        history = frontdesk_messages(journal.events(COLONY), now_tick=TICK)
        second_seq = journal.record_utterance(COLONY, E0, TICK, "And the second?")
        second = AgentRunSession.create(ledger=AgentAuditLedger("And the second?"), question="And the second?",
                                        history=tuple(history))
        second_model = Script([submit("Second answer.")])
        second_result = await runtime_for(second_model).execute(second, settings())
        assert second_result["status"] == "ok"

        request = second_model.requests[0]["messages"]
        assert request[0] == first_model.requests[0]["messages"][0]
        assert request[1:-2] == history
        assert request[-2] == {"role": "user", "content": "And the second?"}
        journal.record_run(COLONY, E0, TICK, "frontdesk", second_seq, second.run_messages())
        assert [m["content"] for m in frontdesk_messages(journal.events(COLONY), now_tick=TICK)
                if m["role"] == "user" and m.get("name") != "historical_narrative"] == ["Compare routes.", "And the second?"]
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_situation_lands_before_the_new_question_and_run_start_still_indexes_it():
    observation = native(items={"conditions": [{"id": "c", "label": "Solar flare"}]})

    async def query(_):
        return observation

    setup = replace(consultation_setup(), situation_provider=SituationProvider(query))

    async def provide():
        return setup

    session = AgentRunSession.create(
        ledger=AgentAuditLedger("What next?"), question="What next?",
        history=({"role": "user", "content": "Previous exchange."},),
    )
    model = Script([submit("Consultation complete.")])
    result = await runtime_for(model, provider=provide).execute(session, settings())
    assert result["status"] == "ok"

    assert session.messages[session.run_start] == {"role": "user", "content": "What next?"}
    assert session.messages[session.run_start - 1]["name"] == "game_situation"
    assert session.messages[session.run_start - 2] == {"role": "user", "content": "Previous exchange."}


@pytest.mark.asyncio
async def test_world_changed_degrades_the_run_without_asking_for_a_new_consultation():
    async def changed(_):
        raise ToolExecutionError("world_changed", "Reload detected", recoverable=False)

    calls = (ToolCall("changed-call", "changed", "{}"),)
    model = Script([AssistantTurn("offline", "offline", "tool_calls", "", calls), submit("Stale claim.")])
    session = AgentRunSession.create(
        ledger=AgentAuditLedger("Current world?"), question="Current world?",
        history=({"role": "user", "content": "Previous exchange."},),
        capabilities=(ToolSpec("changed", "test", {}, changed),),
    )
    result = await runtime_for(model).execute(session, settings())
    assert len(model.turns) == 1 and session.result is result
    assert result["status"] == "degraded" and result["audit"]["error_code"] == "world_changed"
    assert "new consultation" not in json.dumps(result).lower()
    assert "must be read again" in result["answer"]
