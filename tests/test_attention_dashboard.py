import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..workspace.journal import Journal
from ..agent.runtime.frontdesk_service import FrontdeskService
from .test_attention_journal import CHOICES, native
from .test_plugin import make_plugin


def snapshot_data():
    path = Path(__file__).parent / "fixtures/colony-attention-v1/vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))["valid"][0]["payload"]["data"]


def test_attention_display_is_read_only_and_requires_current_connection(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    assert journal.attention_display("c", "e", "conn")["source_status"] == "unavailable"
    journal.ingest_game_event(
        "c", "e", 250, "conn", native(1, 250, "attention_snapshot", snapshot_data()), attention_available=True
    )
    before = journal.events("c")
    display = journal.attention_display("c", "e", "conn")
    assert display["level"] == "urgent" and display["sampled_tick"] == 250
    assert display["reasons"] and display["reasons"][0]["signal"]["priority"] == "Critical"
    assert journal.attention_display("c", "e", "conn") == display
    assert journal.attention_display("c", "e", "new-connection")["source_status"] == "unavailable"
    assert journal.events("c") == before
    journal.close()


@pytest.mark.asyncio
async def test_dashboard_reads_current_scope_and_reports_delivery_outcome_without_side_effects(tmp_path):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=250)
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn", attention_available=True, running=True)
    plugin._autoplay.budget = None
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    plugin._workspace_service = service
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    await service.call(
        "ingest_game_event",
        "c",
        "e",
        250,
        "conn",
        native(1, 250, "attention_snapshot", snapshot_data()),
        attention_available=True,
    )
    state = {
        "status": "observed",
        "connection_epoch": "conn",
        "colony_id": "c",
        "load_epoch": "e",
        "sampled_tick": 250,
        "owner": "planner",
        "pause_revision": 4,
        "pause_transition_revision": 3,
        "paused": True,
        "speed_paused": True,
        "force_paused": False,
        "windows_force_paused": False,
    }
    await service.call(
        "ingest_game_event", "c", "e", 250, "conn", native(2, 250, "pause_changed", {"pause_state": state})
    )
    episode = ("conn", "c", "e", 4, 3)
    await service.call("pause_watchdog_wake", "c", "e", 250, episode, 1)
    await service.call("pause_watchdog_notify", "c", "e", 250, episode)
    await service.call(
        "record",
        "c",
        "e",
        250,
        "plugin",
        "pause.watchdog",
        {"episode": list(episode), "action": "notify_result", "outcome": "rejected"},
    )
    plugin._autoplay.scheduler = SimpleNamespace(pause_state=state)
    before = await service.call("events", "c")
    try:
        dashboard = await plugin.dashboard_ui_context()
        attention = dashboard["autoplay"]["attention"]
        assert attention["available"] and attention["level"] == "urgent"
        assert attention["reasons"][0]["signal"]["priority"] == "Critical"
        assert dashboard["autoplay"]["pause_watchdog"] == {
            "paused": True,
            "owner": "planner",
            "sampled_tick": 250,
            "source_status": "observed",
            "wake_count": 1,
            "notification": "rejected",
        }
        await plugin.dashboard_ui_context()
        assert await service.call("events", "c") == before
        plugin._bridge.connection_key = "new-connection"
        stale = await plugin.dashboard_ui_context()
        assert stale["autoplay"]["attention"]["source_status"] == "unavailable"
        assert stale["autoplay"]["pause_watchdog"]["source_status"] == "unavailable"
        assert stale["autoplay"]["pause_watchdog"]["notification"] is None
    finally:
        await service.close()


def test_incomplete_baseline_marks_source_partial_without_hiding_cadence(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    data = snapshot_data()
    for section in ("alerts", "conditions", "threats"):
        data[section]["rows"] = []
    data["alerts"]["status"] = "partial"
    journal.ingest_game_event(
        "c", "e", 250, "conn", native(1, 250, "attention_snapshot", data), attention_available=True
    )
    assert journal.attention_display("c", "e", "conn")["source_status"] == "partial"
    assert journal.attention_display("c", "e", "conn")["level"] == "steady"
    data["alerts"]["status"] = "ok"
    journal.ingest_game_event(
        "c", "e", 500, "conn", native(2, 500, "attention_snapshot", data), attention_available=True
    )
    assert journal.attention_display("c", "e", "conn")["level"] == "steady"
    journal.close()


def test_display_reasons_follow_active_sources_when_level_does_not_change(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    data = snapshot_data()
    journal.ingest_game_event(
        "c", "e", 250, "conn", native(1, 250, "attention_snapshot", data), attention_available=True
    )
    changed = {
        "category": "alerts",
        "source_id": "second",
        "edge": "appeared",
        "map_id": "Map_1",
        "current": {
            "id": "second",
            "runtime_type": "RimWorld.Alert_FireInHomeArea",
            "priority": "Critical",
            "targets_complete": True,
            "targets": [],
        },
        "previous": None,
    }
    journal.ingest_game_event(
        "c", "e", 500, "conn", native(2, 500, "attention_changed", changed), attention_available=True
    )
    display = journal.attention_display("c", "e", "conn")
    assert display["level"] == "urgent"
    assert {row["signal"].get("runtime_type") for row in display["reasons"] if row["signal"].get("runtime_type")} == {
        "RimWorld.Alert_ColonistNeedsRescuing",
        "RimWorld.Alert_FireInHomeArea",
    }
    assert sum(event["kind"] == "cadence.changed" for event in journal.events("c")) == 1
    journal.close()


@pytest.mark.asyncio
async def test_dashboard_capability_unknown_until_bound_projection_then_explicit_missing(tmp_path):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn", attention_available=False, running=True)
    plugin._autoplay.budget = None
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    plugin._workspace_service = service
    plugin._autoplay.scheduler = SimpleNamespace(pause_state=None)
    try:
        unprojected = await plugin.dashboard_ui_context()
        assert unprojected["autoplay"]["attention"]["available"] is None
        assert unprojected["autoplay"]["pause_watchdog"]["wake_count"] is None
        assert unprojected["autoplay"]["pause_watchdog"]["notification"] is None
        await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
        missing = await plugin.dashboard_ui_context()
        assert missing["autoplay"]["attention"]["available"] is False
        assert missing["autoplay"]["attention"]["level"] == "steady"
        assert missing["autoplay"]["attention"]["source_status"] == "unavailable"
        assert missing["autoplay"]["attention"]["next_daily_tick"] == 60000
        plugin._bridge.session = None
        no_session = await plugin.dashboard_ui_context()
        assert no_session["autoplay"]["attention"]["available"] is None
        plugin._bridge.connection_key = None
        no_connection = await plugin.dashboard_ui_context()
        assert no_connection["autoplay"]["attention"]["available"] is None
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("paused,owner", [(False, "none"), (True, "user"), (True, "unknown")])
async def test_dashboard_does_not_claim_watchdog_counts_without_eligible_episode(tmp_path, paused, owner):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=0)
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn", attention_available=True, running=True)
    plugin._autoplay.budget = None
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    plugin._workspace_service = service
    state = {
        "status": "observed",
        "connection_epoch": "conn",
        "colony_id": "c",
        "load_epoch": "e",
        "sampled_tick": 0,
        "owner": owner,
        "pause_revision": 1,
        "pause_transition_revision": 1,
        "paused": paused,
        "speed_paused": paused,
        "force_paused": False,
        "windows_force_paused": False,
    }
    plugin._autoplay.scheduler = SimpleNamespace(pause_state=state)
    try:
        dashboard = await plugin.dashboard_ui_context()
        watchdog = dashboard["autoplay"]["pause_watchdog"]
        assert watchdog["source_status"] == "observed"
        assert watchdog["paused"] is paused and watchdog["owner"] == owner
        assert watchdog["wake_count"] is None and watchdog["notification"] is None
    finally:
        await service.close()


def test_display_tracks_native_partition_status_changes_after_snapshot(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    data = snapshot_data()
    for section in ("alerts", "conditions", "threats"):
        data[section]["rows"] = []
    journal.ingest_game_event(
        "c", "e", 250, "conn", native(1, 250, "attention_snapshot", data), attention_available=True
    )
    initial = journal.attention_display("c", "e", "conn")
    assert initial["level"] == "steady" and initial["source_status"] == "observed"

    def status(previous, current):
        return {
            "category": "alerts",
            "source_id": "alerts",
            "edge": "status_changed",
            "map_id": "Map_1",
            "previous": {"status": previous, "rows": []},
            "current": {"status": current, "rows": []},
        }

    journal.ingest_game_event(
        "c", "e", 500, "conn", native(2, 500, "attention_changed", status("ok", "partial")), attention_available=True
    )
    partial = journal.attention_display("c", "e", "conn")
    assert partial["level"] == "steady" and partial["source_status"] == "partial" and partial["sampled_tick"] == 500
    journal.ingest_game_event(
        "c", "e", 750, "conn", native(3, 750, "attention_changed", status("partial", "ok")), attention_available=True
    )
    before = journal.events("c")
    recovered = journal.attention_display("c", "e", "conn")
    assert (
        recovered["level"] == "steady" and recovered["source_status"] == "observed" and recovered["sampled_tick"] == 750
    )
    assert journal.events("c") == before
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["connection", "epoch"])
@pytest.mark.parametrize("fail", [False, True])
async def test_dashboard_scope_change_during_projection_returns_unknown(tmp_path, change, fail):
    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=250)
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn", attention_available=True, running=True)
    plugin._autoplay.budget = None
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    plugin._workspace_service = service
    await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
    await service.call(
        "ingest_game_event",
        "c",
        "e",
        250,
        "conn",
        native(1, 250, "attention_snapshot", snapshot_data()),
        attention_available=True,
    )
    state = {
        "status": "observed",
        "connection_epoch": "conn",
        "colony_id": "c",
        "load_epoch": "e",
        "sampled_tick": 250,
        "owner": "planner",
        "pause_revision": 4,
        "pause_transition_revision": 3,
        "paused": True,
        "speed_paused": True,
        "force_paused": False,
        "windows_force_paused": False,
    }
    plugin._autoplay.scheduler = SimpleNamespace(pause_state=state)
    assert (await plugin.dashboard_ui_context())["autoplay"]["attention"]["level"] == "urgent"
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.call

    async def delayed(method, *args, **kwargs):
        result = await original(method, *args, **kwargs)
        if method == "attention_display":
            entered.set()
            await release.wait()
            if fail:
                raise RuntimeError("injected projection failure")
        return result

    service.call = delayed
    task = asyncio.create_task(plugin.dashboard_ui_context())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "connection":
            plugin._bridge.connection_key = "new-connection"
        else:
            game.load_epoch = "new-epoch"
        release.set()
        result = await task
        assert ("panel_error" in result) is fail
        assert result["autoplay"]["attention"]["available"] is None
        assert result["autoplay"]["attention"]["level"] is None
        assert result["autoplay"]["attention"]["sampled_tick"] is None
        assert result["autoplay"]["pause_watchdog"]["source_status"] == "unavailable"
        assert result["autoplay"]["pause_watchdog"]["owner"] is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["user", "planner"])
async def test_dashboard_shows_valid_declaration_when_native_capability_missing(tmp_path, actor):
    from .test_attention_journal import entry

    plugin, _ = make_plugin({"neko_rimworld": {}})
    game = SimpleNamespace(colony_id="c", load_epoch="e", game_tick=1)
    plugin._bridge = SimpleNamespace(session=game, connection_key="conn", attention_available=False, running=True)
    plugin._autoplay.budget = None
    service = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    plugin._workspace_service = service
    plugin._autoplay.scheduler = SimpleNamespace(pause_state=None)
    try:
        await service.call("record_autoplay_activation", "c", "e", 0, CHOICES)
        if actor == "planner":
            await service.call("submit", "c", "e", 1, "planner", entry("strategy", "s"))
            await service.call("submit", "c", "e", 1, "planner", entry("task", "t", "s"))
            lease_seq = await service.call("attention_declare", "c", "e", 1, "planner", "focus", "t", "check")
        else:
            utterance = await service.call("record_utterance", "c", "e", 1, "watch closely")
            lease_seq = await service.call(
                "attention_declare",
                "c",
                "e",
                1,
                "user",
                "focus",
                utterance,
                "User requested attention",
                user_sources=(utterance,),
            )
        before = await service.call("events", "c")
        dashboard = await plugin.dashboard_ui_context()
        attention = dashboard["autoplay"]["attention"]
        assert attention["available"] is False and attention["source_status"] == "unavailable"
        assert attention["level"] == "focus"
        assert attention["leases"][0]["event_seq"] == lease_seq
        assert attention["reasons"][0]["event_seq"] == lease_seq
        assert attention["reasons"][0]["signal"]["actor"] == actor
        assert await service.call("events", "c") == before
    finally:
        await service.close()


def test_display_reports_wholly_unavailable_native_snapshot(tmp_path):
    journal = Journal(tmp_path / "events.sqlite")
    journal.record_autoplay_activation("c", "e", 0, CHOICES)
    data = snapshot_data()
    for section in ("alerts", "conditions", "threats"):
        data[section]["status"] = "unavailable"
        data[section]["rows"] = []
    journal.ingest_game_event(
        "c", "e", 250, "conn", native(1, 250, "attention_snapshot", data), attention_available=True
    )
    display = journal.attention_display("c", "e", "conn")
    assert display["level"] == "steady" and display["source_status"] == "unavailable"
    journal.close()
