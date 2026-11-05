"""Registration traverses the authenticated bridge and returns native coverage."""

import asyncio
import copy

import pytest

from plugin.plugins.neko_rimworld.game.bridge import BridgeProtocolError, BridgeServer
from plugin.plugins.neko_rimworld.game.observation_contract import (
    validate_observation_request,
    validate_observation_result,
)
from plugin.plugins.neko_rimworld.game.protocol import (
    POLICY_REQUEST,
    POLICY_RESULT,
    SESSION_OPEN,
    Payload,
    SessionDescriptor,
    make_envelope,
    write_frame,
)
from plugin.plugins.neko_rimworld.game.write_port import BridgeGameWritePort
from plugin.plugins.neko_rimworld.tests.test_bridge import (
    TOKEN,
    connect_and_challenge,
    finish_authentication,
    read_until,
    wait_until,
)


def request():
    return {"source": "planner", "observations": [{"observation_id": "house-watch", "revision": 1, "conditions": [
        {"id": "walls", "kind": "construction", "map_id": "Map_0", "target_ids": ["Blueprint_1"], "max_samples": 10, "expires_at_tick": 10000}
    ]}]}


def acknowledgement(body):
    data = copy.deepcopy(body)
    for row in data["observations"]:
        for condition in row["conditions"]:
            condition["sample_interval_ticks"] = 250
    data["registrations"] = [{"observation_id": "house-watch", "revision": 1, "status": "registered", "covered_condition_ids": ["walls"], "receipt_id": "native-1"}]
    return data


@pytest.mark.parametrize("change", ["echo", "unknown_covered", "duplicate_condition", "bool_revision", "too_many_targets"])
def test_control_rejects_false_coverage_and_invalid_bounds(change):
    body = request()
    data = acknowledgement(body)
    if change == "echo":
        data.pop("registrations")
    elif change == "unknown_covered":
        data["registrations"][0]["covered_condition_ids"] = ["not-requested"]
    elif change == "duplicate_condition":
        body["observations"][0]["conditions"] *= 2
    elif change == "bool_revision":
        body["observations"][0]["revision"] = True
    else:
        body["observations"][0]["conditions"][0]["target_ids"] = [str(i) for i in range(65)]
    with pytest.raises(ValueError):
        if change in {"echo", "unknown_covered"}:
            validate_observation_result(data)
        else:
            validate_observation_request(body)


@pytest.mark.parametrize("wrong_target", [False, True])
async def test_authenticated_observation_registration_loopback(wrong_target):
    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_observation.v1", "colony_events.v1"))
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server.session is not None)
        bound = BridgeGameWritePort(server, hello.connection_epoch, session)
        task = asyncio.create_task(bound.push_observations(request()))
        incoming = await asyncio.to_thread(read_until, client, POLICY_REQUEST)
        data = acknowledgement(incoming.payload.arguments)
        if wrong_target:
            data["observations"][0]["conditions"][0]["target_ids"] = ["other"]
        write_frame(client, make_envelope(POLICY_RESULT, hello.connection_epoch, 3,
            Payload(status="ok", data=data, issues=[]), session=session, request_id=incoming.request_id))
        if wrong_target:
            with pytest.raises(BridgeProtocolError):
                await task
        else:
            assert await task == data
            assert await bound.events() == []
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_revoke_is_explicit_empty_replace_and_legacy_echo_stays_strict():
    validate_observation_request({"source": "planner", "observations": []})
    validate_observation_result({"source": "planner", "observations": [], "registrations": []})
    old = {"source": "frontdesk", "pause_on_letter_defs": []}
    assert BridgeServer._policy_result_matches(old, old)
    assert not BridgeServer._policy_result_matches({**old, "extra": True}, old)
