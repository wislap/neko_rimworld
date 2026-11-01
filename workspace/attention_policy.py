"""Versioned native attention classification; no game or scheduler dependency."""

import hashlib
import json
from pathlib import Path


def load_policy(path=None):
    raw = Path(path or Path(__file__).with_name("attention_policy.v1.json")).read_bytes()
    data = json.loads(raw)
    if data.get("version") != "attention_policy.v1":
        raise ValueError("Unsupported attention policy")
    return data, hashlib.sha256(raw).hexdigest()


def normalize_signal(signal):
    """Normalize the native attention_changed fixture or an already normalized signal."""
    if "current" not in signal and "previous" not in signal:
        return dict(signal)
    current = signal.get("current") or {}
    previous = signal.get("previous") or {}
    edge = signal["edge"]
    if edge == "priority_changed":
        order = {"Medium": 0, "High": 1, "Critical": 2}
        edge = "upgraded" if order.get(current.get("priority"), -1) > order.get(previous.get("priority"), -1) else "priority_decreased"
    elif edge == "targets_changed":
        edge = "added" if signal.get("added") else "removed"
    category = {"alerts": "alert", "conditions": "condition", "threats": "threat"}.get(signal["category"], signal["category"])
    return {**(current or previous), "category": category, "edge": edge,
            "source_id": signal["source_id"], "added": signal.get("added", []),
            "removed": signal.get("removed", []), "map_id": signal.get("map_id"),
            "sample_tick": signal.get("sample_tick")}


def normalize_attention_event(event):
    """Expand a native snapshot/change into ordered attention.signal payloads.

    Journal appends each returned payload as its own event in order, in the
    same transaction, retaining native event seq and ordinal. Snapshot marker
    establishes map/partition completeness before its baseline rows. Partial
    and unavailable partitions never prove absence.
    """
    kind = event["kind"]
    if kind == "attention_changed":
        if event["edge"] == "status_changed":
            return []
        changed = dict(event)
        changed["source_id"] = _source_id(event.get("map_id"), event["category"], event["source_id"])
        return [normalize_signal(changed)]
    if kind != "attention_snapshot":
        raise ValueError("Expected attention_snapshot or attention_changed")
    map_id = event.get("map_id")
    partitions = {}
    output = []
    for category in ("alerts", "conditions", "threats"):
        section = event[category]
        rows = section.get("rows", [])
        partitions[category] = {"status": section["status"], "truncated": bool(section.get("truncated")),
                                "source_ids": [_source_id(map_id, category, row["id"]) for row in rows]}
        for row in rows:
            output.append({**row, "category": {"alerts": "alert", "conditions": "condition", "threats": "threat"}[category],
                           "source_id": _source_id(map_id, category, row["id"]),
                           "edge": "baseline", "map_id": map_id, "sample_tick": event.get("sample_tick")})
    return [{"category": "snapshot", "source_id": _source_id(map_id, "snapshot", "snapshot"),
             "edge": "snapshot", "map_id": map_id, "sample_tick": event.get("sample_tick"),
             "partitions": partitions}, *output]


def _source_id(map_id, category, identity):
    return json.dumps([map_id, category, identity], ensure_ascii=False, separators=(",", ":"))

def target_identity(target):
    """Native target identity shared by comparison and per-object cooldown."""
    return tuple(target.get(field) for field in ("id", "kind", "map_id", "x", "z", "tile"))


def merge_observation(previous, current):
    """Keep confirmed targets when the new native report is incomplete."""
    result = dict(current)
    if current.get("targets_complete") is False:
        known = {target_identity(target): target for target in previous.get("targets", [])}
        known.update({target_identity(target): target for target in current.get("targets", [])})
        result["targets"] = list(known.values())
    return result

def baseline_changes(previous, current):
    """Translate confirmed reconnect changes to the same normalized delta edges."""
    changes = []
    if previous.get("priority") != current.get("priority") and previous.get("priority") and current.get("priority"):
        order = {"Medium": 0, "High": 1, "Critical": 2}
        edge = "upgraded" if order.get(current["priority"], -1) > order.get(previous["priority"], -1) else "priority_decreased"
        changes.append({**current, "edge": edge})
    def targets(row):
        return {target_identity(target): target for target in row.get("targets", [])}
    old, new = targets(previous), targets(current)
    added = ([value for key, value in new.items() if key not in old]
             if previous.get("targets_complete") is True else [])
    removed = ([value for key, value in old.items() if key not in new]
               if current.get("targets_complete") is True else [])
    if added:
        changes.append({**current, "edge": "added", "added": added, "removed": []})
    if removed:
        changes.append({**current, "edge": "removed", "added": [], "removed": removed})
    return changes

def classify(policy, signal):
    """Classify native identity and edge; disappearances are record-only."""
    signal = normalize_signal(signal)
    category, edge = signal["category"], signal["edge"]
    source = signal["source_id"]
    level = "record"
    if edge in ("appeared", "added", "upgraded"):
        if category == "threat":
            level = "interrupt"
        elif category == "alert":
            if signal.get("priority") == "Critical":
                level = "interrupt"
            elif signal.get("priority") == "High" and signal.get("runtime_type") in policy["acute_high_alerts"]:
                level = "focus"
        elif category == "letter":
            if signal.get("def") in policy["interrupt_letters"]:
                level = "interrupt"
            elif signal.get("def") in policy["focus_letters"]:
                level = "focus"
        elif category == "condition" and signal.get("def") in policy["negative_conditions"]:
            level = "focus"
    return {"source_id": source, "edge": edge, "level": level}
