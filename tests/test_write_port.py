import asyncio
import threading
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_rimworld.game.bridge import (
    BridgeNotConnected,
    BridgeNotDispatched,
    BridgeQueryTimeout,
    BridgeServer,
)
from plugin.plugins.neko_rimworld.game.protocol import SessionDescriptor
from plugin.plugins.neko_rimworld.game.write_port import BridgeGameWritePort
from plugin.plugins.neko_rimworld.tool_contracts import ReceiptLost, ToolExecutionError

SESSION = SessionDescriptor("colony", "a" * 32, 100, 1)


async def test_bound_port_rejects_session_switch_before_worker_enqueue(monkeypatch):
    server = BridgeServer("x" * 32, port=0)
    server._ready_epoch = "connection"
    server._session = SESSION
    server._capabilities = frozenset({"colony_write.v1"})
    port = BridgeGameWritePort(server, "connection", SESSION)
    entered, release = threading.Event(), threading.Event()
    original = server.commit

    def delayed(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "commit", delayed)
    task = asyncio.create_task(port.commit({"load_epoch": SESSION.load_epoch}))
    assert await asyncio.to_thread(entered.wait, 2)
    with server._connection_lock:
        server._session = SessionDescriptor("colony", "b" * 32, 0, 0)
    release.set()
    with pytest.raises(BridgeNotDispatched):
        await task
    assert server._outbound.empty()
    assert not server._pending


async def test_port_does_not_retry_ambiguous_timeout():
    class Bridge:
        calls = 0

        def commit(self, *args, **kwargs):
            self.calls += 1
            raise BridgeQueryTimeout("unknown outcome")

    bridge = Bridge()
    port = BridgeGameWritePort(bridge, "connection", SESSION)
    with pytest.raises(ReceiptLost) as error:
        await port.commit({"load_epoch": SESSION.load_epoch, "idempotency_key": "same-key"})
    assert error.value.idempotency_key == "same-key"
    assert bridge.calls == 1


async def test_port_receipts_and_unavailable_event_methods():
    class Bridge:
        def undo(self, token, **kwargs):
            assert kwargs["expected_connection"] == "connection"
            assert kwargs["expected_session"] == SESSION
            assert not kwargs["cancel_event"].is_set()
            return SimpleNamespace(payload=SimpleNamespace(status="ok", data={"status": "undone"}))

    port = BridgeGameWritePort(Bridge(), "connection", SESSION)
    assert await port.undo("token") == {"status": "undone"}
    with pytest.raises(ToolExecutionError, match="not available"):
        await port.events()
    with pytest.raises(ToolExecutionError, match="not available"):
        await port.push_policy({})
    with pytest.raises(ToolExecutionError) as error:
        await port.commit({"load_epoch": "b" * 32})
    assert error.value.code == "stale_epoch"

async def test_port_surfaces_received_failure_without_retry():
    class Bridge:
        calls = 0

        def dry_run(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(payload=SimpleNamespace(
                status="unsupported", error_code="unsupported", message="write unavailable", data=None
            ))

    bridge = Bridge()
    port = BridgeGameWritePort(bridge, "connection", SESSION)
    with pytest.raises(ToolExecutionError) as error:
        await port.dry_run({"load_epoch": SESSION.load_epoch})
    assert error.value.code == "unsupported"
    assert bridge.calls == 1


def bound_server():
    session = SessionDescriptor("1" * 32, "2" * 32, 1, 1)
    server = BridgeServer("x" * 32, port=0)
    server._ready_epoch = "3" * 32
    server._session = session
    server._capabilities = frozenset({"colony_write.v1"})
    return server, session, BridgeGameWritePort(server, "3" * 32, session)


def changeset(session):
    return {"changeset_id": "cs-1", "author": "planner", "entry_id": "t-1",
            "load_epoch": session.load_epoch, "idempotency_key": "key-1",
            "ops": [{"op": "set_work_priority", "pawn": "Thing_1", "work": "Doctor",
                     "expect": 3, "value": 1, "evidence": "e-1"}]}


async def test_cancel_before_worker_enters_bridge(monkeypatch):
    server, session, port = bound_server()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = server.commit

    def delayed(*args, **kwargs):
        entered.set()
        release.wait(2)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(server, "commit", delayed)
    task = asyncio.create_task(port.commit(changeset(session)))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    assert server._outbound.empty() and not server._pending


async def test_cancel_queued_write_removes_pending_and_queue(monkeypatch):
    server, session, port = bound_server()
    queued = threading.Event()
    original = server._outbound.put_nowait

    def signal_queue(item):
        original(item)
        queued.set()

    monkeypatch.setattr(server._outbound, "put_nowait", signal_queue)
    task = asyncio.create_task(port.commit(changeset(session)))
    assert await asyncio.to_thread(queued.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server._outbound.empty() and not server._pending
    assert server._flush_outbound(None, "3" * 32, 1) == 1


async def test_cancel_dispatched_write_is_unknown_and_pending_can_complete(monkeypatch):
    server, session, port = bound_server()
    queued = threading.Event()
    monkeypatch.setattr(server._outbound, "put_nowait", lambda item: (server._outbound.put(item), queued.set()))
    sent = []
    monkeypatch.setattr(server, "_write", lambda client, envelope: sent.append(envelope))
    task = asyncio.create_task(port.commit(changeset(session)))
    assert await asyncio.to_thread(queued.wait, 2)
    assert server._flush_outbound(None, "3" * 32, 1) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent) == 1 and len(server._pending) == 1
    server._complete_pending(sent[0].request_id, error=BridgeNotConnected("closed"))
    assert not server._pending and server._outbound.empty()


async def test_session_change_cleans_queued_pending(monkeypatch):
    server, session, port = bound_server()
    queued = threading.Event()
    original = server._outbound.put_nowait
    monkeypatch.setattr(server._outbound, "put_nowait", lambda item: (original(item), queued.set()))
    task = asyncio.create_task(port.commit(changeset(session)))
    assert await asyncio.to_thread(queued.wait, 2)
    with server._connection_lock:
        server._session = SessionDescriptor("1" * 32, "4" * 32, 1, 1)
    server._fail_pending(BridgeNotConnected("session changed"))
    with pytest.raises(BridgeNotDispatched):
        await task
    assert not server._pending and server._outbound.empty()


async def test_session_change_waits_for_registration_then_cleans(monkeypatch):
    server, session, port = bound_server()
    entered, release, changed = threading.Event(), threading.Event(), threading.Event()
    original = server._outbound.put_nowait

    def blocked_enqueue(item):
        entered.set()
        assert release.wait(2)
        original(item)

    monkeypatch.setattr(server._outbound, "put_nowait", blocked_enqueue)
    task = asyncio.create_task(port.commit(changeset(session)))
    assert await asyncio.to_thread(entered.wait, 2)

    def switch():
        with server._connection_lock:
            server._session = SessionDescriptor("1" * 32, "4" * 32, 1, 1)
            server._fail_pending(BridgeNotConnected("session changed"))
        changed.set()

    thread = threading.Thread(target=switch)
    thread.start()
    assert not changed.is_set()
    release.set()
    assert await asyncio.to_thread(changed.wait, 2)
    thread.join()
    with pytest.raises(BridgeNotDispatched):
        await task
    assert not server._pending and server._outbound.empty()

async def test_queue_audit_failure_prevents_socket_write(monkeypatch):
    server, session, port = bound_server()
    sent = []
    monkeypatch.setattr(server, '_write', lambda client, envelope: sent.append(envelope))
    async def audit(stage, request_id, envelope):
        raise ToolExecutionError('write_audit_failed', 'unavailable', recoverable=False)
    with pytest.raises(ToolExecutionError, match='unavailable'):
        await port.commit(changeset(session), audit=audit)
    assert not sent and not server._pending and server._outbound.empty()


async def test_cancel_while_queue_audit_waits_prevents_socket_write(monkeypatch):
    server, session, port = bound_server()
    sent, entered, release = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(server, '_write', lambda client, envelope: sent.append(envelope))
    async def audit(stage, request_id, envelope):
        entered.set()
        await release.wait()
    task = asyncio.create_task(port.commit(changeset(session), audit=audit))
    await entered.wait()
    assert await asyncio.to_thread(server._flush_outbound, None, '3' * 32, 1) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.05)
    assert not sent and not server._pending and server._outbound.empty()

async def test_timeout_before_dispatch_never_becomes_receipt_lost(monkeypatch):
    from plugin.plugins.neko_rimworld.game.bridge import BridgeNotDispatched
    server, session, port = bound_server()
    commit = server.commit
    monkeypatch.setattr(server, 'commit', lambda *args, **kwargs: commit(*args, timeout=0.03, **kwargs))
    with pytest.raises(BridgeNotDispatched):
        await port.commit(changeset(session))
    sent = []
    monkeypatch.setattr(server, '_write', lambda client, envelope: sent.append(envelope))
    assert server._flush_outbound(None, '3' * 32, 1) == 1
    assert not sent

async def test_session_change_during_queue_audit_prevents_send(monkeypatch):
    from plugin.plugins.neko_rimworld.game.bridge import BridgeNotDispatched
    server, session, port = bound_server()
    entered, release = asyncio.Event(), asyncio.Event()
    async def audit(stage, request_id, envelope):
        entered.set()
        await release.wait()
    task = asyncio.create_task(port.commit(changeset(session), audit=audit))
    await entered.wait()
    with server._connection_lock:
        server._session = SessionDescriptor(session.colony_id, '4' * 32, 1, 1)
    release.set()
    with pytest.raises(BridgeNotDispatched):
        await task
    assert server._outbound.empty() and not server._pending


async def test_queue_audit_deadline_prevents_send(monkeypatch):
    server, session, port = bound_server()
    commit = server.commit
    monkeypatch.setattr(server, 'commit', lambda *args, **kwargs: commit(*args, timeout=0.03, **kwargs))
    async def audit(stage, request_id, envelope):
        await asyncio.Event().wait()
    with pytest.raises(ToolExecutionError) as error:
        await port.commit(changeset(session), audit=audit)
    assert error.value.code == 'write_audit_failed'
    assert server._outbound.empty() and not server._pending
