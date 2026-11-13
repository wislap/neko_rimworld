from __future__ import annotations

import asyncio
import json

import pytest

from plugin.plugins.neko_rimworld.agent.delivery.handoff import (
    SubmissionDraft,
    SubmissionPatchError,
    count_handoff_tokens,
)
from plugin.plugins.neko_rimworld.agent.model.model_client import ModelClientError
from plugin.plugins.neko_rimworld.agent.runtime.audit import AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.runtime.loop import AgentRuntime
from plugin.plugins.neko_rimworld.message_contracts import ToolCall
from plugin.plugins.neko_rimworld.reports import ReportError, ReportStore

from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, ScriptedModel, query_tool, settings, submitted_turn, turn
from .test_plugin import make_plugin


def record(index=1, **extra):
    return {
        "report_id": f"{index:032x}",
        "state": "complete",
        "question": "Question",
        "full_text": "A full historical report.",
        "observations": [],
        **extra,
    }


def test_length_patch_keeps_detail_without_retransmission():
    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"detail": "Detailed original", "answer": "超限内容。" * 300})
    assert caught.value.details["saved_fields"] == ["answer", "detail"]
    assert caught.value.details["required_fields"] == ["answer"]
    result = draft.apply({"answer": "Brief"})
    assert result["full_text"] == "Brief\n\nDetailed original"
    assert draft.snapshot()["complete"]


def test_invalid_replacements_preserve_valid_inputs_and_clearing_is_explicit():
    draft = SubmissionDraft()
    assert draft.apply({"answer": "Brief", "detail": "Original"})["agent_text"] == "Brief"
    with pytest.raises(SubmissionPatchError):
        draft.apply({"detail": 7})
    assert draft.detail == "Original"
    with pytest.raises(SubmissionPatchError):
        draft.apply({"answer": "New brief"})
    assert draft.answer == "New brief" and draft.detail == "Original"
    result = draft.apply({"detail": None})
    assert result["full_text"] == result["agent_text"] == "New brief"
    with pytest.raises(SubmissionPatchError):
        draft.apply({"full_text": "old alias"})
    with pytest.raises(SubmissionPatchError):
        draft.apply({})


def test_report_store_survives_restart_is_immutable_and_bounded(tmp_path):
    store = ReportStore(tmp_path, capacity=2)
    store.save(record(1, created_at="2026-01-01"))
    store.save(record(1, full_text="Overwrite attempt"))
    assert ReportStore(tmp_path).read(f"{1:032x}")["full_text"] == "A full historical report."
    store.save(record(2, created_at="2026-01-02"))
    store.save(record(3, created_at="2026-01-03"))
    assert store.list()["total"] == 2
    with pytest.raises(ReportError):
        store.read(f"{1:032x}")
    with pytest.raises(ReportError):
        store.read("../secrets")
    (tmp_path / ("a" * 32 + ".json")).write_text("not json", encoding="utf-8")
    assert ReportStore(tmp_path).list()["total"] == 2


def test_large_evidence_does_not_discard_report_body():
    store = ReportStore()
    saved = store.save(record(observations=[{"response": {"game_tick": i}, "data": "x" * 100000} for i in range(12)]))
    full = store.read(saved["report_id"])
    assert full["full_text"] == "A full historical report."
    assert full["evidence_status"] == "omitted_size_limit"
    assert full["observation_count"] == 12
    assert full["samples"][-1]["game_tick"] == 11
    assert full["observations"] == []


async def unused(_args):
    raise AssertionError("Unexpected game query")


def runtime_for(model, store):
    async def sink(value):
        return store.save(value)

    return AgentRuntime(
        tools_provider=semantic_tool_provider(),
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        report_sink=sink,
    )


@pytest.mark.asyncio
async def test_runtime_saves_detail_then_accepts_explicit_short_answer():
    body = "完整报告与调查细节。" * 100
    model = ScriptedModel([submitted_turn(body), submitted_turn("简短交接。")])
    store = ReportStore()
    result = await runtime_for(model, store).run("Detailed report", settings())
    assert result["answer"] == "简短交接。\n\n" + body
    assert result["summary"] == "简短交接。"
    assert result["delivery"]["submission_mode"] == "submit_response"
    assert store.read(result["report"]["report_id"])["state"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ModelClientError("test_failure", "Model failed"), RuntimeError("unexpected"), TimeoutError()]
)
async def test_failed_correction_retains_draft_as_draft(error):
    body = "已完成的调查。" * 100
    model = ScriptedModel([turn("", ToolCall("1", "submit_response", json.dumps({"detail": body}))), error])
    store = ReportStore()
    runtime = runtime_for(model, store)
    result = await runtime.run("Research", settings())
    assert result["answer"] == body
    assert result["delivery"]["submission_mode"] == "operational_fallback"
    saved = store.read(result["report"]["report_id"])
    assert saved["state"] == "draft"
    assert saved["full_text"] == body
    assert saved["agent_text"] is None


@pytest.mark.asyncio
async def test_cancel_after_draft_preserves_saved_body():
    blocked = asyncio.Event()

    class Model:
        count = 0

        async def complete(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                return turn("", ToolCall("1", "submit_response", json.dumps({"detail": "Saved draft"})))
            blocked.set()
            await asyncio.Event().wait()

    store = ReportStore()
    runtime = runtime_for(Model(), store)
    task = asyncio.create_task(runtime.run("Research", settings()))
    await asyncio.wait_for(blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = store.read(store.list()["reports"][0]["report_id"])
    assert saved["state"] == "draft"
    assert saved["full_text"] == "Saved draft"




@pytest.mark.asyncio
async def test_report_entry_full_retrieval_short_projection_and_export_failure():
    from utils.result_parser import parse_plugin_result

    plugin, _ = make_plugin({})

    async def failing_export(**kwargs):
        raise RuntimeError("host export offline")

    plugin.export_push = failing_export
    for i in range(20):
        plugin._reports.save(record(i))
    listed = (await plugin.rimworld_report(action="list", limit=20)).value
    assert count_handoff_tokens(listed["summary"])[0] <= 180
    result = (await plugin.rimworld_report(action="read", report_id=f"{1:032x}", _ctx={"run_id": "host-run"})).value
    assert result["report"]["full_text"] == "A full historical report."
    assert result["export"]["status"] == "unavailable"
    projected = parse_plugin_result(result, llm_result_fields=["summary"], plugin_message="done")
    assert "A full historical report." not in projected
    assert f"{1:032x}" in projected
    assert count_handoff_tokens(projected)[0] <= 180


@pytest.mark.asyncio
async def test_report_storage_failure_keeps_complete_result():
    async def fail_save(_record):
        raise OSError("Storage unavailable")

    model = ScriptedModel([submitted_turn("Complete answer")])
    runtime = AgentRuntime(
        tools_provider=semantic_tool_provider(),
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        report_sink=fail_save,
    )
    result = await runtime.run("Research", settings(), capabilities=tuple([query_tool(unused)]))
    assert result["status"] == "ok"
    assert result["answer"] == result["summary"] == "Complete answer"
    assert result["report"]["saved"] is False


@pytest.mark.asyncio
async def test_public_consult_accepts_only_question_and_reserved_host_context():
    from plugin.sdk.shared.core.entry_runtime import prepare_entry_kwargs

    plugin, _ = make_plugin({})
    method = plugin.rimworld_consult
    for name, value in (("report_id", f"{1:032x}"), ("mode", "single"), ("focus", "clinic")):
        args = prepare_entry_kwargs(
            plugin_id="neko_rimworld",
            entry_id="rimworld_consult",
            handler=method,
            meta=method.__neko_event_meta__,
            args={"question": "Investigate", name: value},
        )
        with pytest.raises(TypeError):
            await method(**args)
    args = prepare_entry_kwargs(
        plugin_id="neko_rimworld",
        entry_id="rimworld_consult",
        handler=method,
        meta=method.__neko_event_meta__,
        args={"question": "Investigate", "_ctx": {"run_id": "host"}},
    )
    assert args == {"question": "Investigate", "_ctx": {"run_id": "host"}}


@pytest.mark.asyncio
async def test_successful_native_export_contains_unshortened_body_and_silent_delivery():
    plugin, _ = make_plugin({})
    body = "报告内容。" * 400
    plugin._reports.save(record(full_text=body))
    sent = []

    async def export(**kwargs):
        sent.append(kwargs)
        return {"export_id": "native-artifact"}

    plugin.export_push = export
    result = (await plugin.rimworld_report(action="read", report_id=f"{1:032x}", _ctx={"run_id": "host-run"})).value
    assert result["export"]["status"] == "exported"
    assert body in sent[0]["text"]
    assert sent[0]["delivery"] == "silent"
    assert sent[0]["run_id"] == "host-run"


@pytest.mark.asyncio
async def test_export_diagnostics_separate_failure_stage_without_exposing_errors():
    from plugin.sdk.plugin import SdkError

    plugin, _ = make_plugin({})
    report_id = plugin._reports.save(record())['report_id']
    context = {"run_id": "host-run"}

    async def run_export(outcome):
        async def export(**kwargs):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        plugin.export_push = export
        return await plugin._report_service.export(report_id, context)

    for outcome, expected in [
        (TimeoutError("secret timeout"), {"stage": "export_push", "kind": "timeout"}),
        ({"success": False, "message": "secret rejection"}, {"stage": "export_push", "kind": "rejected"}),
        (SdkError("secret sdk", code="TRANSPORT_ERROR"),
         {"stage": "export_push", "kind": "sdk_error", "sdk_code": "TRANSPORT_ERROR"}),
        (SdkError("secret sdk", code="secret-code"), {"stage": "export_push", "kind": "sdk_error"}),
        (RuntimeError("secret exception"), {"stage": "export_push", "kind": "error"}),
    ]:
        value = await run_export(outcome)
        assert value == {"status": "unavailable", "reason": "export_failed", "report_retained": True,
                         "diagnostic": expected}
        assert "secret" not in str(value)
    assert await run_export({"success": True}) == {"status": "exported"}
    assert await plugin._report_service.export(report_id, {}) == {"status": "unavailable", "reason": "no_host_run"}
    def failed_read(_):
        raise OSError("secret path")
    plugin._reports.read = failed_read
    assert await plugin._report_service.export(report_id, context) == {
        "status": "unavailable", "reason": "export_failed", "report_retained": None,
        "diagnostic": {"stage": "report_read", "kind": "error"}}


@pytest.mark.asyncio
async def test_missing_report_export_does_not_claim_report_is_retained():
    plugin, _ = make_plugin({})
    outcome = await plugin._report_service.export("f" * 32, {"run_id": "host-run"})
    assert outcome == {"status": "unavailable", "reason": "export_failed", "report_retained": None,
                       "diagnostic": {"stage": "report_read", "kind": "error"}}
