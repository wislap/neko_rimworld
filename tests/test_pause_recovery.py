"""Recovery preserves native pause ownership through dispatch and execution."""

import threading
from types import SimpleNamespace

import pytest

from ..agent.runtime import execution_environment as environment
from ..agent.runtime.audit import AgentAuditLedger
from ..agent.runtime.session import AgentRunSession
from ..game.scripted_world import ScriptedGameWorld
from ..game.write_contract import validate_request
from ..game.write_port import BridgeGameWritePort
from ..tool_contracts import ToolExecutionError, ToolSetup


@pytest.mark.asyncio
async def test_guarded_recovery_rejects_changed_and_policy_owners():
    world = ScriptedGameWorld()
    await world.set_paused(True, role="frontdesk")
    with pytest.raises(ToolExecutionError, match="no longer current"):
        await world.set_paused(False, role="planner", expected_owner="planner")
    assert world.paused
    assert await world.set_paused(False, role="planner", expected_owner="frontdesk") == {
        "paused": False, "role": "planner", "expected_owner": "frontdesk"}
    await world.set_paused(True, role="policy")
    with pytest.raises(ToolExecutionError):
        await world.set_paused(False, role="planner", expected_owner="policy")
    with pytest.raises(ToolExecutionError):
        await world.set_paused(False, role="planner", expected_owner="frontdesk")
    assert world.paused


@pytest.mark.asyncio
async def test_model_recovery_requires_explicit_owner_and_native_support(monkeypatch):
    world = ScriptedGameWorld()
    monkeypatch.setattr(environment, "semantic_interaction_factory", lambda *args: object())
    session = AgentRunSession.create(ledger=AgentAuditLedger("test"), question="test", role="planner")
    session.explicit_scheduler = True
    tool = environment.ExecutionEnvironment(ToolSetup((), write_port=world), session, None).write_tools[0]
    for args in ({"paused": False}, {"paused": False, "expected_owner": "policy"},
                 {"paused": True, "expected_owner": "frontdesk"}):
        with pytest.raises(ToolExecutionError):
            await tool.handler(args)
    assert not session.write_outcomes
    await world.set_paused(True, role="frontdesk")
    result = await tool.handler({"paused": False, "expected_owner": "frontdesk"})
    assert result["expected_owner"] == "frontdesk"
    world.capabilities = {"colony_pause.v1"}
    with pytest.raises(ToolExecutionError, match="atomically guard"):
        await tool.handler({"paused": False, "expected_owner": "frontdesk"})


@pytest.mark.asyncio
async def test_bound_port_forwards_guard_without_losing_identity():
    calls = []
    class Bridge:
        capabilities = {"colony_pause.v1", "colony_pause.resume_guard.v1"}
        def set_paused(self, paused, role, **kwargs):
            calls.append((paused, role, kwargs))
            return SimpleNamespace(payload=SimpleNamespace(status="ok", data={
                "paused": paused, "role": role, "expected_owner": kwargs["expected_owner"]}))
    session = SimpleNamespace(load_epoch="e")
    port = BridgeGameWritePort(Bridge(), "connection", session)
    await port.set_paused(False, role="planner", expected_owner="frontdesk")
    assert calls[0][:2] == (False, "planner")
    assert calls[0][2]["expected_owner"] == "frontdesk"
    assert calls[0][2]["expected_connection"] == "connection"
    assert calls[0][2]["expected_session"] is session
    assert not calls[0][2]["cancel_event"].is_set()


@pytest.mark.parametrize("owner", ["user", "policy", "unknown", "none", None, 1])
def test_guard_contract_rejects_unsupported_owner(owner):
    with pytest.raises(ValueError):
        validate_request("set_paused", {"paused": False, "role": "planner", "expected_owner": owner})


@pytest.mark.parametrize("returned_owner", ["frontdesk", "planner"])
def test_bridge_requires_exact_guard_receipt(returned_owner):
    from ..game.bridge import BridgeProtocolError, BridgeServer
    from ..game.protocol import (
        SESSION_OPEN,
        WRITE_REQUEST,
        WRITE_RESULT,
        Payload,
        SessionDescriptor,
        make_envelope,
        write_frame,
    )
    from .test_bridge import TOKEN, connect_and_challenge, finish_authentication, read_until, wait_until

    server = BridgeServer(TOKEN, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("colony_pause.v1", "colony_pause.resume_guard.v1"))
    results, errors = [], []
    def invoke():
        try:
            results.append(server.set_paused(False, "planner", timeout=2, expected_owner="frontdesk"))
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=invoke)
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: server.session is not None)
        thread.start()
        request = read_until(client, WRITE_REQUEST)
        assert request.payload.arguments == {"paused": False, "role": "planner", "expected_owner": "frontdesk"}
        write_frame(client, make_envelope(WRITE_RESULT, hello.connection_epoch, 3,
            Payload(status="ok", issues=[], data={"paused": False, "role": "planner", "expected_owner": returned_owner}),
            session=session, request_id=request.request_id))
        thread.join(3)
        assert not thread.is_alive()
        if returned_owner == "frontdesk":
            assert len(results) == 1 and not errors
        else:
            assert not results and len(errors) == 1 and isinstance(errors[0], BridgeProtocolError)
    finally:
        client.close()
        assert server.stop(timeout=2)
        if thread.ident is not None:
            thread.join(3)


def test_legacy_bridge_does_not_dispatch_guarded_recovery():
    from ..game.bridge import BridgeServer, BridgeUnsupportedError

    server = BridgeServer("a" * 32, port=0)
    server._capabilities = frozenset({"colony_pause.v1"})
    with pytest.raises(BridgeUnsupportedError):
        server.set_paused(False, "planner", expected_owner="frontdesk")
    assert not server._pending
