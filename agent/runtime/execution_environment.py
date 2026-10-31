"""Bind the current semantic consultation to caller-owned work capabilities."""

from __future__ import annotations

import copy

from ...game.bridge import BridgeNotDispatched
from ...tool_contracts import ToolExecutionError, ToolSetup, ToolSpec, binding_is_current
from ..world.semantic_tools import semantic_interaction_factory
from .tools import ToolRegistry


def compose_tools(game_tools, capabilities):
    result = tuple(game_tools) + tuple(capabilities)
    names = [tool.name for tool in result]
    if len(names) != len(set(names)):
        raise ValueError("Game tools and work capabilities must have distinct names")
    return result


class ExecutionEnvironment:
    def __init__(self, setup: ToolSetup, session, update):
        self.setup = setup
        self.session = session
        session.interaction = semantic_interaction_factory(setup, session, update)
        self.write_tools = ()
        port = setup.write_port
        if (session.explicit_scheduler and setup.observation_source == "native" and port is not None
                and "colony_pause.v1" in getattr(port, "capabilities", ())):
            async def set_paused(args):
                paused = args.get("paused")
                expected_owner = args.get("expected_owner")
                if type(paused) is not bool or set(args) - {"paused", "expected_owner"}:
                    raise ToolExecutionError("invalid_tool_arguments", "paused must be a boolean")
                if session.role == "frontdesk" and not paused:
                    raise ToolExecutionError("pause_forbidden", "The frontdesk may only pause.")
                if paused and "expected_owner" in args:
                    raise ToolExecutionError("invalid_tool_arguments", "expected_owner is only for recovery.")
                if not paused:
                    if expected_owner not in ("planner", "frontdesk", "reflex", "automatic_letter"):
                        raise ToolExecutionError("pause_owner_required", "Name the currently observed, authorized agent pause owner; never user, unknown or policy.")
                    if "colony_pause.resume_guard.v1" not in port.capabilities:
                        raise ToolExecutionError("unsupported", "This Mod cannot atomically guard pause recovery; explicit operator recovery is required.")
                request = {"paused": paused, "role": session.role}
                if not paused:
                    request["expected_owner"] = expected_owner
                try:
                    if session.persist_progress is not None:
                        await session.persist_progress({"pause_request": request})
                except BaseException:
                    session.write_record_issue = "prepared"
                    session.write_record_context = {"mode": "set_paused", "outcome": "not_sent"}
                    raise
                if not binding_is_current(session.binding_check):
                    raise ToolExecutionError("world_changed", "The active run is no longer authorized.", recoverable=False)
                try:
                    result = await port.set_paused(paused, role=session.role, **({"expected_owner": expected_owner} if not paused else {}))
                except BridgeNotDispatched as error:
                    session.write_outcomes.append({"stage": "result", "mode": "set_paused", "outcome": "not_sent"})
                    raise ToolExecutionError("write_not_dispatched", "The pause request was not sent.",
                                             recoverable=False, details={"outcome": "not_sent"}) from error
                except ToolExecutionError:
                    raise
                except BaseException:
                    session.write_record_issue = "unknown"
                    session.write_record_context = {"mode": "set_paused", "outcome": "unknown"}
                    raise
                session.write_outcomes.append({"stage": "result", "mode": "set_paused", "receipt": copy.deepcopy(result)})
                try:
                    if session.persist_progress is not None:
                        await session.persist_progress({"pause_result": copy.deepcopy(result)})
                except BaseException:
                    session.write_record_issue = "result"
                    session.write_record_context = {"mode": "set_paused", "outcome": "known"}
                    raise
                return result

            self.write_tools = (ToolSpec("set_paused", "Pause the game" +
                (" or resume a currently observed and user-authorized agent pause. Recovery requires expected_owner and native atomic support; user, unknown, policy and force pauses cannot be recovered. A changed owner rejects the request; do not retry blindly" if session.role == "planner" else "") + ".",
                {"type": "object", "properties": {"paused": {"type": "boolean"},
                    **({"expected_owner": {"type": "string", "enum": ["planner", "frontdesk", "reflex", "automatic_letter"]}} if session.role == "planner" else {})},
                 "required": ["paused"], "additionalProperties": False}, set_paused, exclusive=True),)
        if (session.role == "planner" and setup.write_catalog is not None
                and setup.write_port is not None and setup.write_identity is not None
                and setup.observation_source == "native" and setup.write_identity() is not None):
            from ..write.service import WriteService

            def authorized_identity():
                return setup.write_identity() if binding_is_current(session.binding_check) else None

            async def record_write(event):
                stage = event["stage"]
                if stage == "result":
                    session.write_outcomes.append(copy.deepcopy(event))
                try:
                    session.ledger.record("write_" + stage, event)
                    if session.persist_progress is not None:
                        await session.persist_progress({"write": copy.deepcopy(event)})
                except BaseException:
                    session.write_record_issue = stage
                    session.write_record_context = {key: event[key] for key in ("mode", "outcome", "attempt") if key in event}
                    raise

            guard = None
            if session.explicit_scheduler and session.takeover_service is None:
                raise ToolExecutionError("takeover_unknown", "The takeover journal is unavailable.", recoverable=False)
            if session.takeover_service is not None:
                from ...workspace.takeovers import TakeoverControl
                guard = TakeoverControl(session.takeover_service, setup.ledger_query, authorized_identity,
                                        setup.write_fields_query)
            service = WriteService(setup.write_catalog, session.observations, setup.write_port,
                                   authorized_identity, source_kind=setup.observation_source,
                                   record=record_write, guard=guard)
            specs = []
            for schema in service.schemas():
                function = schema["function"]
                name = function["name"]
                handler = getattr(service, name)
                specs.append(ToolSpec(name, function["description"], function["parameters"], handler,
                                      exclusive=True))
            self.write_tools += tuple(specs)

    def registry(self):
        session = self.session
        registry = ToolRegistry(context_pruner=session.context_pruner)
        work = (() if session.interaction.finalizing else session.capabilities + self.write_tools)
        if (session.planner_terminal or session.frontdesk_update_only) and session.interaction.finalizing:
            work = tuple(spec for spec in session.capabilities if spec.terminal)
        base = session.interaction.tools()
        if session.planner_terminal:
            base = tuple(spec for spec in base if spec.name != "submit_response")
        for spec in compose_tools(base, work):
            registry.register(spec)
        return registry
