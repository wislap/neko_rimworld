import copy
import hashlib
import json
from pathlib import Path

import pytest
from plugin.plugins.neko_rimworld.game import protocol
from plugin.plugins.neko_rimworld.game.bridge import BridgeServer
from plugin.plugins.neko_rimworld.tests.test_bridge import TOKEN

FIXTURE = Path(__file__).parent / "fixtures/colony-attention-v1/vectors.json"
VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))["valid"]
AUTHORITATIVE_SHA256 = "639528415ee833ff9ba17e1b814945ff58176e0a7a36c2f8f4ddf9d3fc189b5f"


def test_fixture_matches_mod_authority():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == AUTHORITATIVE_SHA256


@pytest.mark.parametrize("vector", VECTORS)
def test_negotiated_vectors_enter_event_stream(vector):
    envelope = protocol.decode_envelope(json.dumps(vector).encode(), direction=protocol.Direction.MOD_TO_PLUGIN, authenticated=True)
    server = BridgeServer(TOKEN, port=0)
    server._ready_epoch = envelope.connection_epoch
    server._session = envelope.session
    server._capabilities = frozenset({"colony_events.v1", "colony_attention.v1"})
    server._event_cursor = envelope.payload.data["cursor"] - 1
    server._handle_game_event(envelope)
    event = server.game_events(server._event_cursor - 1, expected_connection=envelope.connection_epoch, expected_session=envelope.session)[0]
    assert event["data"] == vector["payload"]["data"]


def test_negotiated_malformed_facts_rejected_before_cursor_advances():
    vector = copy.deepcopy(VECTORS[0])
    vector["payload"]["data"]["alerts"]["rows"] = "not rows"
    envelope = protocol.decode_envelope(json.dumps(vector).encode(), direction=protocol.Direction.MOD_TO_PLUGIN, authenticated=True)
    server = BridgeServer(TOKEN, port=0)
    server._ready_epoch, server._session = envelope.connection_epoch, envelope.session
    server._capabilities = frozenset({"colony_events.v1", "colony_attention.v1"})
    assert server.attention_available
    with pytest.raises(protocol.ValidationError):
        server._handle_game_event(envelope)
    assert server._event_cursor == 0
    server._capabilities = frozenset({"colony_events.v1"})
    assert not server.attention_available
    server._handle_game_event(envelope)
    assert server.game_events(0, expected_connection=envelope.connection_epoch, expected_session=envelope.session)[0]["data"] == envelope.payload.data


def test_existing_game_event_remains_generic_with_attention_capability():
    envelope = protocol.decode_envelope(json.dumps(VECTORS[0]).encode(), direction=protocol.Direction.MOD_TO_PLUGIN, authenticated=True)
    envelope.payload.data.clear()
    envelope.payload.data.update(cursor=1, kind="letter", def_name="ThreatBig")
    server = BridgeServer(TOKEN, port=0)
    server._ready_epoch, server._session = envelope.connection_epoch, envelope.session
    server._capabilities = frozenset({"colony_events.v1", "colony_attention.v1"})
    server._handle_game_event(envelope)
    assert server.game_events(0, expected_connection=envelope.connection_epoch, expected_session=envelope.session)[0]["kind"] == "letter"
