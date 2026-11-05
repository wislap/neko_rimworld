import asyncio
import json
from types import SimpleNamespace

import pytest

from ..agent.runtime.audit import AgentAuditRegistry
from ..agent.runtime.colony_scheduler import ColonyScheduler
from ..agent.runtime.loop import AgentRuntime
from ..agent.runtime.frontdesk_service import FrontdeskService
from .consultation_setup import semantic_tool_provider
from .test_agent import FakeLogger, settings
from .test_attention_journal import CHOICES, native
from .test_capability_composition import Script, call


@pytest.mark.asyncio
async def test_diagnostic_clock_stays_raw_but_not_in_actual_model_inputs(tmp_path):
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    model = Script([call("planner_yield", {"handoff": "ready"}), call("update_only", {})])
    runtime = AgentRuntime(
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        tools_provider=semantic_tool_provider(),
    )
    service = FrontdeskService(tmp_path / "events.sqlite", runtime, game_session=lambda: game)
    diagnostic = {"domain": "diagnostic-clock-domain", "frequency": 1000000, "counter": 987654321}
    startup_pause = {
        "status": "observed",
        "monotonic": diagnostic,
        "business": {"monotonic": "business-field-must-stay"},
    }
    startup_seq = await service.call(
        "record", "c", "e", 0, "planner", "autoplay.startup_pause", {"stage": "result", "pause_state": startup_pause}
    )
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    scheduler = ColonyScheduler(service, settings())
    scheduler.start()
    try:
        scheduler.resume(game)
        for _ in range(500):
            events = await service.call("events", "c")
            if any(event["kind"] == "planner.finished" for event in events):
                break
            await asyncio.sleep(0.01)
        raw_startup = await service.call("event", "c", startup_seq)
        assert raw_startup["payload"]["pause_state"]["monotonic"] == diagnostic
        planner_input = str(model.requests[0]["messages"])
        assert "diagnostic-clock-domain" not in planner_input and "987654321" not in planner_input
        assert "business-field-must-stay" in planner_input
        data = {
            "source": "window",
            "windows_force_paused": True,
            "monotonic": {"domain": "outer-clock-domain", "frequency": 42, "counter": 101},
            "pause_state": {"status": "observed", "monotonic": diagnostic},
            "business": {"monotonic": "business-event-field-must-stay"},
        }
        raw = native(1, 0, "pause_changed", data)
        await service.call("ingest_game_event", "c", "e", 0, "conn", raw)
        scheduler.resume(game)
        await asyncio.wait_for(scheduler.next_frontdesk_result(), 5)
        events = await service.call("events", "c")
        stored = next(event for event in events if event["kind"] == "game.event")
        assert stored["payload"]["event"] == raw
        trigger = next(event for event in events if event["kind"] == "frontdesk.trigger")
        presented = json.loads(trigger["payload"]["text"])["event"]["data"]
        assert "monotonic" not in presented and "monotonic" not in presented["pause_state"]
        assert presented["business"]["monotonic"] == "business-event-field-must-stay"
        frontdesk_input = str(model.requests[1]["messages"])
        assert "outer-clock-domain" not in frontdesk_input and "diagnostic-clock-domain" not in frontdesk_input
        assert "business-event-field-must-stay" in frontdesk_input
    finally:
        await scheduler.close()
        await service.close()
