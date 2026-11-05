"""Original PNG -> retained evidence -> real HTTP parts -> portable report artifact."""

import asyncio
import base64
import copy
import hashlib
import io
import json
import struct
import zipfile
import zlib
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from ..agent.delivery.references import validate_references, visible_references
from ..agent.model.model_client import (
    AgentConfigurationError,
    ModelClientError,
    OpenAICompatibleModelClient,
    build_agent_model_settings,
    parse_agent_model_settings,
)
from ..agent.model.model_requests import _canonical_hash, request_model
from ..agent.model.usage import input_reservation
from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.session import AgentRunSession
from ..media import ImageInputConfig, MediaError, MediaStore, prepare_model_messages
from ..message_contracts import ToolResult
from ..reports import ReportError, ReportStore
from .test_agent import FakeLogger, settings, sse_response, turn
from .test_reports import record


def png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def attached_session(store):
    ledger = AgentAuditLedger("Build a house")
    session = AgentRunSession.create(ledger=ledger, question="Build a house", colony_id="c")
    session.media_store = store
    session.image_input = ImageInputConfig(enabled=True, tokens_per_image=1234)
    ref = store.put(
        png(),
        metadata={
            "colony_id": "c",
            "load_epoch": "e",
            "map_id": "m",
            "observation_id": "o",
            "plan_id": "house",
            "revision": 2,
        },
        owner="plan:house",
    )
    session.attach_media(ref, caption="Survey o; draft plan house revision 2")
    return session, ref


async def dispatch(session, model, config=None):
    return await request_model(
        model,
        config or settings(),
        session.ledger,
        session,
        session.request_messages("Research"),
        [],
        finalizing=True,
        reserve=0,
        research_deadline=900,
        cycle=1,
        logger=FakeLogger(),
    )


async def test_original_png_is_actual_http_input_and_audit_is_reconstructable(tmp_path):
    store = MediaStore(tmp_path / "media")
    session, ref = attached_session(store)
    persisted, sent = [], []

    class Budget:
        blocked = False

        async def status(self, scope):
            return {}

        async def reserve(self, role, input_tokens, output_tokens, **kwargs):
            self.reserved = input_tokens
            return "reservation"

        async def begin_dispatch(self, ident):
            return True

        async def settle(self, ident, usage, **kwargs):
            self.usage = usage

    budget = session.budget = Budget()

    async def persist(event):
        persisted.append(copy.deepcopy(event))

    session.persist_progress = persist

    async def handle(request):
        body = json.loads(request.content)
        event = persisted[0]["model_request"]
        parts = next(m["content"] for m in body["messages"] if m.get("name") == "retained_image_evidence")
        assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]) == png()
        assert event["request_sha256"] == _canonical_hash({"messages": body["messages"], "tools": []})
        assert store.references("request:" + event["attempt_id"])[0]["sha256"] == ref["sha256"]
        sent.append(body)
        return sse_response(
            {
                "id": "response",
                "model": "test-model",
                "choices": [{"message": {"content": "House assessed"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 3, "total_tokens": 403},
            }
        )

    client = OpenAICompatibleModelClient(
        replace(settings(), image_input_enabled=True, image_input_tokens_per_image=1234),
        transport=httpx.MockTransport(handle),
    )
    result = await dispatch(session, client)
    assert result.turn.content == "House assessed"
    event = persisted[0]["model_request"]
    assert "base64" not in json.dumps(event)
    assert event["attachments"][0]["sha256"] == hashlib.sha256(png()).hexdigest()
    assert event["input_budget"]["image_tokens_reserved"] == session.image_tokens_reserved == 1234
    assert session.input_tokens_estimated == event["input_budget"]["tokens"]
    assert budget.reserved >= event["input_budget"]["tokens"]
    assert budget.usage["prompt_tokens"] == 400  # Provider total is not inflated by the reservation.
    assert prepare_model_messages(event["messages"], store, session.image_input)[0] == sent[0]["messages"]
    assert session.run_messages() == []  # Auxiliary image input never damages assistant/tool pairing.
    assert len(list((tmp_path / "media").glob("*.png"))) == 1


@pytest.mark.parametrize("failure", ["disabled", "missing", "corrupt", "budget", "count"])
async def test_image_admission_failure_never_dispatches_or_consumes_request(failure, tmp_path):
    store = MediaStore(tmp_path)
    session, ref = attached_session(store)

    class Model:
        async def complete(self, *args, **kwargs):
            raise AssertionError("must not dispatch")

    config = settings()
    if failure == "disabled":
        session.image_input = ImageInputConfig()
    if failure == "missing":
        (tmp_path / (ref["sha256"] + ".png")).unlink()
    if failure == "corrupt":
        (tmp_path / (ref["sha256"] + ".png")).write_bytes(png()[:-1] + b"x")
    if failure == "budget":
        config = replace(config, max_input_tokens=1234)
    if failure == "count":
        session.image_input = ImageInputConfig(enabled=True, tokens_per_image=1234, max_images=1)
        session.attach_media(ref, caption="Second explicit view")
    result = await dispatch(session, Model(), config)
    assert result.error is not None
    assert session.model_calls == session.input_tokens_estimated == session.image_tokens_reserved == 0
    assert session.last_request_media == []


def test_plan_and_request_retention_survive_report_eviction_and_restart(tmp_path):
    reports = ReportStore(tmp_path / "reports", capacity=1)
    store = reports.media_store
    ref = store.put(png(), metadata={"plan_id": "house"}, owner="plan:house")
    store.retain("request:actual", [ref])
    reports.save(record(1, attachments=[ref], created_at="2026-01-01"))
    reports.save(record(2, attachments=[ref], created_at="2026-01-02"))
    restarted = ReportStore(tmp_path / "reports", capacity=1)
    with pytest.raises(ReportError):
        restarted.read(f"{1:032x}")
    assert restarted.media_store.references(f"report:{1:032x}") == []
    for owner in ("plan:house", "request:actual", f"report:{2:032x}"):
        assert restarted.media_store.references(owner)[0]["sha256"] == ref["sha256"]
    with zipfile.ZipFile(io.BytesIO(restarted.export_bundle(f"{2:032x}"))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read(manifest["attachments"][0]["path"]) == png()
        assert json.loads(archive.read("report.json"))["attachments"] == [ref]
        assert "A full historical report." in archive.read("report.md").decode()


def test_only_dispatched_image_manifest_is_citable_and_scope_is_preserved():
    session, ref = attached_session(MediaStore())
    observations = [{"observation_id": "o", "response": {"colony_id": "c", "load_epoch": "e"}}]
    messages = session.request_messages("Research")
    assert ref["evidence_id"] not in visible_references(messages, observations, "c", "e", session.run_start)[1]
    visible = visible_references(messages, observations, "c", "e", session.run_start, media_manifest=[ref])
    assert validate_references({"evidence_ids": [ref["evidence_id"]]}, visible)["evidence_ids"] == [ref["evidence_id"]]
    assert visible[1][ref["evidence_id"]]["current_observation"]
    old = visible_references(messages, observations, "c", "later", session.run_start, media_manifest=[ref])
    assert old[1][ref["evidence_id"]]["historical"]
    assert not visible_references(messages, observations, "another", "e", session.run_start, media_manifest=[ref])[1]


def test_tool_result_media_keeps_plain_tool_content_and_original_evidence():
    session, ref = attached_session(MediaStore())
    session.attachments.clear()
    session.active_media.clear()
    original = {"data": {"observation_id": "o", "image": ref}}
    tool = ToolResult("call", "building_preview", True, original, media=(ref,))
    assert session.tool_message(tool) == tool.as_message()
    assert tool.value == original
    assert session.attachments[0]["media"] == ref


def test_text_only_preparation_and_reservation_are_unchanged():
    messages = [{"role": "user", "content": "Question"}]
    assert prepare_model_messages(messages, None, ImageInputConfig()) == (messages, [], 0)
    allocation = input_reservation("ordinary JSON", 0)
    assert allocation["tokens"] == allocation["text_tokens"]
    assert allocation["conservative_tokens"] >= allocation["tokens"]
    with pytest.raises(ValueError):
        ImageInputConfig(enabled=True)
    with pytest.raises(MediaError):
        MediaStore().put(b"not a PNG")


def test_capacity_failure_and_explicit_reclamation_preserve_independent_owners(tmp_path):
    store = MediaStore(tmp_path, capacity_bytes=len(png()) - 1)
    with pytest.raises(MediaError, match="capacity"):
        store.put(png())
    assert not list(tmp_path.glob("*.png"))
    store = MediaStore(tmp_path, capacity_bytes=len(png()))
    ref = store.put(png(), owner="plan:house")
    store.retain("request:actual", [ref])
    assert store.put(png())["sha256"] == ref["sha256"]  # Reuse does not consume capacity twice.
    store.release("plan:house")
    assert store.collect_unowned() == []
    store.release("request:actual")
    assert store.collect_unowned() == [ref["sha256"]]
    with pytest.raises(MediaError, match="not retained"):
        store.read(ref)


async def test_failed_request_journal_keeps_original_and_prevents_http_dispatch(tmp_path):
    session, ref = attached_session(MediaStore(tmp_path))

    class Model:
        async def complete(self, *args, **kwargs):
            raise AssertionError("must not dispatch")

    async def persist(event):
        raise OSError("injected journal write failure")

    session.persist_progress = persist
    with pytest.raises(OSError, match="journal"):
        await dispatch(session, Model())
    assert session.model_calls == session.input_tokens_estimated == 0
    assert session.media_store.read(ref) == png()


def test_replacing_active_revision_preserves_report_originals_and_explicit_config():
    session, ref = attached_session(MediaStore())
    revision = session.media_store.put(png(), metadata={**ref["metadata"], "revision": 3})
    session.attach_media(revision, caption="New revision", replace_active=True)
    parts = session.media_messages()[0]["content"]
    assert len(parts) == 2 and parts[1]["media"]["metadata"]["revision"] == 3
    assert len(session.attachments) == 2
    with pytest.raises(AgentConfigurationError, match="explicit positive"):
        build_agent_model_settings(base_url="http://localhost/v1", model="test", image_input_enabled=True)
    config, errors = parse_agent_model_settings(
        {
            "neko_rimworld": {
                "agent": {
                    "base_url": "http://localhost/v1",
                    "model": "test",
                    "image_input_enabled": True,
                    "image_input_tokens_per_image": 4321,
                }
            }
        }
    )
    assert not errors and config.image_input_enabled and config.image_input_tokens_per_image == 4321
    assert ImageInputConfig().public_dict()["status"] == "pending_endpoint"


def test_retaining_an_artifact_does_not_activate_disabled_image_input():
    session = AgentRunSession.create(ledger=AgentAuditLedger("Text plan"), question="Text plan")
    session.media_store = MediaStore()
    reference = session.media_store.put(png())
    session.attach_media(reference, caption="Available artifact; image endpoint pending", activate=False)
    assert session.attachments[0]["media"] == reference
    assert session.active_media == session.last_request_media == []
    messages = session.request_messages("Research")
    assert prepare_model_messages(messages, session.media_store, session.image_input) == (messages, [], 0)


async def test_unconfigured_http_client_rejects_images_before_transport():
    def handle(request):
        raise AssertionError("Unconfigured endpoint must not receive an image request")

    client = OpenAICompatibleModelClient(settings(), transport=httpx.MockTransport(handle))
    session, _ = attached_session(MediaStore())
    wire, _, _ = prepare_model_messages(session.request_messages("Research"), session.media_store, session.image_input)
    with pytest.raises(ModelClientError, match="explicitly enabled"):
        await client.complete(wire, [], timeout=3)


def test_failed_media_write_returns_no_reference_and_missing_export_is_explicit(tmp_path, monkeypatch):
    store = MediaStore(tmp_path / "media")

    def fail_write(*args):
        raise OSError("injected artifact write failure")

    monkeypatch.setattr(store, "_write", fail_write)
    with pytest.raises(OSError, match="artifact write"):
        store.put(png(), owner="plan:house")
    assert not list((tmp_path / "media").glob("*.png"))
    reports = ReportStore(tmp_path / "reports")
    ref = reports.media_store.put(png())
    reports.save(record(7, attachments=[ref]))
    (tmp_path / "media" / (ref["sha256"] + ".png")).unlink()
    with pytest.raises(MediaError, match="not retained"):
        reports.export_bundle(f"{7:032x}")


def test_same_pixels_preserve_distinct_immutable_sources_and_visible_references(tmp_path):
    store = MediaStore(tmp_path)
    original = store.put(
        png(),
        metadata={"colony_id": "c", "load_epoch": "e", "observation_id": "one", "revision": 1},
        owner="plan:house",
    )
    newer = store.put(
        png(),
        metadata={"colony_id": "c", "load_epoch": "e", "observation_id": "two", "revision": 2},
        owner="plan:house",
    )
    assert original["sha256"] == newer["sha256"] and original["reference_id"] != newer["reference_id"]
    assert len(list(tmp_path.glob("*.png"))) == 1
    assert len(MediaStore(tmp_path).references("plan:house")) == 2
    observations = [
        {"observation_id": name, "response": {"colony_id": "c", "load_epoch": "e"}} for name in ("one", "two")
    ]
    visible = visible_references([], observations, "c", "e", 0, media_manifest=[original, newer])
    assert {original["evidence_id"], newer["evidence_id"]} == set(visible[1])
    assert visible[1][original["evidence_id"]]["source_observation_id"] == "one"
    assert visible[1][newer["evidence_id"]]["source_observation_id"] == "two"
    tampered = {**original, "metadata": newer["metadata"]}
    with pytest.raises(MediaError, match="provenance"):
        store.read(tampered)
    assert store.collect_unowned() == []


async def test_image_response_and_request_original_survive_scope_exit_cancellation(tmp_path):
    session, ref = attached_session(MediaStore(tmp_path))
    events = []

    async def persist(event):
        events.append(copy.deepcopy(event))

    session.persist_progress = persist

    class Budget:
        blocked = False

        async def status(self, scope):
            return {}

        async def reserve(self, *args, **kwargs):
            return "reservation"

        async def begin_dispatch(self, ident):
            return True

        async def settle(self, ident, usage, **kwargs):
            self.usage, self.response = usage, kwargs.get("response")

    session.budget = Budget()
    response = turn("Image-informed response retained")

    class Model:
        @asynccontextmanager
        async def request_scope(self):
            yield
            raise asyncio.CancelledError()

        async def complete(self, messages, tools, **kwargs):
            assert any(isinstance(m.get("content"), list) for m in messages)
            return response

    with pytest.raises(asyncio.CancelledError):
        await dispatch(session, Model())
    attempt = events[0]["model_request"]["attempt_id"]
    assert session.received_model_turn is response and session.response_audit_pending
    assert not session.model_receipt_unconfirmed
    assert session.budget.usage == response.usage
    assert session.budget.response["audit"]["content"] == response.content
    assert session.last_request_media[0]["reference_id"] == ref["reference_id"]
    assert session.media_store.references("request:" + attempt)[0]["reference_id"] == ref["reference_id"]
    assert session.media_store.read(ref) == png()
