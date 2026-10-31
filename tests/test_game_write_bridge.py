import threading

import pytest

from plugin.plugins.neko_rimworld.game.bridge import BridgeQueryTimeout, BridgeServer, BridgeUnsupportedError
from plugin.plugins.neko_rimworld.game.protocol import (
    SESSION_OPEN,
    WRITE_REQUEST,
    WRITE_RESULT,
    Payload,
    SessionDescriptor,
    make_envelope,
    write_frame,
)
from plugin.plugins.neko_rimworld.tests.test_bridge import (
    TOKEN,
    connect_and_challenge,
    finish_authentication,
    read_until,
    wait_until,
)


def test_write_roundtrip_and_unsupported():
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)
        result = []
        thread = threading.Thread(target=lambda: result.append(server.undo("token")))
        thread.start()
        request = read_until(client, WRITE_REQUEST)
        assert request.payload.mode == "undo"
        assert request.payload.arguments == {"token": "token"}
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
                                         Payload(status="unsupported", error_code="unsupported", message="No", issues=[]),
                                         session=session, request_id=request.request_id))
        thread.join(2)
        assert not thread.is_alive()
        assert result[0].payload.status == "unsupported"
        errors = []
        def invoke_timeout():
            try:
                server.undo("token", timeout=1.5)
            except Exception as exc:
                errors.append(exc)
        timeout_thread = threading.Thread(target=invoke_timeout)
        timeout_thread.start()
        timed_request = read_until(client, WRITE_REQUEST)
        assert timed_request.payload.arguments == {"token": "token"}
        timeout_thread.join(2)
        assert not timeout_thread.is_alive() and len(errors) == 1
        assert isinstance(errors[0], BridgeQueryTimeout)
    finally:
        client.close()
        assert server.stop(timeout=2)

    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port)
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)
        with pytest.raises(BridgeUnsupportedError):
            server.undo("token")
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_write_disconnect_and_mismatched_response():
    from plugin.plugins.neko_rimworld.game.bridge import BridgeWriteUnconfirmed

    for mismatch in (False, True):
        server = BridgeServer(TOKEN, port=0)
        _, port = server.start()
        client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
        errors = []
        try:
            finish_authentication(client, hello, challenge, TOKEN)
            session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
            write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
            wait_until(lambda: server._session is not None)

            def invoke():
                try:
                    server.undo("token", timeout=2)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=invoke)
            thread.start()
            request = read_until(client, WRITE_REQUEST)
            if mismatch:
                wrong = SessionDescriptor(session.colony_id, "4" * 32, 1, 0)
                write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
                    Payload(status="unsupported", error_code="unsupported", message="No", issues=[]),
                    session=wrong, request_id=request.request_id))
            else:
                client.close()
            thread.join(3)
            assert not thread.is_alive()
            assert len(errors) == 1
            assert isinstance(errors[0], BridgeWriteUnconfirmed)
        finally:
            client.close()
            assert server.stop(timeout=2)


@pytest.mark.parametrize("write_pending", [True, False])
def test_response_type_cannot_complete_other_request(write_pending):
    from plugin.plugins.neko_rimworld.game.bridge import BridgeProtocolError, BridgeWriteUnconfirmed
    from plugin.plugins.neko_rimworld.game.protocol import QUERY_REQUEST, QUERY_RESULT

    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1", "query.execute.v2"))
    errors = []
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)

        def invoke():
            try:
                if write_pending:
                    server.undo("token", timeout=2)
                else:
                    server.request("colony_snapshot", {}, timeout=2)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        request = read_until(client, WRITE_REQUEST if write_pending else QUERY_REQUEST)
        payload = Payload(status="unsupported", error_code="unsupported", message="No", issues=[])
        write_frame(client, make_envelope(QUERY_RESULT if write_pending else WRITE_RESULT, hello.connection_epoch, 3,
                                         payload, session=session, request_id=request.request_id))
        thread.join(3)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], BridgeWriteUnconfirmed if write_pending else BridgeProtocolError)
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_late_write_receipt_does_not_retry_or_complete_next_request():
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)
        errors = []
        def invoke_old():
            try:
                server.undo("old", timeout=1.5)
            except Exception as exc:
                errors.append(exc)
        old_thread = threading.Thread(target=invoke_old)
        old_thread.start()
        old = read_until(client, WRITE_REQUEST)
        assert old.payload.arguments == {"token": "old"}
        old_thread.join(2)
        assert not old_thread.is_alive() and len(errors) == 1
        assert isinstance(errors[0], BridgeQueryTimeout)
        assert not server._pending
        result = []
        thread = threading.Thread(target=lambda: result.append(server.undo("new", timeout=2)))
        thread.start()
        new = read_until(client, WRITE_REQUEST)
        assert old.request_id != new.request_id and new.payload.arguments == {"token": "new"}
        payload = Payload(status="unsupported", error_code="unsupported", message="No", issues=[])
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3, payload,
                                         session=session, request_id=old.request_id))
        assert thread.is_alive()
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 4, payload,
                                         session=session, request_id=new.request_id))
        thread.join(2)
        assert not thread.is_alive() and len(result) == 1
        assert not server._pending
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_stale_session_fails_pending_write():
    from plugin.plugins.neko_rimworld.game.bridge import BridgeWriteUnconfirmed
    from plugin.plugins.neko_rimworld.game.protocol import SESSION_STALE

    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
    errors = []
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)

        def invoke():
            try:
                server.undo("token", timeout=2)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        read_until(client, WRITE_REQUEST)
        write_frame(client, make_envelope(SESSION_STALE, hello.connection_epoch, 3,
                                         Payload(reason="load"), session=session))
        thread.join(3)
        assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], BridgeWriteUnconfirmed)
        assert not server._pending
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_wrong_request_id_cannot_complete_write():
    from plugin.plugins.neko_rimworld.game.bridge import BridgeError

    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
    errors = []
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)

        def invoke():
            try:
                server.undo("token", timeout=2)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        request = read_until(client, WRITE_REQUEST)
        wrong_id = "5" * 32 if request.request_id != "5" * 32 else "6" * 32
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
                    Payload(status="unsupported", error_code="unsupported", message="No", issues=[]),
                    session=session, request_id=wrong_id))
        thread.join(3)
        assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], BridgeError)
        assert not server._pending
    finally:
        client.close()
        assert server.stop(timeout=2)


@pytest.mark.parametrize("mode", ["dry_run", "commit", "undo", "set_paused"])
@pytest.mark.parametrize("response_kind", ["ok", "committed", "undone", "paused"])
def test_write_response_mode_correlation(mode, response_kind):
    from plugin.plugins.neko_rimworld.game.bridge import BridgeProtocolError, BridgeWriteUnconfirmed

    session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
    changeset = {
        "changeset_id": "cs", "author": "planner", "entry_id": "task", "load_epoch": session.load_epoch,
        "idempotency_key": "key", "ops": [{"op": "set", "pawn": "p", "expect": 0, "value": 1, "evidence": "e"}],
    }
    data = {
        "ok": {"status": "ok", "preview": []},
        "committed": {"status": "committed", "changeset_id": "cs", "load_epoch": session.load_epoch,
                      "readback": [], "undo_token": "token"},
        "undone": {"status": "undone", "changeset_id": "cs", "load_epoch": session.load_epoch, "readback": []},
        "paused": {"paused": True, "role": "planner"},
    }[response_kind]
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
    results, errors = [], []
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)

        def invoke():
            try:
                args = {"dry_run": (changeset,), "commit": (changeset,), "undo": ("token",),
                        "set_paused": (True, "planner")}[mode]
                results.append(getattr(server, mode)(*args, timeout=2))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        request = read_until(client, WRITE_REQUEST)
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
                                         Payload(status="ok", data=data, issues=[]),
                                         session=session, request_id=request.request_id))
        thread.join(3)
        assert not thread.is_alive()
        matches = {"dry_run": "ok", "commit": "committed", "undo": "undone", "set_paused": "paused"}
        if matches[mode] == response_kind:
            assert len(results) == 1 and not errors
        else:
            assert not results and len(errors) == 1 and isinstance(errors[0], BridgeWriteUnconfirmed if mode in {"commit", "undo"} else BridgeProtocolError)
    finally:
        client.close()
        assert server.stop(timeout=2)


@pytest.mark.parametrize("mode", ["commit", "undo"])
@pytest.mark.parametrize("matching_epoch", [True, False])
def test_write_success_data_epoch_matches_session(mode, matching_epoch):
    from plugin.plugins.neko_rimworld.game.bridge import BridgeProtocolError, BridgeWriteUnconfirmed

    session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
    changeset = {
        "changeset_id": "cs", "author": "planner", "entry_id": "task", "load_epoch": session.load_epoch,
        "idempotency_key": "key", "ops": [{"op": "set", "pawn": "p", "expect": 0, "value": 1, "evidence": "e"}],
    }
    data = {"status": "committed" if mode == "commit" else "undone", "changeset_id": "cs",
            "load_epoch": session.load_epoch if matching_epoch else "4" * 32, "readback": []}
    if mode == "commit":
        data["undo_token"] = "token"
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_write.v1",))
    results, errors = [], []
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server._session is not None)

        def invoke():
            try:
                results.append(server.commit(changeset, timeout=2) if mode == "commit" else server.undo("token", timeout=2))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        request = read_until(client, WRITE_REQUEST)
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
                                         Payload(status="ok", data=data, issues=[]),
                                         session=session, request_id=request.request_id))
        thread.join(3)
        assert not thread.is_alive()
        if matching_epoch:
            assert len(results) == 1 and not errors
        else:
            assert not results and len(errors) == 1 and isinstance(errors[0], BridgeWriteUnconfirmed)
    finally:
        client.close()
        assert server.stop(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['commit', 'undo'])
@pytest.mark.parametrize('failure', ['disconnect', 'protocol'])
async def test_dispatched_write_loss_is_unknown_through_real_bridge_service(mode, failure):
    import asyncio
    import copy
    from ..agent.write.service import WriteService
    from ..game.write_port import BridgeGameWritePort
    from ..tool_contracts import ToolExecutionError
    from .test_write_service import catalog, observation, op
    server = BridgeServer(TOKEN, port=0)
    _, port_number = server.start()
    client, hello, challenge = connect_and_challenge(port_number, capabilities=('colony_write.v1',))
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor('2' * 32, '3' * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        await asyncio.to_thread(wait_until, lambda: server._session is not None)
        identity = (hello.connection_epoch, session.colony_id, session.load_epoch)
        observed = observation()
        observed['response'].update(connection_epoch=identity[0], colony_id=identity[1], load_epoch=identity[2])
        events = []
        async def record(event): events.append(copy.deepcopy(event))
        port = BridgeGameWritePort(server, identity[0], session)
        service = WriteService(catalog(), [observed], port, lambda: identity, record=record)
        task = asyncio.create_task(service.commit({'entry_id': 'task', 'ops': [op()]}) if mode == 'commit'
                                   else service.undo({'token': 'opaque'}))
        request = await asyncio.to_thread(read_until, client, WRITE_REQUEST)
        if failure == 'disconnect':
            client.close()
        else:
            wrong = SessionDescriptor(session.colony_id, '4' * 32, 1, 0)
            write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
                Payload(status='unsupported', error_code='unsupported', message='No', issues=[]),
                session=wrong, request_id=request.request_id))
        with pytest.raises(ToolExecutionError) as error:
            await asyncio.wait_for(task, 3)
        assert error.value.code == ('write_receipt_lost' if mode == 'commit' else 'undo_outcome_unknown')
        assert not error.value.recoverable and error.value.details['outcome'] == 'unknown'
        assert error.value.details['request_id'] == request.request_id
        assert tuple(error.value.details[k] for k in ('connection_epoch', 'colony_id', 'load_epoch')) == identity
        assert [event['stage'] for event in events] == ['prepared', 'queued', 'result']
        assert events[-1]['outcome'] == 'unknown'
    finally:
        client.close()
        assert server.stop(timeout=2)
