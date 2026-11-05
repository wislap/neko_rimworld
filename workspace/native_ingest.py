"""Native game events, receipts and snapshots translated into ledger facts and the wakes they cause.

Writers run inside the transaction the journal opened.
"""

import copy
from dataclasses import dataclass

from ..serialization import canonical
from . import construction, work_state
from .area_evidence import facts as area_facts
from .area_takeovers import plan_stops, validate_change
from .attention_cadence import append_cadence
from .attention_policy import normalize_attention_event
from .construction_evidence import facts
from .field_takeovers import record_takeover, takeovers
from .ledger import LOAD_EVENT, OBSERVED_EVENT, PLUGIN
from .observations import prepare_fact as prepare_observation_fact
from .resource_allocation import prepare_observation
from .resource_allocation import project as resource_project
from .resource_evidence import observed_snapshot
from .role_policies import wake_policy


@dataclass(frozen=True)
class NativeEventIngestion:
    """Committed native cursor and role work produced by that same transaction."""

    changed: bool = False
    event_seq: int | None = None
    planner_wake: bool = False
    frontdesk_wake: bool = False
    authorization_changed: bool = False

    def __bool__(self):
        return self.changed


def record_load(ledger, colony_id, load_epoch, game_tick):
    return ledger.append(colony_id, load_epoch, game_tick, PLUGIN, LOAD_EVENT, {})


def observe_epoch(ledger, colony_id, load_epoch, game_tick):
    """Record a load once when the observed epoch changes; never rewrite history."""
    previous = ledger.current_epoch(colony_id)
    if previous is None and not ledger.event_highwater(colony_id):
        # Establish a read-only colony's first confirmed identity without
        # warning that its empty history changed. Retained legacy history
        # without a confirmed identity requires the ordinary load review.
        ledger.append(colony_id, load_epoch, game_tick, PLUGIN, OBSERVED_EVENT, {})
    elif previous != load_epoch:
        return record_load(ledger, colony_id, load_epoch, game_tick)
    return None


def ingest_game_event(ledger, colony_id, load_epoch, game_tick, connection, event, *, attention_available=False,
                      observed_tick=None):
    """Persist a native event and its default wakes; the raw row is the cursor."""
    cursor = event.get("cursor")
    if (type(cursor) is not int or cursor < 1 or not isinstance(connection, str) or not connection
            or (event.get("connection_epoch"), event.get("colony_id"), event.get("load_epoch"),
                event.get("game_tick")) != (connection, colony_id, load_epoch, game_tick)):
        raise ValueError("Invalid native event cursor or sampled identity")
    if observed_tick is not None and (type(observed_tick) is not int or observed_tick < 0):
        raise ValueError("Invalid observed game tick")
    last = ledger.game_event_cursor(colony_id, load_epoch, connection)
    if cursor <= last:
        previous = ledger.game_event_at_cursor(colony_id, load_epoch, connection, cursor)
        if previous and previous["payload"]["event"] == event:
            return NativeEventIngestion()
        raise ValueError("Native event cursor was reused")
    sampled_tick = game_tick
    previous_tick = ledger.latest_observed_tick(colony_id, load_epoch)
    game_tick = max(sampled_tick, observed_tick if observed_tick is not None else sampled_tick,
                    previous_tick if previous_tick is not None else sampled_tick)
    latest_epoch = ledger.current_epoch(colony_id)
    previous_native = ledger.latest_event(colony_id, "game.event")
    previous_epoch = previous_native["load_epoch"] if previous_native else latest_epoch
    epoch_changed = previous_epoch is not None and previous_epoch != load_epoch
    if epoch_changed and latest_epoch != load_epoch:
        start_tick = event.get("data", {}).get("start_tick")
        loaded_at = (start_tick if event.get("kind") == "session_snapshot"
                     and type(start_tick) is int and 0 <= start_tick <= game_tick else game_tick)
        record_load(ledger, colony_id, load_epoch, loaded_at)
    seq = ledger.append(colony_id, load_epoch, game_tick, "mod", "game.event",
                        {"connection": connection, "event": event, "native_game_tick": sampled_tick,
                         "observed_game_tick": game_tick})
    kind = event.get("kind")
    data = event.get("data", {})
    if kind in {"attention_snapshot", "attention_changed"} and attention_available:
        for ordinal, signal in enumerate(normalize_attention_event({"kind": kind, **data})):
            ledger.append(colony_id, load_epoch, game_tick, "mod", "attention.signal",
                          {**signal, "native_event_seq": seq, "native_game_tick": sampled_tick, "ordinal": ordinal})
    elif kind == "letter":
        ledger.append(colony_id, load_epoch, game_tick, "mod", "attention.signal",
                      {"category": "letter", "edge": "appeared", "source_id": f"letter:{seq}",
                       "def": data.get("def"), "native_event_seq": seq, "native_game_tick": sampled_tick, "ordinal": 0})
    cadence = append_cadence(ledger, colony_id, load_epoch, game_tick)
    planner_wake = any(item["kind"] == "cadence.review_due" for item in cadence["items"])
    if kind == "observation":
        planner_wake = _ingest_observation(ledger, colony_id, load_epoch, game_tick, connection, data, seq) or planner_wake
    authorization_changed = False
    if kind == "area_manual_changed":
        try:
            if data.get("source") != "user_designator" or not isinstance(data.get("changes"), list):
                raise ValueError("Manual area events require a native user-designator source")
            manual = _ingest_area_manual_changes(ledger, colony_id, load_epoch, game_tick, data["changes"], seq)
            authorization_changed = manual["changed"]
            planner_wake = manual["planner_wake"] or planner_wake
        except ValueError as error:
            ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "area.manual_ignored",
                          {"native_event_seq": seq, "reason": str(error)})
    frontdesk_wake = False
    kind = event.get("kind")
    data = event.get("data", {})
    policy = wake_policy(ledger, colony_id)
    lifecycle = ledger.events_by_kind(colony_id, ("autoplay.activation", "autoplay.disabled"))
    autoplay = bool(lifecycle and lifecycle[-1]["kind"] == "autoplay.activation")
    if (epoch_changed and not autoplay) or (kind == "pause_changed" and policy["frontdesk_paused_windows"]
                         and data.get("source") == "window" and data.get("windows_force_paused") is True) or (kind == "letter" and
            ((not autoplay and data.get("def") in policy["frontdesk_letter_defs"])
             or (policy["frontdesk_choice_letters"] and data.get("is_choice_letter") is True))):
        presented_event = copy.deepcopy(event)
        presented_data = presented_event.get("data")
        if isinstance(presented_data, dict):
            presented_data.pop("monotonic", None)
            pause_state = presented_data.get("pause_state")
            if isinstance(pause_state, dict):
                pause_state.pop("monotonic", None)
        text = canonical({"event_seq": seq, "event": presented_event})
        anchor = ledger.append(colony_id, load_epoch, game_tick, "mod", "frontdesk.trigger",
                               {"source": "game", "text": text, "event_seq": seq})
        ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "frontdesk.wake",
                      {"source": "game", "trigger_seq": anchor})
        frontdesk_wake = True
    if epoch_changed:
        ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                      {"source": "game", "text": "World loaded", "event_seq": seq})
        planner_wake = True
    return NativeEventIngestion(True, seq, planner_wake, frontdesk_wake, authorization_changed)


def ingest_area_manual_snapshot(ledger, colony_id, load_epoch, game_tick, connection, observation):
    """Consume save-backed exact manual provenance before the write guard runs."""
    response = observation.get("response", {})
    payload = response.get("payload", {})
    data = payload.get("data", {})
    if (observation.get("request", {}).get("operation") != "write_ledger_snapshot"
            or tuple(response.get(key) for key in ("connection_epoch", "colony_id", "load_epoch")) != (connection, colony_id, load_epoch)
            or payload.get("status") != "ok" or payload.get("issues") or data.get("ledger_status") != "valid"
            or data.get("complete") is not True or not isinstance(data.get("entries"), list)
            or data.get("entry_count") != len(data["entries"])
            or ("manual_sources" in data and not isinstance(data["manual_sources"], list))
            or ("manual_sources_complete" in data and type(data["manual_sources_complete"]) is not bool)):
        raise ValueError("Manual area sources require the original complete native ledger snapshot")
    changes = []
    for row in data["entries"]:
        if row.get("manual_source") is None:
            continue
        change = validate_change(row["manual_source"], game_tick=game_tick)
        if (change["object"], change["field"]) != (row.get("object"), row.get("field")):
            raise ValueError("Native manual source does not match its ledger field")
        changes.append(change)
    for raw_source in data.get("manual_sources", []):
        changes.append(validate_change(raw_source, game_tick=game_tick))
    seen = {(event["payload"]["manual_id"], event["payload"]["object"], event["payload"]["field"])
            for event in ledger.events_by_kind(colony_id, ("area.manual",))}
    if not any((change["manual_id"], change["object"], change["field"]) not in seen for change in changes):
        return {"changed": False, "planner_wake": False}
    tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    source = ledger.append(colony_id, load_epoch, tick, "mod", "area.manual_snapshot", {"observation": observation})
    return _ingest_area_manual_changes(ledger, colony_id, load_epoch, tick, changes, source)


def _ingest_area_manual_changes(ledger, colony_id, load_epoch, game_tick, changes, source_seq):
    changes = [validate_change(change, game_tick=game_tick) for change in changes]
    manual_events = ledger.events_by_kind(colony_id, ("area.manual",))
    seen = {(event["payload"]["manual_id"], event["payload"]["object"], event["payload"]["field"]) for event in manual_events}
    watermarks = {}
    for event in manual_events:
        data = event["payload"]
        scope = (data["origin_load_epoch"], data["object"], data["field"], data["new_value"]["native_instance"])
        watermarks[scope] = max(watermarks.get(scope, 0), data["manual_order"])
    changed, wake_tasks = False, set()
    for change in changes:
        key = (change["manual_id"], change["object"], change["field"])
        if key in seen:
            continue
        scope = (change["origin_load_epoch"], change["object"], change["field"], change["new_value"]["native_instance"])
        if change["manual_order"] <= watermarks.get(scope, 0):
            ledger.append(colony_id, load_epoch, game_tick, "mod", "area.manual", {
                **change, "takeover_seq": None, "source_event_seq": source_seq, "disposition": "superseded"})
            seen.add(key)
            continue
        opened = takeovers(ledger, colony_id)
        takeover = next((event for event in opened if (event["payload"]["object"], event["payload"]["field"])
                         == (change["object"], change["field"])), None)
        takeover_seq = takeover["seq"] if takeover else record_takeover(ledger, colony_id, load_epoch, game_tick, {
            "object": change["object"], "field": change["field"], "value_read": change["new_value"],
            "ai_value": change["ownership"]["value"] if change["ownership"] is not None else None,
            "changeset_id": change["ownership"]["changeset_id"] if change["ownership"] is not None else None})
        event_payload = {**change, "takeover_seq": takeover_seq, "source_event_seq": source_seq, "disposition": "applied"}
        seq = ledger.append(colony_id, load_epoch, game_tick, "mod", "area.manual", event_payload)
        manual_events.append({"seq": seq, "kind": "area.manual", "payload": event_payload})
        events = ledger.events_by_kind(colony_id, work_state.KINDS)
        state = construction.project(events, load_epoch=load_epoch)
        for arguments in plan_stops(state, change=change, manual_events=manual_events, open_takeovers=takeovers(ledger, colony_id)):
            spec = construction.prepare_fact(events, **arguments, observation_id="native-manual:" + str(seq), load_epoch=load_epoch)
            ledger.append(colony_id, load_epoch, game_tick, spec["source"], spec["kind"], spec["payload"])
            project = next(row for row in state["plans"] if row["plan_id"] == arguments["plan_id"])
            wake_tasks.add(project["task_id"])
        changed = True
        seen.add(key)
        watermarks[scope] = change["manual_order"]
    planner_wake = False
    starts = ledger.events_by_kind(colony_id, ("planner.started",), load_epoch=load_epoch)
    last_start = starts[-1]["seq"] if starts else 0
    pending = ledger.events_by_kind(colony_id, ("planner.wake",), after_seq=last_start, load_epoch=load_epoch)
    planner_wake = any(event["payload"].get("review_type") == "manual_area" for event in pending)
    for task_id in wake_tasks:
        task = ledger.revision(task_id)
        work_id = "manual-area:" + task_id
        if task is None or task["status"] == "closed" or any(event["payload"].get("work_id") == work_id for event in pending):
            continue
        ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake", {
            "source": "game", "text": "The player changed a planned area cell; preserve the exact taken-over fields.",
            "task_id": task_id, "work_id": work_id, "event_seq": source_seq, "review_type": "manual_area"})
        planner_wake = True
    return {"changed": changed, "planner_wake": planner_wake}


def _ingest_observation(ledger, colony_id, load_epoch, game_tick, connection, data, native_seq):
    """Only native ACK-bound samples become business facts; coalesce pending work."""
    binding = {"colony_id": colony_id, "load_epoch": load_epoch, "connection_epoch": connection}
    events = ledger.events_by_kind(colony_id, work_state.KINDS)
    fact_data = {key: data[key] for key in ("observation_id", "revision", "condition_id", "receipt_id", "status",
                                           "sample_tick", "observation", "coverage") if key in data}
    try:
        spec = prepare_observation_fact(events, binding=binding, **fact_data)
    except (ValueError, KeyError, TypeError) as error:
        ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "observation.ignored",
                      {"native_event_seq": native_seq, "reason": str(error)})
        return False
    ledger.append(colony_id, load_epoch, game_tick, spec["source"], spec["kind"],
                  {**spec["payload"], "native_event_seq": native_seq})
    native_observation = data.get("observation", {})
    if data["status"] == "changed" and native_observation.get("format") == "construction_intents.v1":
        reconcile_construction(ledger, colony_id, load_epoch, game_tick, connection, {
            "observation_id": "native-event:" + str(native_seq), "request": {"operation": "inspect_construction_intents"},
            "response": {"connection_epoch": connection, "colony_id": colony_id, "load_epoch": load_epoch,
                         "game_tick": data["sample_tick"], "payload": {"status": "ok", "data": native_observation}}})
    request = next(e for e in reversed(events) if e["kind"] == "observation.requested"
                   and e["payload"]["observation_id"] == data["observation_id"])
    task_id = request["payload"]["task_id"]
    condition = next(row for row in request["payload"]["conditions"] if row["id"] == data["condition_id"])
    if data["status"] in {"changed", "unknown"} and condition["kind"] == "resources":
        for decision in resource_project(events)["decisions"]:
            if decision["task_id"] != task_id:
                continue
            resource_data = native_observation if data["status"] == "changed" else {"map_id": condition.get("map_id"), "objects": []}
            snapshot = observed_snapshot(resource_data, decision, observation_id="native-event:" + str(native_seq),
                load_epoch=load_epoch, game_tick=data["sample_tick"], target_ids=condition["target_ids"])
            if snapshot is not None:
                observed = prepare_observation(events, allocation_id=decision["allocation_id"],
                    expected_revision=decision["revision"], snapshot=snapshot)
                ledger.append(colony_id, load_epoch, game_tick, observed["source"], observed["kind"], observed["payload"])
    task = ledger.revision(task_id)
    if task is None or task["status"] == "closed":
        return False
    starts = ledger.events_by_kind(colony_id, ("planner.started",), load_epoch=load_epoch)
    last_start = starts[-1]["seq"] if starts else 0
    key = "observation:" + task_id
    pending = ledger.events_by_kind(colony_id, ("planner.wake",), after_seq=last_start, load_epoch=load_epoch)
    if any(e["payload"].get("work_id") == key for e in pending):
        return False
    ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "planner.wake",
                  {"source": "game", "text": "Registered observation changed; read colony_work for merged current facts.",
                   "work_id": key, "task_id": task_id, "event_seq": native_seq,
                   "review_type": "observation"})
    return True


def reconcile_construction(ledger, colony_id, load_epoch, game_tick, connection_epoch, observation):
    """Project native registered intent identity; never adopt a same-position object."""
    response = observation.get("response", {})
    if tuple(response.get(k) for k in ("connection_epoch", "colony_id", "load_epoch")) != (connection_epoch, colony_id, load_epoch):
        raise ValueError("Construction facts must belong to the active world")
    operation = observation.get("request", {}).get("operation")
    if operation not in {"inspect_construction_intents", "inspect_construction", "inspect_areas"}:
        return []
    payload = response.get("payload", {})
    if payload.get("status") not in {"ok", "partial"}:
        return []
    if operation == "inspect_construction_intents" and (payload.get("status") != "ok" or payload.get("issues")):
        return []
    tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    events = ledger.events_by_kind(colony_id, work_state.KINDS)
    state = construction.project(events, load_epoch=load_epoch)
    if operation == "inspect_construction_intents":
        prepared = facts(state, payload.get("data", {}), observation_id=observation["observation_id"], load_epoch=load_epoch)
    else:
        prepared = area_facts(state, payload.get("data", {}), operation=operation,
                              observation_id=observation["observation_id"], load_epoch=load_epoch)
    seqs = []
    for arguments in prepared:
        spec = construction.prepare_fact(events, **arguments)
        seq = ledger.append(colony_id, load_epoch, tick, spec["source"], spec["kind"], spec["payload"])
        events.append({"seq": seq, "load_epoch": load_epoch, **spec})
        seqs.append(seq)
    return seqs


def is_native_construction_commit(source, kind, payload):
    """A planner result row that committed construction operations through the native writer."""
    write = payload.get("write", {}) if isinstance(payload, dict) else {}
    return (kind == "planner.progress" and source == "planner" and write.get("stage") == "result"
            and write.get("mode") == "commit" and write.get("origin") == "native"
            and any(op.get("op") in {"place_blueprint", "cancel_blueprint", "restart_blueprint"}
                    for op in write.get("changeset", {}).get("ops", [])))


def record_construction_batch(ledger, colony_id, load_epoch, game_tick, write, source_seq):
    envelope = write.get("envelope") or {}
    receipt = write.get("receipt") or {}
    confirmed = (write.get("transport_state") == "confirmed_receipt" and write.get("outcome") == "known"
                 and envelope.get("colony_id") == colony_id and envelope.get("load_epoch") == load_epoch
                 and envelope.get("payload", {}).get("status") == "ok")
    status = ("committed" if confirmed and receipt.get("status") == "committed" else
              "rejected" if confirmed or write.get("outcome") in {"known_failure", "not_sent"} else "unknown")
    if status == "committed":
        _record_construction_receipts(ledger, colony_id, load_epoch, game_tick, write, source_seq)
    plans = work_state.current(ledger, colony_id, load_epoch)["construction"]["plans"]
    for plan in plans:
        elements = [op["value"]["element_id"] for op in write["changeset"]["ops"]
                    if op.get("op") in {"place_blueprint", "restart_blueprint"} and op.get("value", {}).get("plan_id") == plan["plan_id"]]
        if elements:
            ledger.append(colony_id, load_epoch, game_tick, PLUGIN, "construction.batch", {
                "plan_id": plan["plan_id"], "revision": plan["revision"], "element_ids": elements,
                "changeset_id": write.get("changeset_id"), "status": status, "source_seq": source_seq})


def _record_construction_receipts(ledger, colony_id, load_epoch, game_tick, write, source_seq):
    events = ledger.events_by_kind(colony_id, work_state.KINDS)
    for op in write["changeset"]["ops"]:
        operation = op.get("op")
        if operation not in {"place_blueprint", "cancel_blueprint", "restart_blueprint"}:
            continue
        key = op if operation == "cancel_blueprint" else op.get("value", {})
        rows = [row for row in write["receipt"].get("readback", []) if isinstance(row.get("value"), dict)
                and row["value"].get("plan_id") == key.get("plan_id")
                and row["value"].get("element_id") == key.get("element_id")
                and row["value"].get("field_identity") == {"object": row.get("object"), "field": row.get("field")}]
        if len(rows) != 1 or op.get("map") != rows[0]["value"].get("map_id"):
            continue
        native = rows[0]["value"]
        evidence_id = "native-commit:" + str(source_seq)
        state = construction.project(events, load_epoch=load_epoch)
        if operation == "restart_blueprint":
            plan = next((row for row in state["plans"] if row["plan_id"] == key.get("plan_id")), None)
            current = next((row for row in plan["elements"] if row["id"] == key.get("element_id")), None) if plan else None
            previous = current.get("binding", {}).get("object_id") if current else None
            original = op.get("expect", {}).get("intent", {})
            target = native.get("target") or {}
            if (previous is None or previous not in original.get("lineage", []) or native.get("stage") != "blueprint"
                    or native.get("source_id") != target.get("id") or native.get("element") != key):
                continue
            spec = construction.prepare_restart(events, plan_id=key["plan_id"], element_id=key["element_id"],
                load_epoch=load_epoch, previous_object_id=previous, object_id=target["id"], observation_id=evidence_id,
                field_identity=native["field_identity"])
            prepared = [spec]
        else:
            data = {"format": "construction_intents.v1", "map_id": op["map"], "objects": [native]}
            prepared = [construction.prepare_fact(events, **arguments) for arguments in facts(state, data,
                observation_id=evidence_id, load_epoch=load_epoch)]
        for spec in prepared:
            seq = ledger.append(colony_id, load_epoch, game_tick, spec["source"], spec["kind"], spec["payload"])
            events.append({"seq": seq, "load_epoch": load_epoch, **spec})
