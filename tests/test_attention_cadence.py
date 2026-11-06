import hashlib
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.workspace import attention_policy
from plugin.plugins.neko_rimworld.workspace.attention_policy import classify, load_policy, normalize_attention_event
from plugin.plugins.neko_rimworld.workspace.cadence import project, validate_declaration


def event(seq, tick, kind, payload=None):
    return {"seq": seq, "game_tick": tick, "load_epoch": "e", "kind": kind, "payload": payload or {}}


def test_policy_hash_and_native_classification():
    policy, digest = load_policy()
    assert digest == hashlib.sha256(Path(attention_policy.__file__).with_name("attention_policy.v1.json").read_bytes()).hexdigest()
    signal = {"source_id": "a", "category": "alert", "edge": "appeared", "priority": "High", "runtime_type": "RimWorld.Alert_StarvationColonists"}
    assert classify(policy, signal)["level"] == "focus"
    assert classify(policy, {**signal, "edge": "disappeared"})["level"] == "record"
    assert classify(policy, {**signal, "priority": "Critical"})["level"] == "interrupt"


def test_daily_due_ids_merge_and_are_explicitly_consumed():
    policy, _ = load_policy()
    events = [event(1, 100, "autoplay.activation"), event(2, 200, "planner.started")]
    state = project(events, epoch="e", now_tick=120100, policy=policy)
    assert state["pending_daily"] == ["daily:1:e:1", "daily:1:e:2"]
    assert len([i for i in state["items"] if i["kind"] == "cadence.review_due"]) == 2
    events += [event(3, 120101, "planner.finished"), event(4, 120102, "planner.started", {"work_ids": ["daily:1:e:1", "daily:1:e:2"]})]
    assert project(events, epoch="e", now_tick=120102, policy=policy)["pending_daily"] == []


def test_fact_lifetime_and_new_generation():
    policy, _ = load_policy()
    signal = {"source_id": "raid", "category": "threat", "edge": "appeared"}
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", signal)]
    assert project(events, epoch="e", now_tick=1, policy=policy)["level"] == "urgent"
    assert project(events, epoch="e", now_tick=15001, policy=policy)["level"] == "focus"
    assert project(events, epoch="e", now_tick=75001, policy=policy)["level"] == "steady"
    events.append(event(3, 75002, "attention.signal", {**signal, "edge": "added"}))
    assert project(events, epoch="e", now_tick=75002, policy=policy)["level"] == "urgent"


def test_declaration_quota_and_cooldown():
    policy, _ = load_policy()
    events = [event(1, 0, "autoplay.activation")]
    lease = validate_declaration(events, epoch="e", tick=1, actor="planner", level="focus", reference="task", reason="watch", policy=policy, open_tasks={"task"})
    assert lease["end_tick"] == 15001
    events.append(event(2, 1, "cadence.lease_declared", lease))
    with pytest.raises(ValueError, match="cooling"):
        validate_declaration(events, epoch="e", tick=15001, actor="planner", level="focus", reference="task", reason="watch", policy=policy, open_tasks={"task"})
    second = validate_declaration(events, epoch="e", tick=17501, actor="planner", level="urgent", reference="task", reason="watch", policy=policy, open_tasks={"task"})
    events.append(event(3, 17501, "cadence.lease_declared", second))
    with pytest.raises(ValueError, match="quota"):
        validate_declaration(events, epoch="e", tick=25001, actor="planner", level="focus", reference="task", reason="watch", policy=policy, open_tasks={"task"})


def test_derived_events_use_append_tick_and_replay_idempotently():
    policy, _ = load_policy()
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", {"source_id": "raid", "category": "threat", "edge": "appeared"})]
    state = project(events, epoch="e", now_tick=16000, policy=policy)
    assert all(item["game_tick"] == 16000 for item in state["items"])
    assert any(item["payload"].get("effective_tick") == 15001 for item in state["items"])
    for seq, item in enumerate(state["items"], 3):
        events.append(event(seq, item["game_tick"], item["kind"], item["payload"]))
    replay = project(events, epoch="e", now_tick=16000, policy=policy)
    assert replay["level"] == state["level"]
    assert replay["items"] == []


def test_disappearance_cools_one_level_at_a_time():
    policy, _ = load_policy()
    signal = {"source_id": "raid", "category": "threat", "edge": "appeared"}
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", signal), event(3, 100, "attention.signal", {**signal, "edge": "disappeared"})]
    assert project(events, epoch="e", now_tick=2599, policy=policy)["level"] == "urgent"
    assert project(events, epoch="e", now_tick=2600, policy=policy)["level"] == "focus"
    assert project(events, epoch="e", now_tick=5100, policy=policy)["level"] == "steady"


def test_focus_window_and_interrupt_are_separate():
    policy, _ = load_policy()
    high = {"source_id": "h", "category": "alert", "edge": "appeared", "priority": "High", "runtime_type": "RimWorld.Alert_StarvationColonists"}
    critical = {"source_id": "c", "category": "alert", "edge": "appeared", "priority": "Critical"}
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", high), event(3, 2, "attention.signal", critical)]
    due = [x["payload"] for x in project(events, epoch="e", now_tick=2501, policy=policy)["items"] if x["kind"] == "cadence.review_due"]
    assert any(x["id"] == "interrupt:3" for x in due)
    assert any(x["id"] == "focus:2" for x in due)


def test_running_prevents_escalated_review_and_finish_restarts_clock():
    policy, _ = load_policy()
    signal = {"source_id": "raid", "category": "threat", "edge": "appeared"}
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", signal), event(3, 10, "planner.started"), event(4, 5000, "planner.finished"), event(5, 6000, "planner.started")]
    state = project(events, epoch="e", now_tick=10000, policy=policy)
    assert not any(x["payload"]["id"].startswith("escalated:") for x in state["items"])
    events.append(event(6, 10001, "planner.finished"))
    state = project(events, epoch="e", now_tick=12501, policy=policy)
    assert any(x["payload"]["id"] == "escalated:10001" for x in state["items"])


def test_native_fixture_edges_are_classified():
    policy, _ = load_policy()
    critical = {"runtime_type": "RimWorld.Alert_ColonistNeedsRescuing", "priority": "Critical"}
    high = {**critical, "priority": "High"}
    base = {"category": "alerts", "source_id": "a", "map_id": "Map_1"}
    upgrade = {**base, "edge": "priority_changed", "previous": high, "current": critical}
    added = {**base, "edge": "targets_changed", "previous": critical, "current": critical,
             "added": [{"id": "Thing_1"}], "removed": []}
    disappeared = {**base, "edge": "disappeared", "previous": critical, "current": None}
    assert [classify(policy, x)["level"] for x in (upgrade, added, disappeared)] == ["interrupt", "interrupt", "record"]


def test_lease_early_expiry_and_cross_epoch():
    policy, _ = load_policy()
    events = [event(1, 0, "autoplay.activation")]
    lease = validate_declaration(events, epoch="e", tick=1, actor="planner", level="urgent", reference="task", reason="watch", policy=policy, open_tasks={"task"})
    events.extend([event(2, 1, "cadence.lease_declared", lease), event(3, 100, "task.closed", {"reference": "task"})])
    state = project(events, epoch="e", now_tick=100, policy=policy)
    expired = [item for item in state["items"] if item["kind"] == "cadence.lease_expired"]
    assert expired[0]["payload"]["expired_tick"] == 100
    assert state["level"] == "steady"
    with pytest.raises(ValueError, match="cooling"):
        validate_declaration(events, epoch="e", tick=2599, actor="planner", level="focus", reference="another", reason="watch", policy=policy, open_tasks={"another"})
    validate_declaration(events, epoch="e", tick=2600, actor="planner", level="focus", reference="another", reason="watch", policy=policy, open_tasks={"another"})
    events.append({**event(4, 200, "world.loaded"), "load_epoch": "new"})
    assert project(events, epoch="new", now_tick=200, policy=policy)["leases"] == {}
    validate_declaration(events, epoch="new", tick=200, actor="planner", level="focus", reference="another", reason="watch", policy=policy, open_tasks={"another"})


def test_signal_source_quiet_period_does_not_extend_lifetime():
    policy, _ = load_policy()
    signal = {"source_id": "raid", "category": "threat", "edge": "appeared"}
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", signal),
              event(3, 100, "attention.signal", {**signal, "edge": "disappeared"}),
              event(4, 200, "attention.signal", signal)]
    state = project(events, epoch="e", now_tick=15001, policy=policy)
    assert state["level"] == "focus"
    assert not any(x["payload"].get("id") == "interrupt:4" for x in state["items"])


def test_user_request_requires_received_original_and_is_bounded():
    policy, _ = load_policy()
    events = [event(1, 0, "autoplay.activation")]
    with pytest.raises(ValueError, match="utterance"):
        validate_declaration(events, epoch="e", tick=1, actor="user", level="focus", reference=7, reason="watch", policy=policy)
    with pytest.raises(ValueError, match="limited"):
        validate_declaration(events, epoch="e", tick=1, actor="user", level="urgent", reference=7, reason="watch", policy=policy, user_utterances={7})
    lease = validate_declaration(events, epoch="e", tick=1, actor="user", level="focus", reference=7, reason="watch", policy=policy, user_utterances={7})
    assert lease["end_tick"] == 60001


def test_focus_daily_limit_counts_started_runs_and_retains_excess():
    policy, _ = load_policy()
    events = [event(1, 0, "autoplay.activation")]
    high = {"category": "alert", "edge": "appeared", "priority": "High", "runtime_type": "RimWorld.Alert_StarvationColonists"}
    for index in range(5):
        signal_seq = len(events) + 1
        t = index * 5000 + 1
        events.append(event(signal_seq, t, "attention.signal", {**high, "source_id": str(index)}))
        if index < 4:
            events.append(event(len(events) + 1, t + 2500, "planner.started", {"work_ids": [f"focus:{signal_seq}"], "signal_seqs": [signal_seq]}))
            events.append(event(len(events) + 1, t + 2600, "planner.finished"))
    state = project(events, epoch="e", now_tick=22501, policy=policy)
    assert state["pending_focus"] is None
    assert state["deferred_signal_seqs"] == [events[-1]["seq"]]
    assert len([x for x in state["items"] if x["payload"]["id"].startswith("focus:")]) == 4


def test_low_food_focus_received_by_running_planner_is_consumed_once():
    policy, _ = load_policy()
    low_food = {"category": "alert", "edge": "appeared", "priority": "High", "runtime_type": "RimWorld.Alert_LowFood"}
    assert classify(policy, {**low_food, "source_id": "x"})["level"] == "focus"
    events = [event(1, 0, "autoplay.activation"), event(2, 100, "planner.started", {"work_ids": [], "signal_seqs": []}),
              event(3, 1000, "attention.signal", {**low_food, "source_id": "food"})]
    state = project(events, epoch="e", now_tick=3500, policy=policy)
    assert state["pending_focus"] == "focus:3"
    events.append(event(4, 3600, "planner.received", {"work_ids": ["focus:3"], "signal_seqs": [3]}))
    state = project(events, epoch="e", now_tick=3600, policy=policy)
    assert state["pending_focus"] is None
    events.append(event(5, 3700, "planner.finished"))
    assert project(events, epoch="e", now_tick=3700, policy=policy)["pending_focus"] is None
    starvation = {"category": "alert", "edge": "appeared", "priority": "High",
                  "runtime_type": "RimWorld.Alert_StarvationColonists", "source_id": "starve"}
    events.append(event(6, 4000, "attention.signal", starvation))
    assert project(events, epoch="e", now_tick=6500, policy=policy)["pending_focus"] == "focus:6"


def test_disable_and_load_emit_lease_expiry_once():
    policy, _ = load_policy()
    events = [event(1, 0, "autoplay.activation")]
    lease = validate_declaration(events, epoch="e", tick=1, actor="planner", level="focus", reference="task", reason="watch", policy=policy, open_tasks={"task"})
    events.append(event(2, 1, "cadence.lease_declared", lease))
    loaded = events + [{**event(3, 10, "world.loaded"), "load_epoch": "new"}]
    state = project(loaded, epoch="new", now_tick=10, policy=policy)
    assert state["items"][0]["payload"]["reason"] == "epoch_changed"
    disabled = events + [event(3, 10, "autoplay.disabled")]
    state = project(disabled, epoch="e", now_tick=10, policy=policy)
    assert not state["active"]
    assert state["items"][0]["payload"]["expired_tick"] == 10
    item = state["items"][0]
    disabled.append(event(4, 10, item["kind"], item["payload"]))
    assert project(disabled, epoch="e", now_tick=10, policy=policy)["items"] == []


def test_load_keeps_activation_anchor_but_not_old_facts_or_daily_work():
    policy, _ = load_policy()
    signal = {"source_id": "raid", "category": "threat", "edge": "appeared"}
    events = [event(1, 1000, "autoplay.activation"), event(2, 1001, "attention.signal", signal),
              event(3, 61000, "planner.started", {"work_ids": ["daily:1:e:1"]}),
              {**event(4, 30000, "world.loaded"), "load_epoch": "new"}]
    state = project(events, epoch="new", now_tick=61000, policy=policy)
    assert state["level"] == "steady"
    assert state["pending_daily"] == ["daily:1:new:1"]
    events[-1]["game_tick"] = 100
    state = project(events, epoch="new", now_tick=100, policy=policy)
    assert state["next_daily_tick"] == 61000
    assert state["pending_daily"] == []


def test_authoritative_native_snapshot_to_changed_and_received_identity():
    import json

    policy, _ = load_policy()
    fixture = Path(__file__).parent / "fixtures/colony-attention-v1/vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))["valid"]
    snapshot = vectors[0]["payload"]["data"]
    payloads = normalize_attention_event(snapshot)
    assert len(payloads) == 4
    events = [event(1, 0, "autoplay.activation")]
    for payload in payloads:
        events.append(event(len(events) + 1, 250, "attention.signal", payload))
    state = project(events, epoch="e", now_tick=250, policy=policy)
    assert state["level"] == "urgent"
    assert {f["partition"] for f in state["facts"].values()} == {"alerts", "conditions", "threats"}
    assert state["pending_signal_seqs"] == [3, 4, 5]
    # Replaying the same baseline does not renew existing source lifetimes.
    for payload in payloads:
        events.append(event(len(events) + 1, 500, "attention.signal", payload))
    state = project(events, epoch="e", now_tick=500, policy=policy)
    assert all(f["since"] == 250 for f in state["facts"].values())
    threat_disappearance = vectors[6]["payload"]["data"]
    for payload in normalize_attention_event(threat_disappearance):
        events.append(event(len(events) + 1, 600, "attention.signal", payload))
    absent_seq = events[-1]["seq"]
    events.append(event(len(events) + 1, 601, "planner.started", {"work_ids": [], "signal_seqs": [3]}))
    state = project(events, epoch="e", now_tick=601, policy=policy)
    assert 3 not in state["pending_signal_seqs"]
    assert absent_seq in state["pending_signal_seqs"]
    assert any(not f["active"] for f in state["facts"].values())
    events.append(event(len(events) + 1, 602, "planner.received", {"signal_seqs": [absent_seq]}))
    assert absent_seq not in project(events, epoch="e", now_tick=602, policy=policy)["pending_signal_seqs"]


def test_priority_decrease_is_record_but_updates_continuing_fact():
    policy, _ = load_policy()
    source = {"category": "alerts", "source_id": "a", "map_id": "Map_1"}
    critical = {"runtime_type": "RimWorld.Alert_StarvationColonists", "priority": "Critical"}
    high = {**critical, "priority": "High"}
    appeared = normalize_attention_event({**source, "kind": "attention_changed", "edge": "appeared", "current": critical, "previous": None})[0]
    lowered = normalize_attention_event({**source, "kind": "attention_changed", "edge": "priority_changed", "current": high, "previous": critical})[0]
    assert classify(policy, lowered)["level"] == "record"
    events = [event(1, 0, "autoplay.activation"), event(2, 1, "attention.signal", appeared), event(3, 10, "attention.signal", lowered)]
    state = project(events, epoch="e", now_tick=5010, policy=policy)
    assert state["level"] == "focus"
    assert state["facts"][lowered["source_id"]]["active"]
    assert 3 in state["pending_signal_seqs"]


def test_partial_snapshot_never_proves_absence_and_map_switch_replaces_scope():
    policy, _ = load_policy()
    base = {"kind": "attention_snapshot", "map_id": "a", "alerts": {"status": "ok", "rows": [{"id": "x", "priority": "Critical"}]},
            "conditions": {"status": "ok", "rows": []}, "threats": {"status": "ok", "rows": []}}
    events = [event(1, 0, "autoplay.activation")]
    for payload in normalize_attention_event(base):
        events.append(event(len(events) + 1, 1, "attention.signal", payload))
    partial = {**base, "alerts": {"status": "partial", "rows": []}}
    events.append(event(len(events) + 1, 2, "attention.signal", normalize_attention_event(partial)[0]))
    assert project(events, epoch="e", now_tick=5000, policy=policy)["level"] == "urgent"
    new_map = {**base, "map_id": "b", "alerts": {"status": "ok", "rows": []}}
    events.append(event(len(events) + 1, 5001, "attention.signal", normalize_attention_event(new_map)[0]))
    assert project(events, epoch="e", now_tick=5001, policy=policy)["level"] == "steady"


def test_reconnect_baseline_compares_priority_and_targets_without_renewing_unchanged():
    policy, _ = load_policy()
    row = {"id": "a", "runtime_type": "RimWorld.Alert_StarvationColonists", "priority": "High",
           "targets_complete": True, "targets": [{"id": "one", "kind": "thing", "map_id": "m"}]}
    snapshot = {"kind": "attention_snapshot", "map_id": "m", "alerts": {"status": "ok", "rows": [row]},
                "conditions": {"status": "ok", "rows": []}, "threats": {"status": "ok", "rows": []}}
    events = [event(1, 0, "autoplay.activation")]
    def add_snapshot(tick, snap):
        for payload in normalize_attention_event(snap):
            events.append(event(len(events) + 1, tick, "attention.signal", payload))
    add_snapshot(1, snapshot)
    add_snapshot(100, snapshot)
    state = project(events, epoch="e", now_tick=100, policy=policy)
    fact = next(iter(state["facts"].values()))
    assert fact["since"] == 1
    assert not any(item["payload"].get("id") == "interrupt:5" for item in state["items"])
    raised = {**row, "priority": "Critical"}
    add_snapshot(200, {**snapshot, "alerts": {"status": "ok", "rows": [raised]}})
    state = project(events, epoch="e", now_tick=200, policy=policy)
    assert state["level"] == "urgent"
    assert any(item["payload"].get("id") == f"interrupt:{events[-1]['seq']}" for item in state["items"])
    expanded = {**raised, "targets": [*row["targets"], {"id": "two", "kind": "thing", "map_id": "m"}]}
    add_snapshot(300, {**snapshot, "alerts": {"status": "ok", "rows": [expanded]}})
    state = project(events, epoch="e", now_tick=300, policy=policy)
    assert any(item["payload"].get("id") == f"interrupt:{events[-1]['seq']}" for item in state["items"])
    assert next(iter(state["facts"].values()))["since"] == 300
    add_snapshot(400, {**snapshot, "alerts": {"status": "ok", "rows": [expanded]}})
    assert next(iter(project(events, epoch="e", now_tick=400, policy=policy)["facts"].values()))["since"] == 300
    lowered = {**expanded, "priority": "High"}
    add_snapshot(500, {**snapshot, "alerts": {"status": "ok", "rows": [lowered]}})
    state = project(events, epoch="e", now_tick=500, policy=policy)
    assert state["level"] == "focus"
    assert not any(item["payload"].get("id") == f"interrupt:{events[-1]['seq']}" for item in state["items"])


def test_target_identity_and_partial_snapshot_recovery():
    policy, _ = load_policy()
    thing = {"id": "Thing_1", "kind": "thing", "map_id": "m", "x": None, "z": None, "tile": None}
    cell_a = {"id": None, "kind": "cell", "map_id": "m", "x": 1, "z": 2, "tile": 10}
    cell_b = {"id": None, "kind": "cell", "map_id": "m", "x": 3, "z": 4, "tile": 10}
    row = {"id": "a", "runtime_type": "RimWorld.Alert_ColonistNeedsRescuing", "priority": "Critical",
           "targets_complete": True, "targets": [thing, cell_a, cell_b]}
    snapshot = {"kind": "attention_snapshot", "map_id": "m", "alerts": {"status": "ok", "rows": [row]},
                "conditions": {"status": "ok", "rows": []}, "threats": {"status": "ok", "rows": []}}
    events = [event(1, 0, "autoplay.activation")]
    def add(tick, native):
        for payload in normalize_attention_event(native):
            events.append(event(len(events) + 1, tick, "attention.signal", payload))
    add(1, snapshot)
    partial_row = {**row, "targets_complete": False, "targets": [thing, cell_a]}
    add(100, {**snapshot, "alerts": {"status": "partial", "rows": [partial_row]}})
    state = project(events, epoch="e", now_tick=100, policy=policy)
    assert len(next(iter(state["facts"].values()))["observed"]["targets"]) == 3
    add(200, snapshot)
    state = project(events, epoch="e", now_tick=200, policy=policy)
    assert next(iter(state["facts"].values()))["since"] == 1
    assert not any(item["payload"]["id"] == f"interrupt:{events[-1]['seq']}" for item in state["items"])
    source = {"kind": "attention_changed", "category": "alerts", "source_id": "a", "map_id": "m", "edge": "targets_changed"}
    without_a = {**row, "targets": [thing, cell_b]}
    add(300, {**source, "previous": row, "current": without_a, "added": [], "removed": [cell_a]})
    cell_c = {**cell_a, "x": 8}
    with_c = {**without_a, "targets": [thing, cell_b, cell_c]}
    add(400, {**source, "previous": without_a, "current": with_c, "added": [cell_c], "removed": []})
    state = project(events, epoch="e", now_tick=400, policy=policy)
    assert any(item["payload"]["id"] == f"interrupt:{events[-1]['seq']}" for item in state["items"])
    with_a = {**with_c, "targets": [thing, cell_a, cell_b, cell_c]}
    add(500, {**source, "previous": with_c, "current": with_a, "added": [cell_a], "removed": []})
    state = project(events, epoch="e", now_tick=500, policy=policy)
    assert not any(item["payload"]["id"] == f"interrupt:{events[-1]['seq']}" for item in state["items"])
