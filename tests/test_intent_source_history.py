"""Original intent sources survive revisions without reviving closed intents."""
import json
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditRegistry
from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.loop import AgentRuntime
from ..agent.runtime.frontdesk_service import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings
from .test_capability_composition import Script, call


def intent(seq, revision=0, status='open'):
    return {'entry_id': 'standing', 'expected_revision': revision, 'kind': 'intent', 'status': status,
            'priority': 1, 'parent': None, 'source': {'event_seq': seq}, 'evidence': [],
            'review_at_tick': None, 'changesets': [], 'body': 'Current intent'}


@pytest.mark.asyncio
async def test_active_intent_source_history_in_both_role_requests_and_reopen(tmp_path):
    game = SimpleNamespace(colony_id='colony', load_epoch='old', game_tick=1)
    planner = Script([call('planner_yield', {'handoff': 'done'})])
    early_frontdesk = Script([call('submit_response', {'answer': 'done'})])
    frontdesk = Script([call('submit_response', {'answer': 'done'})])
    reopened_frontdesk = Script([call('submit_response', {'answer': 'done'})])
    models = iter([early_frontdesk, frontdesk, planner, reopened_frontdesk])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: next(models), tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / 'events.sqlite', runtime, game_session=lambda: game)
    first = await service.call('record_utterance', 'colony', 'old', 1, 'Original standing authorization')
    await service.call('submit', 'colony', 'old', 1, 'frontdesk', intent(first))
    second = await service.call('record_utterance', 'colony', 'old', 2, 'Later related words')
    await service.call('submit', 'colony', 'old', 2, 'frontdesk', intent(second, 1))
    try:
        game.game_tick = 180_000
        await service.consult(game, 'Earlier question', settings())
        early = str(early_frontdesk.requests[0]['messages'])
        assert early.count('Original standing authorization') == 1
        assert early.count('Later related words') == 1
        game.game_tick = 200_001
        game.load_epoch = 'new'
        await service.consult(game, 'Current question', settings())
        front = next(m['content'] for m in frontdesk.requests[0]['messages'] if m.get('name') == 'colony_board')
        context = json.loads(front.split(': ', 1)[1])
        sources = context['intent_source_history'][0]['sources']
        assert sources[0]['event_seq'] == first and sources[0]['text'] == 'Original standing authorization'
        assert sources[1]['event_seq'] == second and sources[1]['status'] == 'referenced'
        assert 'text' not in sources[1]
        assert 'not independent current authorization' in context['intent_source_history'][0]['meaning']
        assert str(frontdesk.requests[0]['messages']).count('Original standing authorization') == 1
        assert str(frontdesk.requests[0]['messages']).count('Later related words') == 1
        scheduler = ColonyScheduler(service, settings())
        scheduler.start()
        await scheduler.wake(game, source='plugin', text='Review')
        import asyncio
        for _ in range(500):
            if planner.requests:
                break
            await asyncio.sleep(.01)
        assert planner.requests
        request = planner.requests[0]['messages']
        assert any('Original standing authorization' in str(message) and 'Later related words' in str(message)
                   and 'not independent current authorization' in str(message) for message in request)
        await scheduler.close()
        third = await service.call('record_utterance', 'colony', 'new', game.game_tick, 'Withdraw prior intent')
        await service.call('submit', 'colony', 'new', game.game_tick, 'frontdesk', intent(third, 2, 'closed'))
        assert await service.call('intent_source_history', 'colony') == []
        fourth = await service.call('record_utterance', 'colony', 'new', game.game_tick, 'Reopen with new scope')
        await service.call('submit', 'colony', 'new', game.game_tick, 'frontdesk', intent(fourth, 3, 'open'))
        histories = await service.call('intent_source_history', 'colony')
        assert [x['event_seq'] for x in histories[0]['sources']] == [fourth]
        assert 'Original standing authorization' not in str(histories)
        await service.consult(game, 'After reopening', settings())
        reopened = next(m['content'] for m in reopened_frontdesk.requests[0]['messages'] if m.get('name') == 'colony_board')
        reopened_context = json.loads(reopened.split(': ', 1)[1])
        assert [x['event_seq'] for x in reopened_context['intent_source_history'][0]['sources']] == [fourth]
        assert 'Original standing authorization' not in str(reopened_context['intent_source_history'])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repeated_source_revisions_are_references_and_missing_history_is_explicit(tmp_path):
    game = SimpleNamespace(colony_id='colony', load_epoch='e', game_tick=1)
    service = FrontdeskService(tmp_path / 'events.sqlite', None, game_session=lambda: game)
    a = await service.call('record_utterance', 'colony', 'e', 1, 'First words')
    b = await service.call('record_utterance', 'colony', 'e', 2, 'Second words')
    await service.call('submit', 'colony', 'e', 1, 'frontdesk', intent(a))
    await service.call('submit', 'colony', 'e', 2, 'frontdesk', intent(b, 1))
    await service.call('submit', 'colony', 'e', 3, 'frontdesk', intent(a, 2))
    try:
        rows = await service.call('intent_source_history', 'colony', referenced=[a])
        sources = rows[0]['sources']
        assert [(item['event_seq'], item['revisions']) for item in sources] == [(a, [1, 3]), (b, [2])]
        assert 'text' not in sources[0] and sources[1]['text'] == 'Second words'
        journal = service.journal
        original = journal._ledger.event
        journal._ledger.event = lambda colony, seq: None if seq == b else original(colony, seq)
        missing = await service.call('intent_source_history', 'colony')
        assert missing[0]['sources'][1] == {'event_seq': b, 'revisions': [2], 'status': 'unavailable'}
        journal._ledger.event = lambda colony, seq: None if seq == a else original(colony, seq)
        with pytest.raises(ValueError, match='Current intent source'):
            await service.call('intent_source_history', 'colony')
        journal._ledger.event = lambda colony, seq: None if seq == b else original(colony, seq)
        model = Script([call('submit_response', {'answer': 'done'})])
        service.runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                                       model_factory=lambda _: model, tools_provider=semantic_tool_provider())
        game.game_tick = 200_001
        await service.consult(game, 'Continue', settings())
        board_message = next(m['content'] for m in model.requests[0]['messages'] if m.get('name') == 'colony_board')
        context = json.loads(board_message.split(': ', 1)[1])
        assert context['intent_source_history'][0]['sources'][1]['status'] == 'unavailable'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_history_query_does_not_decode_unrelated_board_revisions(tmp_path, monkeypatch):
    from ..workspace import ledger as ledger_module

    game = SimpleNamespace(colony_id='colony', load_epoch='e', game_tick=1)
    service = FrontdeskService(tmp_path / 'events.sqlite', None, game_session=lambda: game)
    first = await service.call('record_utterance', 'colony', 'e', 1, 'Intent words')
    await service.call('submit', 'colony', 'e', 1, 'frontdesk', intent(first))
    strategy = {'entry_id': 'strategy', 'expected_revision': 0, 'kind': 'strategy', 'status': 'open',
                'priority': 1, 'parent': None, 'source': {}, 'evidence': [], 'review_at_tick': None,
                'changesets': [], 'body': 'large-unrelated' * 10000}
    await service.call('submit', 'colony', 'e', 1, 'planner', strategy)
    original = ledger_module._revision
    def checked(row):
        assert row['kind'] == 'intent'
        return original(row)
    monkeypatch.setattr(ledger_module, '_revision', checked)
    try:
        history = await service.call('intent_source_history', 'colony', referenced=[first])
        assert history[0]['sources'][0]['event_seq'] == first
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_undelivered_pending_source_is_not_claimed_visible_or_injected(tmp_path):
    game = SimpleNamespace(colony_id='colony', load_epoch='e', game_tick=10)
    model = Script([call('submit_response', {'answer': 'done'})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, tools_provider=semantic_tool_provider())
    service = FrontdeskService(tmp_path / 'events.sqlite', runtime, game_session=lambda: game)
    first = await service.call('record_utterance', 'colony', 'e', 10, 'First visible words')
    pending = await service.call('record_utterance', 'colony', 'e', 10, 'Still undelivered words')
    await service.call('submit', 'colony', 'e', 10, 'frontdesk', intent(pending))
    try:
        await service.consult(game, 'First visible words', settings(), utterance_seq=first,
                              pending_utterances=[first, pending])
        messages = model.requests[0]['messages']
        context = json.loads(next(m['content'] for m in messages if m.get('name') == 'colony_board').split(': ', 1)[1])
        source = context['intent_source_history'][0]['sources'][0]
        assert source['status'] == 'pending' and source['event_seq'] == pending and 'text' not in source
        assert 'Still undelivered words' not in str(messages)
    finally:
        await service.close()
