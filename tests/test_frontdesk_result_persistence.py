import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.runtime.frontdesk_service import FrontdeskService
from plugin.plugins.neko_rimworld.workspace.journal import Journal


@pytest.mark.asyncio
async def test_consult_cancel_after_commit_retries_one_run(tmp_path, monkeypatch):
    committed, acknowledge = threading.Event(), threading.Event()
    original = Journal.record_run
    first = True

    def commit_before_ack(journal, *args, **kwargs):
        nonlocal first
        seq = original(journal, *args, **kwargs)
        if first:
            first = False
            committed.set()
            if not acknowledge.wait(5):
                raise TimeoutError("test acknowledgement was not released")
        return seq

    class Runtime:
        async def execute(self, session, settings):
            return {"status": "ok", "answer": "Saved answer"}

    monkeypatch.setattr(Journal, "record_run", commit_before_ack)
    game = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", Runtime(), game_session=lambda: game)
    try:
        task = asyncio.create_task(service.consult(game, "Question", None))
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        acknowledge.set()
        following = await service.consult(game, "Next question", None)
        runs = [e for e in await service.call("events", "c") if e["kind"] == "agent.run"]
        assert len(runs) == 2
        assert runs[0]["payload"]["result"]["answer"] == "Saved answer"
        assert runs[0]["payload"]["audit"]["run_id"] != runs[1]["payload"]["audit"]["run_id"]
        assert following["workspace"]["history_saved"] is True
        assert "c" not in service.pending
    finally:
        acknowledge.set()
        await service.close()


def test_run_identity_retries_reject_conflicts_and_keep_legacy_and_new_runs(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    try:
        utterance = journal.record_utterance("c", "old", 10, "Question")
        args = ("c", "old", 10, "frontdesk", utterance, [])
        result, audit = {"status": "ok", "answer": "Original"}, {"run_id": "run-1", "usage": {"input": 5}}
        seq = journal.record_run(*args, result=result, audit=audit)
        journal.record_load("c", "new", 20)
        assert journal.record_run(*args, result=copy.deepcopy(result), audit=copy.deepcopy(audit)) == seq
        for changed_args, changed_result, changed_audit in [
            (("c", "new", 10, "frontdesk", utterance, []), result, audit),
            (("c", "old", 11, "frontdesk", utterance, []), result, audit),
            (args, {**result, "answer": "Different"}, audit),
            (args, result, {**audit, "usage": {"input": 6}}),
        ]:
            with pytest.raises(ValueError, match="conflict"):
                journal.record_run(*changed_args, result=changed_result, audit=changed_audit)
        other = journal.record_utterance("other", "old", 10, "Other question")
        with pytest.raises(ValueError, match="conflict"):
            journal.record_run("other", "old", 10, "frontdesk", other, [], result=result, audit=audit)
        second = journal.record_run(*args, result=result, audit={"run_id": "run-2"})
        legacy_a = journal.record_run(*args, result=result, audit={})
        legacy_b = journal.record_run(*args, result=result, audit={})
        assert len({seq, second, legacy_a, legacy_b}) == 4
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_concurrent_pending_settlement_and_replacement_do_not_delete_new_run(tmp_path, monkeypatch):
    committed, acknowledge = threading.Event(), threading.Event()
    original = Journal.record_run
    first = True
    def block_after_commit(journal, *args, **kwargs):
        nonlocal first
        seq = original(journal, *args, **kwargs)
        if first:
            first = False
            committed.set()
            if not acknowledge.wait(5):
                raise TimeoutError("test acknowledgement was not released")
        return seq
    monkeypatch.setattr(Journal, "record_run", block_after_commit)
    game = SimpleNamespace(colony_id="c", load_epoch="old", game_tick=10)
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    try:
        utterance = await service.call("record_utterance", "c", "old", 10, "Question")
        args = ("c", "old", 10, "frontdesk", utterance, [])
        old = (args, {"result": {"status": "ok"}, "audit": {"run_id": "old-run"}})
        new = (args, {"result": {"status": "ok"}, "audit": {"run_id": "new-run"}})
        service.pending["c"] = old
        one = asyncio.create_task(service.settle_pending("c"))
        assert await asyncio.to_thread(committed.wait, 3)
        two = asyncio.create_task(service.settle_pending("c"))
        await asyncio.sleep(0)
        service.pending["c"] = new
        acknowledge.set()
        await asyncio.gather(one, two)
        assert service.pending["c"] is new
        await service.settle_pending("c")
        runs = [e for e in await service.call("events", "c") if e["kind"] == "agent.run"]
        assert [r["payload"]["audit"]["run_id"] for r in runs] == ["old-run", "new-run"]
        assert "c" not in service.pending
    finally:
        acknowledge.set()
        await service.close()
