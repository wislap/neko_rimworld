import threading

import pytest

from plugin.plugins.neko_rimworld.game.bridge import BridgeEventGap, BridgeNotConnected, BridgeServer
from plugin.plugins.neko_rimworld.game.protocol import (
    GAME_EVENT,
    POLICY_REQUEST,
    POLICY_RESULT,
    SESSION_OPEN,
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


def test_policy_and_event_stream_share_connection():
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_events.v1", "colony_pause.v1"))
    session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server.session is not None)
        policy = {"pause_on_letter_defs": ["ThreatBig"], "source": "frontdesk"}
        responses = []
        thread = threading.Thread(target=lambda: responses.append(server.push_policy(policy)))
        thread.start()
        request = read_until(client, POLICY_REQUEST)
        assert request.payload.arguments == policy
        write_frame(client, make_envelope(POLICY_RESULT, hello.connection_epoch, 3,
                    Payload(status="ok", data=policy, issues=[]), session, request.request_id))
        thread.join(2)
        assert responses[0].payload.data == policy
        for seq, cursor in ((4, 1), (5, 2)):
            write_frame(client, make_envelope(GAME_EVENT, hello.connection_epoch, seq,
                        Payload(data={"cursor": cursor, "kind": "heartbeat"}), session))
        wait_until(lambda: server._event_cursor == 2)
        assert [e["cursor"] for e in server.game_events(1, expected_connection=hello.connection_epoch, expected_session=session)] == [2]
        write_frame(client, make_envelope(GAME_EVENT, hello.connection_epoch, 6,
                    Payload(data={"cursor": 4, "kind": "heartbeat"}), session))
        wait_until(lambda: server._event_cursor == 4)
        with pytest.raises(BridgeEventGap):
            server.game_events(2, expected_connection=hello.connection_epoch, expected_session=session)
        with pytest.raises(BridgeNotConnected):
            server.game_events(0, expected_connection="f" * 32, expected_session=session)
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_event_buffer_reports_eviction():
    server = BridgeServer(TOKEN, port=0)
    session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
    server._ready_epoch = "1" * 32
    server._session = session
    server._capabilities = frozenset({"colony_events.v1"})
    for cursor in range(1, 1026):
        server._handle_game_event(make_envelope(GAME_EVENT, server._ready_epoch, cursor,
                                  Payload(data={"cursor": cursor, "kind": "heartbeat"}), session))
    with pytest.raises(BridgeEventGap):
        server.game_events(0, expected_connection=server._ready_epoch, expected_session=session)
    assert len(server.game_events(1, expected_connection=server._ready_epoch, expected_session=session)) == 1024


def test_event_identity_and_epoch_change():
    from plugin.plugins.neko_rimworld.game.bridge import BridgeProtocolError

    server = BridgeServer(TOKEN, port=0)
    old = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
    new = SessionDescriptor("2" * 32, "4" * 32, 2, 1)
    server._ready_epoch = "1" * 32
    server._session = old
    server._capabilities = frozenset({"colony_events.v1"})
    event = make_envelope(GAME_EVENT, server._ready_epoch, 1,
                          Payload(data={"cursor": 1, "kind": "heartbeat"}), old)
    server._handle_game_event(event)
    saved = server.game_events(0, expected_connection=server._ready_epoch, expected_session=old)[0]
    assert (saved["connection_epoch"], saved["colony_id"], saved["load_epoch"], saved["game_tick"], saved["snapshot_sequence"]) == (server._ready_epoch, old.colony_id, old.load_epoch, 1, 0)
    assert saved["data"] == {"cursor": 1, "kind": "heartbeat"}
    with pytest.raises(BridgeProtocolError):
        server._handle_game_event(event)
    server._handle_session_open(make_envelope(SESSION_OPEN, server._ready_epoch, 2, session=new))
    with pytest.raises(BridgeNotConnected):
        server.game_events(1, expected_connection=server._ready_epoch, expected_session=old)
    with pytest.raises(BridgeProtocolError):
        server._handle_game_event(event)
    server._handle_game_event(make_envelope(GAME_EVENT, server._ready_epoch, 3,
                              Payload(data={"cursor": 1, "kind": "session_snapshot", "origin": "loaded_game", "start_tick": 2}), new))
    assert server.game_events(0, expected_connection=server._ready_epoch, expected_session=new)[0]["cursor"] == 1


@pytest.mark.asyncio
async def test_event_only_capabilities_bind_without_write_catalog():
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.game.observation import Observation
    from plugin.plugins.neko_rimworld.game.query_tools import CatalogTools
    from plugin.plugins.neko_rimworld.tests.test_catalog_tools import _public_source, response

    bridge = SimpleNamespace(connection_key="1" * 32, session=SimpleNamespace(colony_id="colony", load_epoch="one"),
                             capabilities=frozenset({"colony_events.v1", "colony_pause.v1"}))
    calls = []

    async def query(args):
        calls.append(args)
        raw = response().to_dict()
        raw.update(connection_epoch=bridge.connection_key, colony_id="colony", load_epoch="one")
        raw["payload"] = {"status": "ok", "issues": [], "data": _public_source()}
        return Observation({"response": raw})

    setup = await CatalogTools(lambda: bridge, query).load()
    assert setup.write_catalog is None
    assert setup.write_port.capabilities == bridge.capabilities
    assert setup.write_identity() == (bridge.connection_key, "colony", "one")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_port_requires_explicit_subscription_and_reports_gap_details():
    from types import SimpleNamespace

    from plugin.plugins.neko_rimworld.game.write_port import BridgeGameWritePort
    from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError

    class Bridge:
        capabilities = frozenset({"colony_events.v1", "colony_pause.v1"})

        def push_policy(self, policy, **kwargs):
            assert policy == {"pause_on_letter_defs": [], "source": "frontdesk"}
            assert kwargs["expected_connection"] == "1" * 32
            return SimpleNamespace(payload=SimpleNamespace(status="ok", data=policy))

        def game_events(self, since, **kwargs):
            raise BridgeEventGap("missing", details={"requested_cursor": since, "latest_cursor": 4,
                                                    "oldest_cursor": 1, "missing_from": 2, "missing_to": 3})

    port = BridgeGameWritePort(Bridge(), "1" * 32, SessionDescriptor("2" * 32, "3" * 32, 1, 0))
    with pytest.raises(ToolExecutionError) as error:
        await port.events()
    assert error.value.code == "events_not_subscribed"
    await port.push_policy({"pause_on_letter_defs": [], "source": "frontdesk"})
    with pytest.raises(ToolExecutionError) as error:
        await port.events(1)
    assert error.value.code == "event_gap" and not error.value.recoverable
    assert error.value.details == {"requested_cursor": 1, "latest_cursor": 4, "oldest_cursor": 1,
                                   "missing_from": 2, "missing_to": 3}
