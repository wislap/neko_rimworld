"""Observation origin comes from trusted tool setup, never a game payload."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ..agent.world.semantic_tools import SemanticInteraction
from ..tool_contracts import ToolExecutionError, ToolSetup
from .test_semantic_tools import Session, _update, setup_for


def test_source_defaults_to_native_and_rejects_unknown_origin():
    assert ToolSetup(()).observation_source == 'native'
    with pytest.raises(ValueError):
        ToolSetup((), observation_source='untrusted')


@pytest.mark.asyncio
async def test_trusted_setup_overrides_payload_source_and_errors_keep_origin():
    async def read(args, *, tool_name):
        return {'data': {'source_kind': 'native', 'coverage': {'partial': True}}, 'status': 'partial'}
    stub = SimpleNamespace(original={'inspect_state': object()}, setup=SimpleNamespace(observation_source='offline_captured_graph'), execute_native=read)
    value = await SemanticInteraction._world_read(stub, {}, 'inspect_state')
    assert value['source_kind'] == 'offline_captured_graph'
    assert value['coverage']['partial']
    stub.setup.observation_source = 'native'
    assert (await SemanticInteraction._world_read(stub, {}, 'inspect_state'))['source_kind'] == 'native'

    interaction = SemanticInteraction(replace(setup_for(), observation_source='offline_captured_graph'), Session(), _update)
    async def spoof(args):
        return {'source_kind':'native', 'coverage':{'partial':True}}
    interaction.look = spoof
    look = next(tool for tool in interaction.read_tools() if tool.name == 'look')
    assert (await look.handler({}))['source_kind'] == 'offline_captured_graph'
    assert 'no live Verse reads' in look.description

    async def fail(args):
        raise ToolExecutionError('unavailable', 'not captured', details={'source_kind':'native'})
    interaction.look = fail
    look = next(tool for tool in interaction.read_tools() if tool.name == 'look')
    with pytest.raises(ToolExecutionError) as rejected:
        await look.handler({})
    assert rejected.value.details['source_kind'] == 'offline_captured_graph'
