import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.game.observation import Observation
from plugin.plugins.neko_rimworld.game.query_tools import CatalogTools

from .test_catalog_tools import _public_source, response


@pytest.mark.asyncio
async def test_catalog_write_capability_and_epoch_binding():
    directory = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    class Bridge:
        connection_key = '1' * 32
        capabilities = frozenset({'colony_write.transactions.v1'})
        session = SimpleNamespace(colony_id='colony', load_epoch='one')
    bridge = Bridge()
    calls = []
    async def query(args):
        calls.append(copy.deepcopy(args))
        raw = response().to_dict()
        raw.update(connection_epoch=bridge.connection_key, colony_id=bridge.session.colony_id, load_epoch=bridge.session.load_epoch)
        catalog = _public_source()
        catalog['colony_write'] = directory
        raw['payload'] = {'status': 'ok', 'issues': [], 'data': catalog}
        return Observation({'response': raw})
    provider = CatalogTools(lambda: bridge, query)
    first = await provider.load()
    assert first.write_catalog == directory and first.write_identity() == ('1' * 32, 'colony', 'one')
    bridge.session = SimpleNamespace(colony_id='colony', load_epoch='two')
    assert first.write_identity() is None
    second = await provider.load()
    assert second.write_identity() == ('1' * 32, 'colony', 'two') and len(calls) == 2
    bridge.capabilities = frozenset()
    third = await provider.load()
    assert third.write_port is None

@pytest.mark.asyncio
async def test_catalog_cannot_bind_a_session_changed_during_setup():
    directory = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    class Bridge:
        connection_key = '1' * 32
        session = SimpleNamespace(colony_id='colony', load_epoch='one')
        @property
        def capabilities(self):
            self.session = SimpleNamespace(colony_id='colony', load_epoch='two')
            return frozenset({'colony_write.transactions.v1'})
    bridge = Bridge()
    async def query(_):
        raw = response().to_dict()
        raw.update(connection_epoch=bridge.connection_key, colony_id='colony', load_epoch='one')
        catalog = _public_source()
        catalog['colony_write'] = directory
        raw['payload'] = {'status': 'ok', 'issues': [], 'data': catalog}
        return Observation({'response': raw})
    setup = await CatalogTools(lambda: bridge, query).load()
    assert not setup.tools and setup.write_port is None

@pytest.mark.asyncio
async def test_invalid_write_catalog_is_not_published():
    directory = json.loads((Path(__file__).parent / 'fixtures/colony-write-v1/catalog.json').read_text(encoding='utf-8'))
    directory['operations'][0]['op'] = 'tampered'
    class Bridge:
        connection_key = '1' * 32
        session = SimpleNamespace(colony_id='colony', load_epoch='one')
        capabilities = frozenset({'colony_write.transactions.v1'})
    bridge = Bridge()
    async def query(_):
        raw = response().to_dict()
        raw.update(connection_epoch=bridge.connection_key, colony_id='colony', load_epoch='one')
        catalog = _public_source()
        catalog['colony_write'] = directory
        raw['payload'] = {'status': 'ok', 'issues': [], 'data': catalog}
        return Observation({'response': raw})
    setup = await CatalogTools(lambda: bridge, query).load()
    assert not setup.tools and setup.write_port is None
