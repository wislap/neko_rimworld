import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from ..agent.runtime import frontdesk_service as runtime


@pytest.mark.asyncio
async def test_journal_timing_separates_queue_worker_failure_and_cancel(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class Journal:
        def __init__(self, path): pass
        def first(self):
            entered.set()
            release.wait(3)
            return "done"
        def second(self): return "next"
        def fail(self): raise ValueError("sensitive text must not be sampled")
        def close(self): pass
    monkeypatch.setattr(runtime, "Journal", Journal)
    service = runtime.FrontdeskService(tmp_path / "db", None, game_session=lambda: None)
    first = asyncio.create_task(service.call("first"))
    assert await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(service.call("second"))
    await asyncio.sleep(.05)
    pending = service.journal_timing_snapshot()
    assert pending["methods"]["first"]["started"] == 1
    assert pending["methods"]["second"]["submitted"] == 1
    assert pending["methods"]["second"]["started"] == 0
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == "next"
    with pytest.raises(ValueError):
        await service.call("fail")
    snapshot = service.journal_timing_snapshot()
    assert snapshot["methods"]["first"]["await_cancelled"] == 1
    assert snapshot["methods"]["first"]["completed"] == 1
    assert snapshot["methods"]["second"]["queue_seconds"] >= .04
    assert snapshot["methods"]["second"]["queue_max_seconds"] == snapshot["methods"]["second"]["queue_seconds"]
    assert snapshot["methods"]["first"]["worker_max_seconds"] == snapshot["methods"]["first"]["worker_seconds"]
    assert snapshot["methods"]["fail"]["failed"] == 1
    assert "sensitive text" not in str(snapshot)
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_queued_call_never_starts_or_counts_as_active(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class Journal:
        def __init__(self, path): self.second_calls = 0
        def first(self):
            entered.set()
            release.wait(3)
        def second(self): self.second_calls += 1
        def close(self): pass
    monkeypatch.setattr(runtime, "Journal", Journal)
    service = runtime.FrontdeskService(tmp_path / "db", None, game_session=lambda: None)
    first = asyncio.create_task(service.call("first"))
    assert await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(service.call("second"))
    try:
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        snapshot = service.journal_timing_snapshot()
        item = snapshot["methods"]["second"]
        assert item["submitted"] == item["await_cancelled"] == 1
        assert item["started"] == item["completed"] == 0
        assert "active" not in item
    finally:
        release.set()
        await first
        await service.close()
    assert second.done() and service.journal.second_calls == 0


@pytest.mark.asyncio
async def test_ingest_failures_capture_worker_binding_cursor_and_scope(tmp_path):
    from ..tool_contracts import ToolExecutionError
    from ..workspace.session_view import frontdesk_messages
    from .test_event_load_sample import snapshot

    current = SimpleNamespace(colony_id="colony", load_epoch="old", game_tick=50, connection_epoch="old-conn")
    identity = ["old-conn", "colony", "old"]
    service = runtime.FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    event = snapshot("old", 50, 10)
    try:
        assert await service.call("ingest_game_event", "colony", "old", 50, "connection", event, observed_tick=55)
        assert not await service.call("ingest_game_event", "colony", "old", 50, "connection", event, observed_tick=55)
        assert service.journal_timing_snapshot()["ingest_failures"] is None
        before_messages = frontdesk_messages(await service.call("events", "colony"), now_tick=55)
        changed = dict(event, data={"secret": "must not appear"})
        for _ in range(2):
            with pytest.raises(ValueError, match="cursor was reused"):
                await service.call("ingest_game_event", "colony", "old", 50, "connection", changed, observed_tick=55,
                                   diagnostic_binding_identity=lambda: tuple(identity))
        first = service.journal_timing_snapshot()["ingest_failures"]
        assert first["count"] == first["last_repeat_count"] == 2
        assert first["first"]["failure_index"] == 1
        assert first["last"]["failure_index"] == 2
        assert first["first"]["stage"] == "journal_ingest"
        assert first["first"]["exception_type"] == "ValueError"
        assert first["first"]["cursor"] == event["cursor"]
        assert first["first"]["native_sample_tick"] == 50
        assert first["first"]["observed_tick"] == 55
        assert first["first"]["frames"][-1]["function"] == "ingest_game_event"
        assert "must not appear" not in str(first)
        current.load_epoch, current.game_tick = "new", 100
        identity[:] = ["new-conn", "colony", "new"]
        with pytest.raises(ToolExecutionError) as exc:
            await service.call("ingest_game_event", "colony", "old", 50, "connection", event,
                               diagnostic_binding_identity=lambda: tuple(identity))
        assert exc.value.code == "world_changed"
        failures = service.journal_timing_snapshot()["ingest_failures"]
        assert failures["count"] == 3 and failures["last_repeat_count"] == 1
        assert failures["intermediate_omitted"] == 1
        assert failures["first"] == first["first"]
        assert failures["last"]["current_binding"] == {"connection_epoch": "new-conn", "colony_id": "colony", "load_epoch": "new"}
        assert failures["last"]["stage"] == "active_scope"
        assert failures["last"]["error_code"] == "world_changed"
        assert failures["last"]["target"] == {"colony_id": "colony", "load_epoch": "old", "connection_epoch": "connection"}
        assert failures["last"]["current"] == {"colony_id": "colony", "load_epoch": "new", "connection_epoch": "new-conn", "game_tick": 100}
        assert service.journal_timing_snapshot()["methods"]["ingest_game_event"]["failed"] == 3
        assert len(await service.call("events_by_kind", "colony", ("game.event",))) == 1
        assert frontdesk_messages(await service.call("events", "colony"), now_tick=55) == before_messages
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ingest_binding_rejection_and_diagnostic_failure_preserve_exception(tmp_path, monkeypatch):
    from ..tool_contracts import ToolExecutionError
    from .test_event_load_sample import snapshot

    current = SimpleNamespace(colony_id="colony", load_epoch="old", game_tick=50)
    service = runtime.FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    event = snapshot("old", 50, 10)
    try:
        with pytest.raises(ToolExecutionError) as exc:
            await service.call("ingest_game_event", "colony", "old", 50, "connection", event,
                               command_binding_check=lambda: False,
                               diagnostic_binding_identity=lambda: ("new-conn", "colony", "old"))
        assert exc.value.code == "world_changed"
        record = service.journal_timing_snapshot()["ingest_failures"]["first"]
        assert record["stage"] == "connection_binding"
        assert record["current"]["connection_epoch"] == "new-conn"
        def broken_diagnostics(*args):
            raise RuntimeError("diagnostics failed")
        monkeypatch.setattr(service, "_record_ingest_failure", broken_diagnostics)
        with pytest.raises(ToolExecutionError) as exc:
            await service.call("ingest_game_event", "colony", "old", 50, "connection", event,
                               command_binding_check=lambda: False)
        assert exc.value.code == "world_changed"
        assert service.journal_timing_snapshot()["methods"]["ingest_game_event"]["failed"] == 2
        assert await service.call("events", "colony") == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ingest_worker_logs_each_distinct_failure_and_not_duplicates(tmp_path, monkeypatch, caplog):
    from ..tool_contracts import ToolExecutionError
    from .test_event_load_sample import snapshot

    current = SimpleNamespace(colony_id="colony", load_epoch="old", game_tick=50, connection_epoch="conn")
    service = runtime.FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    event = snapshot("old", 50, 10)
    caplog.set_level(logging.WARNING, logger=runtime.__name__)
    try:
        assert await service.call("ingest_game_event", "colony", "old", 50, "connection", event)
        assert not await service.call("ingest_game_event", "colony", "old", 50, "connection", event)
        assert not caplog.records
        failures = iter([ValueError("secret A"), OSError("secret B"),
                         ToolExecutionError("event_gap", "secret C")])
        def fail_ingest(*args, **kwargs):
            raise next(failures)
        monkeypatch.setattr(service.journal, "ingest_game_event", fail_ingest)
        for exc_type in (ValueError, OSError, ToolExecutionError):
            with pytest.raises(exc_type):
                await service.call("ingest_game_event", "colony", "old", 50, "connection", event,
                                   observed_tick=55,
                                   diagnostic_binding_identity=lambda: ("conn", "colony", "old"))
        records = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records]
        assert [item["failure_index"] for item in records] == [1, 2, 3]
        assert [item["exception_type"] for item in records] == ["ValueError", "OSError", "ToolExecutionError"]
        assert [item["error_code"] for item in records] == [None, None, "event_gap"]
        assert all(item["stage"] == "journal_ingest" and item["cursor"] == event["cursor"] for item in records)
        assert all(item["native_sample_tick"] == 50 and item["observed_tick"] == 55 for item in records)
        assert all(item["target"]["connection_epoch"] == "connection" and
                   item["current_binding"]["connection_epoch"] == "conn" for item in records)
        assert all("frames" not in item for item in records)
        assert "secret" not in caplog.text
        assert service.journal_timing_snapshot()["methods"]["ingest_game_event"]["failed"] == 3
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ingest_logger_failure_does_not_replace_worker_exception(tmp_path, monkeypatch):
    from ..tool_contracts import ToolExecutionError
    from .test_event_load_sample import snapshot

    current = SimpleNamespace(colony_id="colony", load_epoch="old", game_tick=50)
    service = runtime.FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    event = snapshot("old", 50, 10)
    def fail_logging(*args, **kwargs):
        raise RuntimeError("logger failed")
    monkeypatch.setattr(logging.getLogger(runtime.__name__), "warning", fail_logging)
    try:
        with pytest.raises(ToolExecutionError) as exc:
            await service.call("ingest_game_event", "colony", "old", 50, "connection", event,
                               command_binding_check=lambda: False)
        assert exc.value.code == "world_changed"
        timing = service.journal_timing_snapshot()
        assert timing["methods"]["ingest_game_event"]["failed"] == 1
        assert timing["ingest_failures"]["first"]["stage"] == "connection_binding"
    finally:
        await service.close()
