"""Durable desired observation and current native coverage are distinct facts."""

from copy import deepcopy

from ..game.observation_contract import (
    CONDITION_OPTIONAL_FIELDS,
    CONDITION_REQUIRED_FIELDS,
    MAX_CONDITIONS,
    MAX_NATIVE_TICK,
    MAX_OBSERVATIONS,
    MAX_SAMPLE_INTERVAL_TICKS,
    MAX_SAMPLES,
    MAX_TARGETS,
)
from .ledger import is_integer

# Planner policy on top of the native limits: an observation expires within this horizon.
MAX_EXPIRY_HORIZON_TICKS = 600000

FORMAT = "observation_intent.v1"
KINDS = frozenset({"observation.requested", "observation.revoked", "observation.coverage", "observation.fact"})


def _identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _binding(value):
    if not isinstance(value, dict):
        raise ValueError("Observation binding is required")
    for name in ("colony_id", "load_epoch", "connection_epoch"):
        _identity(value.get(name), name)
    return {name: value[name] for name in ("colony_id", "load_epoch", "connection_epoch")}


def _event(kind, actor, **payload):
    return {"kind": kind, "source": actor, "payload": {"format": FORMAT, **deepcopy(payload)}}


def project(events, *, binding, enabled=True):
    binding = _binding(binding)
    intents = {}
    activation_seq = None
    for event in events:
        payload = event.get("payload", {})
        if event.get("kind") == "autoplay.activation":
            activation_seq = event.get("seq")
            continue
        if event.get("kind") not in KINDS or payload.get("format") != FORMAT:
            continue
        identity = payload["observation_id"]
        kind = event["kind"]
        if kind == "observation.requested":
            old = intents.get(identity, {})
            intents[identity] = {**deepcopy(payload), "active": True, "_ack": None, "_facts": [],
                                 "history": [*old.get("history", []), {"kind": kind, "seq": event.get("seq"), "revision": payload["revision"]}]}
        elif identity in intents:
            current = intents[identity]
            current["history"].append({"kind": kind, "seq": event.get("seq"), "revision": payload["revision"]})
            if payload["revision"] != current["revision"]:
                continue
            if kind == "observation.revoked":
                current["active"] = False
                current["reason"] = payload["reason"]
            elif kind == "observation.coverage":
                previous_ack = current["_ack"]
                current["_ack"] = {**deepcopy(payload), "event_seq": event.get("seq")}
                if not previous_ack or previous_ack["binding"] != payload["binding"] or previous_ack["receipt_id"] != payload["receipt_id"]:
                    current["_facts"] = []
            elif kind == "observation.fact":
                current["_facts"].append(deepcopy(payload))
    coverage, output = [], []
    for current in intents.values():
        ack = current.pop("_ack")
        facts = current.pop("_facts")
        matching = bool(ack and ack["binding"] == binding and (activation_seq is None or
                        is_integer(ack.get("event_seq")) and ack["event_seq"] > activation_seq))
        covered = set(ack["covered_condition_ids"]) if matching else set()
        condition_states = {identity: "covered" for identity in covered}
        for fact in facts:
            if matching and fact["binding"] == binding and fact["receipt_id"] == ack["receipt_id"]:
                identity = fact["condition_id"]
                if condition_states.get(identity) in {"expired", "exhausted"}:
                    continue
                if fact["status"] in {"expired", "exhausted", "unknown"}:
                    covered.discard(identity)
                    condition_states[identity] = fact["status"]
                elif fact["status"] == "changed" and identity in ack["covered_condition_ids"]:
                    covered.add(identity)
                    condition_states[identity] = "covered"
            elif ack and ack["binding"] == binding and fact["binding"] == binding and fact["receipt_id"] == ack["receipt_id"] and fact["status"] in {"expired", "exhausted"}:
                # Reactivation needs a new ACK, but a known terminal condition
                # must not consume execution quota or regain a sample budget.
                condition_states[fact["condition_id"]] = fact["status"]
        desired = {condition["id"] for condition in current["conditions"]}
        if not current["active"] or not enabled:
            status, covered = "inactive", set()
        elif not matching:
            status = "pending_rebind" if current["binding"] != binding or ack and ack["binding"] != binding else "pending"
        elif ack["status"] == "rejected":
            status = "rejected"
        else:
            status = "covered" if covered == desired else "partial" if covered else "uncovered"
        current["executable"] = bool(enabled and current["active"] and current["binding"]["colony_id"] == binding["colony_id"])
        current["coverage_status"] = status
        current["latest_facts"] = facts
        coverage.append({"observation_id": current["observation_id"], "revision": current["revision"], "status": status,
                         "binding": deepcopy(binding), "covered_condition_ids": sorted(covered),
                         "uncovered_condition_ids": sorted(desired - covered), "receipt_id": ack["receipt_id"] if matching else None,
                         "condition_states": condition_states, "ack": ack, "history": deepcopy(current["history"])})
        output.append(current)
    return {"intents": output, "coverage": coverage}


def _current(events, observation_id, binding):
    return next((row for row in project(events, binding=binding)["intents"] if row["observation_id"] == observation_id), None)


def prepare_request(events, *, observation_id, task_id, expected_revision, conditions, supported_kinds, binding, now_tick=None):
    binding = _binding(binding)
    _identity(observation_id, "observation_id")
    _identity(task_id, "task_id")
    if now_tick is not None and (not is_integer(now_tick) or now_tick < 0):
        raise ValueError("now_tick must be a nonnegative native tick")
    conditions = deepcopy(list(conditions))
    if not 1 <= len(conditions) <= MAX_CONDITIONS:
        raise ValueError(f"An observation requires 1..{MAX_CONDITIONS} conditions")
    ids, target_count = set(), 0
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) - CONDITION_REQUIRED_FIELDS - CONDITION_OPTIONAL_FIELDS:
            raise ValueError("Unsupported native observation condition fields")
        identity = _identity(condition.get("id"), "condition id")
        if identity in ids or condition.get("kind") not in supported_kinds:
            raise ValueError("Condition ids must be unique and kinds natively supported")
        ids.add(identity)
        if "map_id" in condition:
            _identity(condition["map_id"], "condition map_id")
        if condition["kind"] == "construction":
            _identity(condition.get("plan_id"), "construction condition plan_id")
        targets = condition.get("target_ids")
        if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)):
            raise ValueError("Condition requires unique native target ids")
        for target in targets:
            _identity(target, "target_id")
        target_count += len(targets)
        if not is_integer(condition.get("max_samples")) or not 1 <= condition["max_samples"] <= MAX_SAMPLES:
            raise ValueError(f"max_samples must be 1..{MAX_SAMPLES}")
        expiry = condition.get("expires_at_tick")
        if not is_integer(expiry) or not 1 <= expiry <= MAX_NATIVE_TICK:
            raise ValueError("expires_at_tick must be a positive native tick")
        if now_tick is not None and not now_tick < expiry <= now_tick + MAX_EXPIRY_HORIZON_TICKS:
            raise ValueError(f"Observation expiry must be within {MAX_EXPIRY_HORIZON_TICKS} ticks")
        interval = condition.get("sample_interval_ticks", 250)
        if not is_integer(interval) or not 1 <= interval <= MAX_SAMPLE_INTERVAL_TICKS:
            raise ValueError(f"sample_interval_ticks must be 1..{MAX_SAMPLE_INTERVAL_TICKS}")
    projected = project(events, binding=binding)
    current = next((row for row in projected["intents"] if row["observation_id"] == observation_id), None)
    if type(expected_revision) is not int or expected_revision != (current["revision"] if current else 0):
        raise ValueError("Observation revision conflict")
    coverage = {row["observation_id"]: row for row in projected["coverage"]}
    live_others = []
    for row in projected["intents"]:
        if not row["active"] or row["observation_id"] == observation_id:
            continue
        states = coverage[row["observation_id"]]["condition_states"]
        live = [condition for condition in row["conditions"] if states.get(condition["id"]) not in {"expired", "exhausted"}
                and (now_tick is None or condition["expires_at_tick"] > now_tick)]
        if live:
            live_others.append(live)
    if len(live_others) >= MAX_OBSERVATIONS or target_count + sum(len(condition["target_ids"]) for live in live_others for condition in live) > MAX_TARGETS:
        raise ValueError("Active native observation quota exceeded")
    return _event("observation.requested", "planner", observation_id=observation_id, task_id=task_id,
                  revision=expected_revision + 1, conditions=conditions, binding=binding)


def prepare_revoke(events, *, observation_id, expected_revision, reason):
    _identity(reason, "reason")
    # Recover the durable binding; revocation is independent of the live connection.
    requests = [event["payload"] for event in events if event.get("kind") == "observation.requested" and event.get("payload", {}).get("observation_id") == observation_id]
    if not requests:
        raise ValueError("Unknown observation intent")
    current = _current(events, observation_id, requests[-1]["binding"])
    if type(expected_revision) is not int or current["revision"] != expected_revision or not current["active"]:
        raise ValueError("Observation revision conflict or already revoked")
    return _event("observation.revoked", "planner", observation_id=observation_id, revision=expected_revision, reason=reason)


def prepare_coverage(events, *, observation_id, revision, binding, status, covered_condition_ids, receipt_id, reason=None):
    binding = _binding(binding)
    current = _current(events, observation_id, binding)
    if current is None or type(revision) is not int or revision != current["revision"] or not current["active"]:
        raise ValueError("Coverage must acknowledge the current active revision")
    if current["binding"]["colony_id"] != binding["colony_id"]:
        raise ValueError("Coverage cannot cross colonies")
    if status not in {"registered", "rejected"}:
        raise ValueError("Coverage requires a native registration acknowledgement")
    covered = list(covered_condition_ids)
    desired = {condition["id"] for condition in current["conditions"]}
    if len(covered) != len(set(covered)) or not set(covered).issubset(desired) or (status == "rejected" and covered):
        raise ValueError("Coverage may only acknowledge requested conditions")
    _identity(receipt_id, "receipt_id")
    previous = next(row for row in project(events, binding=binding)["coverage"] if row["observation_id"] == observation_id)["ack"]
    if previous and previous["binding"] == binding and previous["receipt_id"] == receipt_id and (previous["status"] != status or set(previous["covered_condition_ids"]) != set(covered)):
        raise ValueError("A native receipt cannot change registration contents")
    return _event("observation.coverage", "mod", observation_id=observation_id, revision=revision, binding=binding,
                  status=status, covered_condition_ids=covered, receipt_id=receipt_id, reason=reason)


def prepare_fact(events, *, observation_id, revision, condition_id, receipt_id, status, sample_tick, observation, binding, coverage=None):
    binding = _binding(binding)
    current = _current(events, observation_id, binding)
    if current is None or type(revision) is not int or revision != current["revision"] or not current["active"]:
        raise ValueError("Observation fact must target the current active revision")
    row = next(row for row in project(events, binding=binding)["coverage"] if row["observation_id"] == observation_id)
    ack = row["ack"]
    if row["receipt_id"] != receipt_id or not ack or ack["status"] != "registered" or condition_id not in ack["covered_condition_ids"]:
        raise ValueError("Observation fact must identify a current native receipt and condition")
    if row["condition_states"].get(condition_id) in {"expired", "exhausted"}:
        raise ValueError("A terminal observation condition requires a new registration")
    if status not in {"changed", "expired", "exhausted", "unknown"} or not is_integer(sample_tick) or sample_tick < 0:
        raise ValueError("Unsupported native observation status or sample tick")
    if not isinstance(observation, dict):
        raise ValueError("Native observation must be an object")
    return _event("observation.fact", "mod", observation_id=observation_id, revision=revision, condition_id=condition_id,
                  receipt_id=receipt_id, status=status, sample_tick=sample_tick, observation=observation,
                  coverage=coverage, binding=binding)
