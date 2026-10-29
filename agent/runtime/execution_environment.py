"""Bind the current semantic consultation to caller-owned work capabilities."""

from __future__ import annotations

import copy

from ...tool_contracts import ToolSetup, ToolSpec
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
        if (session.role == "planner" and setup.write_catalog is not None
                and setup.write_port is not None and setup.write_identity is not None
                and setup.observation_source == "native" and setup.write_identity() is not None):
            from ..write.service import WriteService

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
                    raise

            service = WriteService(setup.write_catalog, session.observations, setup.write_port,
                                   setup.write_identity, source_kind=setup.observation_source,
                                   record=record_write)
            specs = []
            for schema in service.schemas():
                function = schema["function"]
                name = function["name"]
                handler = getattr(service, name)
                specs.append(ToolSpec(name, function["description"], function["parameters"], handler,
                                      exclusive=True))
            self.write_tools = tuple(specs)

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
