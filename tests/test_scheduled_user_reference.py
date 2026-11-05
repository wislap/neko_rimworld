import json
from types import SimpleNamespace

from ..agent.runtime.audit import AgentAuditRegistry
from ..agent.runtime.loop import AgentRuntime
from ..agent.runtime.frontdesk_service import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings
from .test_capability_composition import Script, call


async def test_scheduled_user_reference_is_visible_and_points_to_verbatim_original(tmp_path):
    words = '把这个储存区交还给你，接着管。'
    game = SimpleNamespace(colony_id='colony', load_epoch='epoch', game_tick=10)
    class Model(Script):
        async def complete(self, messages, tools, *, timeout):
            references = [json.loads(m['content']) for m in messages if m.get('name') == 'user_event_reference']
            assert len(references) == 1
            event = await service.call('event', game.colony_id, references[0]['user_event_seq'])
            assert event['kind'] == 'user.utterance' and event['payload']['text'] == words
            assert any(m.get('role') == 'user' and m.get('content') == words for m in messages)
            return await super().complete(messages, tools, timeout=timeout)
    model = Model([call('submit_response', {'answer': '已收到原话。'})])
    runtime = AgentRuntime(audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           tools_provider=semantic_tool_provider(), model_factory=lambda _: model)
    service = FrontdeskService(tmp_path / 'events.sqlite', runtime, game_session=lambda: game)
    try:
        result = await service.consult(game, words, settings(), explicit_update_only=True)
        assert result['status'] == 'ok' and len(model.requests) == 1
    finally:
        await service.close()
