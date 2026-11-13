"""Versioned levels for native colony vitals; no game or scheduler dependency."""

import hashlib
import json
from pathlib import Path

LEVELS = ("ok", "watch", "danger")


def load_policy(path=None):
    raw = Path(path or Path(__file__).with_name("vitals_policy.v1.json")).read_bytes()
    data = json.loads(raw)
    if data.get("version") != "vitals_policy.v1":
        raise ValueError("Unsupported vitals policy")
    return data, hashlib.sha256(raw).hexdigest()


def domain_level(policy, domain):
    """``unknown`` for an unavailable domain: a failed read never reads as healthy."""
    if domain.get("status") == "unavailable":
        return "unknown"
    name, numbers = domain["domain"], domain.get("numbers", {})
    rules = policy[name]
    if name == "food":
        days = numbers.get("days_edible")
        if days is not None and days < rules["days_edible_danger"]:
            return "danger"
        if days is not None and days < rules["days_edible_watch"]:
            return "watch"
        edible = numbers.get("ready_nutrition", 0) + numbers.get("raw_nutrition", 0)
        lowest = numbers.get("min_unroofed_hit_points_ratio")
        if (edible > 0 and numbers.get("unroofed_nutrition", 0) > rules["unroofed_share_watch"] * edible
                and lowest is not None and lowest < rules["unroofed_hit_points_watch"]):
            return "watch"
        return "ok"
    if name == "life":
        if any(numbers.get(key, 0) > 0 for key in rules["danger_counts"]):
            return "danger"
        if any(numbers.get(key, 0) > 0 for key in rules["watch_counts"]) or (rules["watch_bleeding"] and numbers.get("max_bleed_rate", 0) > 0):
            return "watch"
        return "ok"
    if name == "shelter":
        return "watch" if any(numbers.get(key, 0) > 0 for key in rules["watch_counts"]) else "ok"
    if name == "labor":
        return "danger" if rules["danger_uncovered_work"] and numbers.get("uncovered_work") else "ok"
    raise ValueError(f"Unknown vitals domain {name}")


def levels(policy, data):
    return {domain["domain"]: domain_level(policy, domain) for domain in data.get("domains", [])}
