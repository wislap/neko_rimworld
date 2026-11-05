"""Verify committed configuration against later original native observations."""

import math
import struct

from ...workspace.construction_evidence import target_rotation_matches
from .service import WriteService, _path, _safe_path, _substitute, _unsafe

_MISSING = object()


def _equal(a, b):
    """JSON equality permits numeric spelling changes and keeps bool distinct."""
    if type(a) in (int, float) and type(b) in (int, float):
        return math.isfinite(a) and math.isfinite(b) and a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _single(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return _MISSING
    try:
        return struct.unpack("!f", struct.pack("!f", value))[0]
    except (OverflowError, struct.error):
        return _MISSING


def _float_equal(a, b):
    left, right = _single(a), _single(b)
    return left is not _MISSING and right is not _MISSING and left == right


def _production_setting_equal(name, a, b):
    if name == "ingredient_search_radius":
        return _float_equal(a, b)
    if name == "hp_range":
        return (isinstance(a, dict) and isinstance(b, dict) and set(a) == set(b) == {"min", "max"}
                and all(_float_equal(a[key], b[key]) for key in a))
    if name == "ingredient_filter":
        if not isinstance(a, dict) or not isinstance(b, dict) or a.keys() != b.keys():
            return False
        for key in a:
            if key in {"hit_points", "mental_break_chance"}:
                if not _production_setting_equal("hp_range", a[key], b[key]):
                    return False
            elif key in {"definitions", "special_filters"}:
                if not isinstance(a[key], list) or not isinstance(b[key], list):
                    return False
                selector = (lambda item: item["definition"]) if key == "special_filters" else None
                if not _equal(sorted(a[key], key=selector), sorted(b[key], key=selector)):
                    return False
            elif not _equal(a[key], b[key]):
                return False
        return True
    return _equal(a, b)


def _food_filter_equal(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict) or a.keys() != b.keys():
        return False
    for key in a:
        if key in {"hitpoints", "mental_break_chance"}:
            if (not isinstance(a[key], list) or not isinstance(b[key], list) or len(a[key]) != 2 or len(b[key]) != 2
                    or not all(_float_equal(x, y) for x, y in zip(a[key], b[key]))):
                return False
        elif key == "allowed_defs":
            if not isinstance(a[key], list) or not isinstance(b[key], list) or not _equal(sorted(a[key]), sorted(b[key])):
                return False
        elif not _equal(a[key], b[key]):
            return False
    return True


def _cells(value):
    if not isinstance(value, list) or any(not isinstance(cell, dict) or set(cell) != {"x", "z"}
                                         or any(type(cell[key]) is not int for key in cell) for cell in value):
        return None
    result = [(cell["z"], cell["x"]) for cell in value]
    return sorted(result) if len(result) == len(set(result)) else None


def _manual_id(value):
    return value is None or isinstance(value, str) and bool(value.strip())


def _flags(value, *, observed=False):
    required = {"x", "z", "enabled", "manual_id"} if observed else {"x", "z", "enabled"}
    if not isinstance(value, list) or any(not isinstance(cell, dict) or set(cell) != required
                                         or type(cell["x"]) is not int or type(cell["z"]) is not int
                                         or type(cell["enabled"]) is not bool
                                         or observed and not _manual_id(cell["manual_id"]) for cell in value):
        return None
    result = [(cell["z"], cell["x"], cell["enabled"], cell["manual_id"]) if observed
              else (cell["z"], cell["x"], cell["enabled"]) for cell in value]
    return sorted(result) if len({item[:2] for item in result}) == len(result) else None


def _selected_coordinates(value):
    if not isinstance(value, list) or any(not isinstance(cell, dict) or not {"x", "z"} <= cell.keys()
                                         or cell.keys() - {"x", "z", "enabled"}
                                         or type(cell["x"]) is not int or type(cell["z"]) is not int
                                         or "enabled" in cell and type(cell["enabled"]) is not bool for cell in value):
        return None
    return _cells([{"x": cell["x"], "z": cell["z"]} for cell in value])


def _area_row(data, descriptor, op):
    spec = descriptor["observation"]["extract"]
    rows = _path(data, spec["objects_path"])
    target = _substitute(spec["object_id"], op)
    matches = [row for row in rows if isinstance(row, dict)
               and _path(row, spec["identity_path"]) == target
               and all(_path(row, rule["path"]) == _substitute(rule["value"], op) for rule in spec.get("match", []))]
    return matches[0] if len(matches) == 1 else None


def _area_manual_ids(row):
    if row.get("manual_ids_complete") is not True or not isinstance(row.get("manual_ids"), list):
        return None
    result = {}
    for item in row["manual_ids"]:
        if (not isinstance(item, dict) or set(item) != {"x", "z", "manual_id"}
                or type(item["x"]) is not int or type(item["z"]) is not int or not _manual_id(item["manual_id"])):
            return None
        coordinate = (item["z"], item["x"])
        if coordinate in result:
            return None
        result[coordinate] = item["manual_id"]
    return result


def _verify_area_fields(values, area_id, instance, flags):
    """Every affected native cell has one stable ownership field and receipt."""
    if (not isinstance(area_id, str) or not area_id or not isinstance(instance, str) or not instance
            or not 1 <= len(flags) <= 64):
        return False
    expected = {(area_id, f"area_flag:{x}:{z}"): {"native_instance": instance, "enabled": enabled, "manual_id": manual_id}
                for z, x, enabled, manual_id in flags}
    return expected.keys() == values.keys() and all(_equal(values[key], value) for key, value in expected.items())


def _identifiers(value):
    return (isinstance(value, list) and all(isinstance(item, str) and item for item in value)
            and len(value) == len(set(value)))


def _readbacks(receipt, fields):
    if not isinstance(fields, list) or not fields or not isinstance(receipt.get("readback"), list):
        return None
    result = {}
    for field in fields:
        if not isinstance(field, dict) or not all(isinstance(field.get(key), str) for key in ("object", "field")):
            return None
        identity = (field["object"], field["field"])
        matches = [row for row in receipt["readback"] if isinstance(row, dict)
                   and (row.get("object"), row.get("field")) == identity and "value" in row]
        if identity in result or len(matches) != 1:
            return None
        result[identity] = matches[0]["value"]
    return result


def _one_readback(values, object_id, field):
    return values.get((object_id, field), _MISSING) if len(values) == 1 else _MISSING


def _production_bills(data, descriptor, op):
    spec = descriptor["observation"]["extract"]
    objects = _path(data, spec["objects_path"])
    if not isinstance(objects, list):
        return None
    target = _substitute(spec.get("object_id"), op)
    rows = [row for row in objects if isinstance(row, dict) and _path(row, spec["identity_path"]) == target
            and all(_path(row, rule["path"]) == _substitute(rule["value"], op) for rule in spec.get("match", []))]
    if len(rows) != 1 or _unsafe(rows[0]) or _safe_path(rows[0], spec["status_path"]) != spec.get("status_value", "observed"):
        return None
    row = rows[0]
    complete = spec.get("complete_path", spec["path"][:-1] + ["complete"])
    bills = _safe_path(row, spec["path"])
    if (_safe_path(row, complete) is not True or not isinstance(bills, list)
            or any(not isinstance(bill, dict) or _unsafe(bill) for bill in bills)
            or not _identifiers([bill.get("bill_id") for bill in bills])):
        return None
    return bills


def _verify_bill_creation(op, committed, bills, *, full_snapshot=False):
    if not isinstance(committed, dict) or not isinstance(committed.get("created"), dict):
        return False
    created = committed["created"]
    if (full_snapshot or op["op"] == "copy_production_bill") and not {"runtime_type", "settings", "presentation", "unfinished_id"} <= created.keys():
        return False
    identifier = created.get("bill_id")
    ids = [bill["bill_id"] for bill in bills]
    matches = [bill for bill in bills if bill["bill_id"] == identifier]
    recipe = op["value"].get("recipe") if op["op"] == "add_production_bill" else op.get("expect", {}).get("source", {}).get("recipe")
    if (len(matches) != 1 or not _equal(committed.get("bill_ids"), ids) or created.get("recipe") != recipe
            or matches[0].get("recipe") != recipe or matches[0].get("bill_kind") != "production"):
        return False
    bill = matches[0]
    current = {"bill_id": identifier, "recipe": bill.get("recipe")}
    # New native receipts carry the full creation snapshot. Old supported
    # receipts retain their original minimal bill-id/recipe contract.
    if "runtime_type" in created:
        current["runtime_type"] = bill.get("runtime_type")
        if current["runtime_type"] not in {"RimWorld.Bill_Production", "RimWorld.Bill_ProductionWithUft"}:
            return False
        prior = op.get("expect", {}).get("bill_ids")
        if not _identifiers(prior) or identifier in prior or ids != prior + [identifier]:
            return False
    if "settings" in created:
        settings = created["settings"]
        if not isinstance(settings, dict) or not settings or bill.get("supported_standard_production") is not True:
            return False
        current["settings"] = {key: bill.get(key, _MISSING) for key in settings}
        if any(value is _MISSING for value in current["settings"].values()):
            return False
        if not all(_production_setting_equal(key, current["settings"][key], value) for key, value in settings.items()):
            return False
    if "presentation" in created:
        current["presentation"] = bill.get("presentation", _MISSING)
    if "unfinished_id" in created:
        unfinished = bill.get("unfinished", _MISSING)
        if unfinished is _MISSING or unfinished is not None and (
                not isinstance(unfinished, dict) or unfinished.get("bound_bill_id") != identifier
                or bill.get("runtime_type") != "RimWorld.Bill_ProductionWithUft"):
            return False
        current["unfinished_id"] = unfinished.get("id") if isinstance(unfinished, dict) else None
    if current.keys() != created.keys():
        return False
    if any(key != "settings" and not _equal(current[key], created[key]) for key in created):
        return False
    requested = op["value"].get("settings", {}) if op["op"] == "add_production_bill" else {}
    return all(key in created.get("settings", {}) and _production_setting_equal(key, value, created["settings"][key])
               for key, value in requested.items())


def _policy_content(row):
    keys = ("native_instance", "label", "filter", "users", "users_complete")
    if (not isinstance(row, dict) or row.get("status") != "observed" or _unsafe(row)
            or not all(key in row for key in keys) or row["users_complete"] is not True
            or not isinstance(row["native_instance"], str) or not row["native_instance"] or not _identifiers(row["users"])):
        return None
    return {key: row[key] for key in keys}


def _verify_food_structure(op, data, committed):
    structure, rows = data.get("structure", {}), data.get("objects")
    ids = structure.get("policy_ids")
    if (data.get("complete") is not True or data.get("users_complete") is not True or not isinstance(rows, list)
            or not _identifiers(ids) or not isinstance(committed, dict) or not _equal(ids, committed.get("policy_ids"))):
        return False
    previous = op.get("expect", {}).get("policy_ids")
    if not _identifiers(previous):
        return False
    if op["op"] == "delete_food_policy":
        identifier = op["policy"]
        return (identifier in previous and ids == [item for item in previous if item != identifier]
                and committed.get("policy_id") == identifier and committed.get("policy", _MISSING) is None
                and not any(isinstance(row, dict) and row.get("policy_id") == identifier for row in rows))
    created = committed.get("created")
    if not isinstance(created, dict) or not isinstance(created.get("content"), dict):
        return False
    identifier = created.get("policy_id")
    if not isinstance(identifier, str) or not identifier or identifier in previous or ids != previous + [identifier]:
        return False
    matches = [row for row in rows if isinstance(row, dict) and row.get("policy_id") == identifier]
    content = _policy_content(matches[0]) if len(matches) == 1 else None
    if content is None or not _equal(content, created["content"]) or content["label"] != op["value"]["label"]:
        return False
    if op["op"] == "copy_food_policy":
        source = op.get("expect", {}).get("source", {})
        if (not isinstance(source.get("native_instance"), str) or source["native_instance"] == content["native_instance"]
                or not _food_filter_equal(source.get("filter"), content["filter"])):
            return False
    return True


def _verify_region_creation(op, data, committed):
    structure, rows = data.get("structure", {}), data.get("objects")
    if (data.get("complete") is not True or data.get("map_id") != op["map"] or not isinstance(rows, list)
            or not isinstance(committed, dict) or not isinstance(committed.get("created"), dict)):
        return False
    created = committed["created"]
    if (not _equal({key: value for key, value in committed.items() if key != "created"}, structure)
            or not _identifiers(structure.get("zone_ids")) or not _identifiers(structure.get("allowed_area_ids"))):
        return False
    previous = op.get("expect", {})
    identifier = created.get("id")
    changed = "allowed_area_ids" if op["op"] == "create_allowed_area" else "zone_ids"
    unchanged = "zone_ids" if changed == "allowed_area_ids" else "allowed_area_ids"
    if (not _identifiers(previous.get(changed)) or identifier in previous[changed]
            or structure[changed] != previous[changed] + [identifier]
            or not _equal(structure[unchanged], previous.get(unchanged))):
        return False
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == identifier]
    if len(matches) != 1 or _unsafe(matches[0]) or matches[0].get("status") != "observed":
        return False
    current = matches[0]
    return (isinstance(created.get("native_instance"), str) and bool(created["native_instance"])
            and all(key in current and _equal(current[key], value) for key, value in created.items())
            and current.get("map_id") == op["map"]
            and current.get("kind") == ("allowed" if op["op"] == "create_allowed_area" else op["value"]["kind"])
            and _cells(current.get("cells")) is not None and _cells(current["cells"]) == _cells(op["value"].get("cells"))
            and ("label" not in op["value"] or current.get("label") == op["value"]["label"]))


def _verify_region_removal(op, data, committed, request):
    """An explicit native miss and complete map structure prove deletion."""
    zone = op["op"] == "remove_zone"
    identifier = op["zone" if zone else "area"]
    structure, previous, rows = data.get("structure"), op.get("expect", {}), data.get("objects")
    changed, unchanged = ("zone_ids", "allowed_area_ids") if zone else ("allowed_area_ids", "zone_ids")
    if (op.get("value") is not False or data.get("map_id") != op["map"] or data.get("complete") is not True
            or data.get("structure_complete") is not True or not isinstance(structure, dict)
            or structure.get("map_id") != op["map"] or not isinstance(rows, list)
            or not _identifiers(structure.get(changed)) or not _identifiers(structure.get(unchanged))
            or not _identifiers(previous.get(changed)) or not _identifiers(previous.get(unchanged))
            or previous.get("map_id") != op["map"] or previous.get("removed_id") != identifier
            or identifier not in previous[changed] or identifier in structure[changed]
            or structure[changed] != [item for item in previous[changed] if item != identifier]
            or not _equal(structure[unchanged], previous[unchanged])
            or any(not isinstance(row, dict) or row.get("id") == identifier for row in rows)):
        return False
    arguments = request.get("arguments", {})
    selected, missing = arguments.get("ids"), data.get("missing_ids")
    if (not _identifiers(selected) or identifier not in selected or not _identifiers(missing) or identifier not in missing
            or not isinstance(committed, dict) or committed.get("removed_id") != identifier
            or committed.get("removed", _MISSING) is not None
            or not _equal({key: value for key, value in committed.items() if key not in {"removed_id", "removed"}}, structure)):
        return False
    original = previous.get("removed")
    if (not isinstance(original, dict) or original.get("id") != identifier or original.get("map_id") != op["map"]
            or not isinstance(original.get("native_instance"), str) or not original["native_instance"]):
        return False
    if zone:
        return (original.get("kind") in {"stockpile", "growing"} and original.get("references_complete") is True
                and original.get("bill_references") == []
                and (original["kind"] != "stockpile" or original.get("storage_group_members_complete") is True
                     and original.get("storage_group_id", _MISSING) is None and original.get("storage_group_members") == []))
    return original.get("kind") == "allowed" and original.get("users_complete") is True and original.get("users") == []


def _verify_construction(op, data, values):
    """Confirm issued-element existence; task completion checks stages separately."""
    element = op["value"]
    if (data.get("format") != "construction_intents.v1" or data.get("complete") is not True
            or data.get("map_id") != op["map"] or not isinstance(data.get("objects"), list)):
        return False
    rows = [row for row in data["objects"] if isinstance(row, dict)
            and row.get("plan_id") == element.get("plan_id") and row.get("element_id") == element.get("element_id")]
    if len(rows) != 1:
        return False
    row = rows[0]
    identity = row.get("field_identity", {})
    committed = _one_readback(values, identity.get("object"), identity.get("field"))
    if (not isinstance(committed, dict) or row.get("status") != "observed" or _unsafe(row) or identity.get("object") != op["map"]
            or row.get("map_id") != op["map"] or not _equal(row.get("element"), element)
            or not _equal(committed.get("element"), element) or not _equal(committed.get("field_identity"), identity)
            or not isinstance(row.get("source_id"), str) or row["source_id"] != committed.get("source_id")):
        return False
    if op["op"] == "restart_blueprint":
        previous = op.get("expect", {}).get("intent", {})
        if (previous.get("stage") not in {"destroyed", "user_cancelled", "model_cancelled"}
                or not isinstance(previous.get("source_id"), str) or previous["source_id"] == row["source_id"]
                or previous.get("plan_id") != element.get("plan_id") or previous.get("element_id") != element.get("element_id")
                or previous.get("map_id") != op["map"] or not _equal(previous.get("element"), element)):
            return False
    lineage = row.get("lineage")
    if not _identifiers(lineage) or not lineage or lineage[0] != row["source_id"]:
        return False
    if row.get("stage") == "floor_complete":
        return (element.get("kind") == "floor" and row.get("floor_observed") is True
                and row.get("provenance") == "native_transition" and len(lineage) > 1 and row.get("target") is None
                and row.get("previous_object_id") == lineage[-1])
    target = row.get("target")
    if row.get("stage") not in {"blueprint", "frame", "built"} or not isinstance(target, dict) or target.get("id") != lineage[-1]:
        return False
    if (not all(_equal(target.get(key), element.get(key)) for key in ("definition", "x", "z", "stuff"))
            or not target_rotation_matches(row, committed=committed)):
        return False
    return ((row["stage"] == "blueprint" and row.get("provenance") == "native_creation" and lineage == [row["source_id"]])
            or (row["stage"] in {"frame", "built"} and row.get("provenance") == "native_transition" and len(lineage) > 1))


def _verify_cancellation(op, data, values):
    if (data.get("format") != "construction_intents.v1" or data.get("complete") is not True
            or data.get("map_id") != op["map"] or not isinstance(data.get("objects"), list)):
        return False
    rows = [row for row in data["objects"] if isinstance(row, dict)
            and row.get("plan_id") == op.get("plan_id") and row.get("element_id") == op.get("element_id")]
    if len(rows) != 1:
        return False
    row, prior = rows[0], op.get("expect", {})
    identity = row.get("field_identity", {})
    committed = _one_readback(values, identity.get("object"), identity.get("field"))
    return (op.get("value") is False and row.get("status") == "observed" and not _unsafe(row)
            and row.get("stage") == "model_cancelled" and row.get("target", _MISSING) is None
            and row.get("map_id") == op["map"] and identity.get("object") == op["map"]
            and row.get("provenance") == "native_creation" and _equal(row.get("cancel_guard"), committed)
            and prior.get("stage") == "blueprint" and prior.get("plan_id") == op["plan_id"]
            and prior.get("element_id") == op["element_id"] and prior.get("map_id") == op["map"]
            and isinstance(prior.get("source_id"), str) and bool(prior["source_id"]) and prior["source_id"] == row.get("source_id")
            and row.get("lineage") == [prior["source_id"]] and _equal(prior.get("element"), row.get("element")))


def verify_op(directory, op, observation, connection, colony, epoch, commit_tick, receipt, fields):
    """Confirm requested effects using a strictly later, bound native observation."""
    descriptor = next((item for item in directory["operations"] if item["op"] == op.get("op")), None)
    if descriptor is None:
        return False
    response, request = observation.get("response") or {}, observation.get("request") or {}
    payload = response.get("payload") or {}
    read = descriptor.get("verification_read", descriptor["observation"]["read"])
    if ((response.get("connection_epoch"), response.get("colony_id"), response.get("load_epoch")) != (connection, colony, epoch)
            or response.get("type") != "query_result" or not response.get("request_id")
            or type(response.get("game_tick")) is not int or response["game_tick"] <= commit_tick
            or request.get("operation") != read.get("native_operation")
            or payload.get("status") not in ("ok", "partial") or payload.get("issues")
            or not isinstance(observation.get("observation_id"), str) or not observation["observation_id"]):
        return False
    data = payload.get("data")
    values = _readbacks(receipt, fields)
    if not isinstance(data, dict) or _unsafe(data) or values is None:
        return False
    name = op["op"]
    if name in {"place_blueprint", "restart_blueprint"}:
        return _verify_construction(op, data, values)
    if name == "cancel_blueprint":
        return _verify_cancellation(op, data, values)
    if name in {"add_production_bill", "copy_production_bill", "remove_production_bill"}:
        bills = _production_bills(data, descriptor, op)
        committed = _one_readback(values, op["worktable"], "production_bills")
        if bills is None or committed is _MISSING:
            return False
        if name != "remove_production_bill":
            supports_initial_settings = "settings" in descriptor["schema"]["properties"]["value"].get("properties", {})
            return _verify_bill_creation(op, committed, bills, full_snapshot=supports_initial_settings)
        current_ids = [bill["bill_id"] for bill in bills]
        return (isinstance(committed, dict) and committed.get("removed") == op["bill"]
                and op["bill"] not in current_ids and _equal(committed.get("bill_ids"), current_ids)
                and _identifiers(op.get("expect", {}).get("bill_ids"))
                and op["bill"] in op["expect"]["bill_ids"]
                and current_ids == [identifier for identifier in op["expect"]["bill_ids"] if identifier != op["bill"]])
    if name in {"create_food_policy", "copy_food_policy", "delete_food_policy"}:
        return _verify_food_structure(op, data, _one_readback(values, "food_policies", "food_policy_structure"))
    if name in {"create_zone", "create_allowed_area"}:
        field = "zone_structure" if name == "create_zone" else "area_structure"
        return _verify_region_creation(op, data, _one_readback(values, op["map"], field))
    if name in {"remove_zone", "remove_allowed_area"}:
        target, prefix = (op["zone"], "zone_removed:") if name == "remove_zone" else (op["area"], "area_removed:")
        return _verify_region_removal(op, data, _one_readback(values, op["map"], prefix + target), request)
    service = WriteService(directory, [observation], None, lambda: (connection, colony, epoch))
    extracted = service._evidence(op, descriptor)
    if extracted is None or extracted[1] != observation["observation_id"]:
        return False
    value = extracted[0]
    if name in {"set_bill", "set_production_bill"}:
        if not isinstance(value, dict) or set(value) != set(op["value"]) or len(values) != len(value):
            return False
        if {field.removeprefix("bill:") for _, field in values} != set(value):
            return False
        return all(field.startswith("bill:") and (name == "set_bill" or object_id == op["bill"])
                   and _production_setting_equal(field[5:], value[field[5:]], committed)
                   and _production_setting_equal(field[5:], value[field[5:]], op["value"][field[5:]])
                   for (object_id, field), committed in values.items())
    if name == "set_food_policy_content":
        committed = _one_readback(values, op["policy"], "food_policy_content")
        return (isinstance(value, dict) and value.get("users_complete") is True
                and value.get("native_instance") == op.get("expect", {}).get("native_instance")
                and _equal(value, committed) and value.get("label") == op["value"]["label"]
                and _food_filter_equal(value.get("filter"), op["value"].get("filter")))
    if name in {"set_area_cells", "set_area_cell_flags"}:
        if not isinstance(value, dict) or value.get("native_instance") != op.get("expect", {}).get("native_instance"):
            return False
        row = _area_row(data, descriptor, op)
        if row is None:
            return False
        if name == "set_area_cell_flags":
            desired, current = _flags(op["value"].get("cells")), _flags(value.get("cells"), observed=True)
            selected = _selected_coordinates(request.get("arguments", {}).get("cells"))
            previous = _flags(op.get("expect", {}).get("cells"), observed=True)
            if (desired is None or current is None or [item[:3] for item in current] != desired or selected is None
                    or previous is None or [item[:2] for item in previous] != [item[:2] for item in desired]
                    or any(before[3] != after[3] for before, after in zip(previous, current))
                    or selected != [item[:2] for item in desired]):
                return False
        else:
            desired, previous = _cells(op["value"].get("cells")), _cells(op.get("expect", {}).get("cells"))
            revision = value.get("manual_revision")
            generations = _area_manual_ids(row)
            if (desired is None or previous is None or _cells(value.get("cells")) != desired
                    or not isinstance(revision, str) or not revision or revision != op.get("expect", {}).get("manual_revision")
                    or generations is None):
                return False
            desired_set, previous_set = set(desired), set(previous)
            current = [(z, x, (z, x) in desired_set, generations.get((z, x))) for z, x in sorted(desired_set ^ previous_set)]
        return _verify_area_fields(values, row.get("id"), value["native_instance"], current)
    if name == "set_zone_cells":
        committed = _one_readback(values, op["zone"], "zone_cells")
        return (isinstance(value, dict) and _equal(value, committed)
                and value.get("native_instance") == op.get("expect", {}).get("native_instance")
                and _cells(value.get("cells")) is not None and _cells(value["cells"]) == _cells(op["value"].get("cells")))
    return "value" in op and _equal(value, op["value"]) and all(_equal(committed, value) for committed in values.values())
