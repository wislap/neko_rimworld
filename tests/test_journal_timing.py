import asyncio
import threading

import pytest

from ..workspace import runtime


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
