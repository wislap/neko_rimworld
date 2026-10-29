"""Validation and deterministic projections for journaled wake conditions."""

import math
import operator

COMPARISONS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
               "le": operator.le, "gt": operator.gt, "ge": operator.ge}


def _incomplete_coverage(value):
    if value is False or value is None:
        return True
    if isinstance(value, str):
        return value.lower() in {"partial", "incomplete", "unknown", "unavailable", "truncated", "error", "failed"}
    if isinstance(value, dict):
        status = value.get("status")
        return (isinstance(status, str) and _incomplete_coverage(status)
                or value.get("complete") is False or value.get("truncated") is True
                or bool(value.get("issues")) or value.get("has_more") is True
                or value.get("next_offset") is not None
                or any(_incomplete_coverage(value[key]) for key in ("coverage", "scope", "page")
                       if key in value and isinstance(value[key], dict)))
    return False


def path_value(value, path):
    def check(node):
        if not isinstance(node, dict):
            return
        if (node.get("status") in {"partial", "incomplete", "unknown", "unavailable", "error", "failed"}
                or node.get("issues") or node.get("truncated") is True or node.get("complete") is False
                or node.get("has_more") is True or node.get("next_offset") is not None
                or "coverage" in node and _incomplete_coverage(node["coverage"])):
            raise ValueError("Observation path is incomplete")
    check(value)
    for part in path:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            raise ValueError("Observation path is missing")
        check(value)
    return value


def numeric(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_condition(condition):
    if not isinstance(condition, dict):
        raise ValueError("Condition must be an object")
    condition = dict(condition)
    kind = condition.get("kind")
    required = {"deadline": {"kind", "tick"}, "periodic": {"kind", "interval_ticks"},
                "event": {"kind", "event_kind", "filters"},
                "numeric": {"kind", "tool", "arguments", "path", "op", "value"}}
    if kind not in required or not required[kind] <= condition.keys():
        raise ValueError("Unknown or incomplete condition")
    allowed = required[kind] | ({"interval_ticks"} if kind == "numeric" else set())
    if condition.keys() != allowed and not (kind == "numeric" and condition.keys() == required[kind]):
        raise ValueError("Unexpected condition fields")
    if kind in {"deadline", "periodic"}:
        value = condition["tick" if kind == "deadline" else "interval_ticks"]
        if type(value) is not int or not 0 <= value < 2**63 or (kind == "periodic" and value == 0):
            raise ValueError("Invalid tick or interval")
    if kind == "event":
        if not isinstance(condition["event_kind"], str) or not condition["event_kind"]:
            raise ValueError("Event kind required")
        if not isinstance(condition["filters"], dict) or any(not isinstance(k, str) for k in condition["filters"]):
            raise ValueError("Event filters must be an object")
        for value in condition["filters"].values():
            if isinstance(value, dict) and set(value) == {"op", "value"}:
                if not isinstance(value["op"], str) or value["op"] not in COMPARISONS or not numeric(value["value"]):
                    raise ValueError("Event comparison is invalid")
    if kind == "numeric":
        if not isinstance(condition["tool"], str) or not condition["tool"] or not isinstance(condition["arguments"], dict):
            raise ValueError("Numeric condition requires a read and arguments")
        if not isinstance(condition["path"], list) or any(type(p) not in (str, int) for p in condition["path"]):
            raise ValueError("Numeric observation path must be a list")
        if not isinstance(condition["op"], str) or condition["op"] not in COMPARISONS or not numeric(condition["value"]):
            raise ValueError("Numeric comparison is invalid")
        interval = condition.get("interval_ticks", 2500)
        if type(interval) is not int or not 0 <= interval < 2**63:
            raise ValueError("Invalid numeric interval")
        condition["interval_ticks"] = max(250, interval)
    return condition
