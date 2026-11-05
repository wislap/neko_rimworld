"""Exact native manual area edits, separate from automatic mask changes."""

from copy import deepcopy

from .area_evidence import NATIVE_KINDS
from .ledger import is_integer


def validate_change(change, *, game_tick):
    if not isinstance(change, dict) or change.get("source") != "user_designator":
        raise ValueError("Area control requires an exact native user-designator source")
    for name in ("manual_id", "origin_load_epoch", "map_id", "object", "field", "area_kind", "designator"):
        if not isinstance(change.get(name), str) or not change[name].strip():
            raise ValueError("Native manual area source lacks " + name)
    cell = change.get("cell", {})
    if any(type(cell.get(axis)) is not int for axis in ("x", "z")):
        raise ValueError("Native manual area source requires its exact cell")
    if change["field"] != f"area_flag:{cell['x']}:{cell['z']}":
        raise ValueError("Native manual source cell and field disagree")
    if change["area_kind"] not in {"home", "build_roof", "no_roof", "allowed"}:
        raise ValueError("Unsupported native manual area kind")
    if type(change.get("tick")) is not int or not 0 <= change["tick"] <= game_tick:
        raise ValueError("Native manual area source has an invalid sample tick")
    if not is_integer(change.get("manual_order")) or change["manual_order"] < 1:
        raise ValueError("Native manual area source requires its persistent causal order")
    for name in ("old_value", "new_value"):
        value = change.get(name)
        if (not isinstance(value, dict) or type(value.get("enabled")) is not bool
                or not isinstance(value.get("native_instance"), str) or not value["native_instance"]):
            raise ValueError("Native manual area source requires complete " + name)
    ownership = change.get("ownership")
    if ownership is not None and (not isinstance(ownership, dict)
            or not isinstance(ownership.get("changeset_id"), str) or not ownership["changeset_id"]
            or not isinstance(ownership.get("value"), dict)
            or type(ownership["value"].get("enabled")) is not bool
            or ownership["value"].get("native_instance") != change["new_value"]["native_instance"]):
        raise ValueError("Native manual ownership must be the original AI field or null")
    if "ownership" not in change:
        raise ValueError("Native manual source must explicitly declare prior ownership")
    instances = {change[name]["native_instance"] for name in ("old_value", "new_value")}
    if len(instances) != 1 or change["old_value"] == change["new_value"]:
        raise ValueError("Manual area evidence must identify one changed native instance")
    if "manual_id" in change["new_value"] and change["new_value"]["manual_id"] != change["manual_id"]:
        raise ValueError("Manual area generation differs from its native source")
    return deepcopy(change)


def plan_stops(state, *, change, manual_events, open_takeovers):
    """Pause matching engineering areas, protecting only actually controlled cells."""
    opened = {event["seq"] for event in open_takeovers}
    latest = {}
    for event in manual_events:
        data = event["payload"]
        if data.get("takeover_seq") in opened:
            latest[(data["object"], data["field"])] = data
    result = []
    for project in state["plans"]:
        plan = project["plan"]
        if plan["map_id"] != change["map_id"]:
            continue
        by_id = {element["id"]: element for element in project["elements"]}
        for area in plan.get("areas", []):
            if NATIVE_KINDS.get(area["kind"]) != change["area_kind"]:
                continue
            required = {(cell["x"] + plan["origin"]["x"], cell["z"] + plan["origin"]["z"]) for cell in area["cells"]}
            if (change["cell"]["x"], change["cell"]["z"]) not in required:
                continue
            prior = by_id[area["id"]]
            # Allowed areas are not unique on a map: bind to a previously read
            # real area ID rather than infer a reference from shared geometry.
            if area["kind"] == "allowed":
                fact = prior.get("fact") or {}
                identities = fact.get("field_identities") or ([fact["field_identity"]] if fact.get("field_identity") else [])
                if not any(scope["object"] == change["object"] for scope in identities):
                    continue
            affected = [data for data in latest.values() if data["map_id"] == change["map_id"]
                        and data["object"] == change["object"] and data["area_kind"] == change["area_kind"]
                        and (data["cell"]["x"], data["cell"]["z"]) in required]
            if affected:
                result.append({"plan_id": project["plan_id"], "element_id": area["id"], "stage": "user_replaced",
                               "object_id": None, "field_identities": [{"object": data["object"], "field": data["field"]}
                                                                        for data in affected],
                               "native_action_id": change["manual_id"]})
    return result
