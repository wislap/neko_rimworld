import threading

import pytest

from ..game.bridge import BridgeBusyError, BridgeNotConnected, BridgeServer
from ..game.protocol import (
    QUERY_REQUEST,
    QUERY_RESULT,
    SESSION_OPEN,
    Payload,
    SessionDescriptor,
    make_envelope,
    write_frame,
)
from .test_bridge import TOKEN, connect_and_challenge, finish_authentication, read_until, wait_until


def test_idle_read_skips_busy_without_deferring_and_accepts_next_idle_slot():
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port)
    threads, results, errors = [], [], []
    def invoke(arguments, **options):
        try:
            results.append(server.request('colony_snapshot', arguments, timeout=3, **options))
        except Exception as exc:
            errors.append(exc)
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor('5' * 32, '6' * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server.session is not None)
        first = threading.Thread(target=invoke, args=({},))
        threads.append(first)
        first.start()
        request = read_until(client, QUERY_REQUEST)
        with pytest.raises(BridgeBusyError):
            server.request('colony_snapshot', {'skipped': True}, idle_only=True,
                           expected_connection=hello.connection_epoch, expected_session=session)
        with server._pending_lock:
            assert list(server._pending) == [request.request_id]
        write_frame(client, make_envelope(QUERY_RESULT, hello.connection_epoch, 3,
                                         Payload(status='ok', data={'first': True}, issues=[]),
                                         session=session, request_id=request.request_id))
        first.join(3)
        assert not first.is_alive() and not errors
        second = threading.Thread(target=invoke, args=({'idle': True},), kwargs={
            'idle_only': True, 'expected_connection': hello.connection_epoch, 'expected_session': session})
        threads.append(second)
        second.start()
        actual = read_until(client, QUERY_REQUEST)
        assert actual.payload.arguments == {'idle': True}
        write_frame(client, make_envelope(QUERY_RESULT, hello.connection_epoch, 4,
                                         Payload(status='ok', data={'idle': True}, issues=[]),
                                         session=session, request_id=actual.request_id))
        second.join(3)
        assert not second.is_alive() and not errors
        assert [result.payload.data for result in results] == [{'first': True}, {'idle': True}]
        old = SessionDescriptor(session.colony_id, '7' * 32, 1, 0)
        with pytest.raises(BridgeNotConnected):
            server.request('colony_snapshot', {}, idle_only=True,
                           expected_connection=hello.connection_epoch, expected_session=old)
        with server._pending_lock:
            assert not server._pending
    finally:
        client.close()
        assert server.stop(timeout=2)
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()


def test_idle_admission_flag_requires_boolean():
    server = BridgeServer(TOKEN, port=0)
    with pytest.raises(ValueError, match='idle_only'):
        server.request('colony_snapshot', {}, idle_only=1)
