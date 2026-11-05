"""Validation of the Mod-owned colony_write.v1 wire contract."""

import re

from ..values import is_nonblank_text


def _bad():
    raise ValueError("Invalid write payload.")


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        _bad()


def _require_text(value, limit=256):
    if not is_nonblank_text(value) or len(value) > limit:
        _bad()


def _epoch(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        _bad()


def validate_request(mode, args):
    if mode in ("dry_run", "commit"):
        _fields(args, ("changeset_id", "author", "entry_id", "load_epoch", "idempotency_key", "ops"))
        for name in ("changeset_id", "author", "entry_id", "load_epoch", "idempotency_key"):
            _require_text(args[name])
        if args["author"] != "planner":
            _bad()
        _epoch(args["load_epoch"])
        ops = args["ops"]
        if not isinstance(ops, list) or not 1 <= len(ops) <= 64:
            _bad()
        for op in ops:
            if not isinstance(op, dict) or len(op) > 16 or not {"op", "evidence", "expect", "value"} <= op.keys():
                _bad()
            _require_text(op["op"])
            _require_text(op["evidence"])
            # The Mod's JObject null check treats JSON null as an existing token.
            targets = {k: v for k, v in op.items() if k not in {"op", "evidence", "expect", "value"}}
            if not targets.keys() & {"pawn", "bill", "thing", "worktable", "storage", "research", "map", "policy", "bed", "zone", "area"}:
                _bad()
            if any(not isinstance(v, str) or not v.strip() for v in targets.values()):
                _bad()
            expected, value = op["expect"], op["value"]
            if op["op"] == "add_production_bill":
                _fields(op, ("op", "worktable", "value", "expect", "evidence"))
                _require_text(op['worktable'])
                _fields(expected, ("bill_ids",))
                if not isinstance(value, dict) or "recipe" not in value or set(value) - {"recipe", "settings"}:
                    _bad()
                if "settings" in value and not isinstance(value["settings"], dict):
                    _bad()
                _require_text(value["recipe"])
                identifiers = expected["bill_ids"]
                if not isinstance(identifiers, list):
                    _bad()
                for identifier in identifiers:
                    _require_text(identifier)
                if len(set(identifiers)) != len(identifiers):
                    _bad()
            elif op["op"] in {"place_blueprint", "restart_blueprint", "cancel_blueprint", "create_food_policy", "copy_food_policy", "set_food_policy_content", "delete_food_policy", "remove_production_bill", "copy_production_bill", "create_zone", "create_allowed_area", "remove_zone", "remove_allowed_area", "set_zone_cells", "set_area_cells", "set_area_cell_flags"}:
                _validate_management_operation(op)
            elif isinstance(expected, dict) or isinstance(value, dict):
                if not isinstance(expected, dict) or not isinstance(value, dict) or not expected or expected.keys() != value.keys():
                    _bad()
    elif mode == "undo":
        _fields(args, ("token",))
        _require_text(args["token"])
    elif mode == "set_paused":
        guarded = isinstance(args, dict) and "expected_owner" in args
        _fields(args, ("paused", "role", "expected_owner") if guarded else ("paused", "role"))
        if type(args["paused"]) is not bool or args["role"] not in ("planner", "frontdesk", "policy"):
            _bad()
        if args["role"] in ("frontdesk", "policy") and not args["paused"]:
            _bad()
        if guarded and (args["paused"] or args["role"] != "planner" or args["expected_owner"] not in
                        ("planner", "frontdesk", "reflex", "automatic_letter")):
            _bad()
    else:
        _bad()


def _validate_management_operation(op):
    """Distinct intent and observed CAS shapes for supported structural writes.

The native catalog supplies field-level business validation; the wire contract
keeps each operation's target and envelope exact instead of weakening old ops.
"""
    name, value, expected = op["op"], op["value"], op["expect"]
    target = "map" if name in {"place_blueprint", "restart_blueprint", "cancel_blueprint", "create_zone", "create_allowed_area"} else "bill" if name in {"remove_production_bill", "copy_production_bill"} else "zone" if name in {"set_zone_cells", "remove_zone"} else "area" if name in {"set_area_cells", "set_area_cell_flags", "remove_allowed_area"} else "policy"
    names = {"op", target, "value", "expect", "evidence"}
    if name in {"remove_production_bill", "copy_production_bill"}:
        names.update(("worktable", "map"))
    if name in {"set_zone_cells", "set_area_cells", "set_area_cell_flags", "remove_zone", "remove_allowed_area"}:
        names.add("map")
    if name == "cancel_blueprint":
        names.update(("plan_id", "element_id"))
    _fields(op, names)
    if not isinstance(expected, dict) or not expected:
        _bad()
    if name == "cancel_blueprint":
        if value is not False:
            _bad()
        _require_text(op["plan_id"], 128)
        _require_text(op["element_id"], 128)
        return
    if name in {"remove_zone", "remove_allowed_area"}:
        if value is not False:
            _bad()
        return
    if name in {"remove_production_bill", "copy_production_bill"}:
        if value is not (name == "copy_production_bill"):
            _bad()
        return
    if not isinstance(value, dict) or not value:
        _bad()
    if name in {"create_zone", "create_allowed_area", "set_zone_cells", "set_area_cells", "set_area_cell_flags"}:
        required = {"cells", "kind"} if name == "create_zone" else {"cells"}
        if not required <= value.keys() or value.keys() - required - ({"label"} if name in {"create_zone", "create_allowed_area"} else set()):
            _bad()
        cells = value["cells"]
        if not isinstance(cells, list) or len(cells) > (64 if name == "set_area_cell_flags" else 1024):
            _bad()
        for cell in cells:
            _fields(cell, ("x", "z", "enabled") if name == "set_area_cell_flags" else ("x", "z"))
            if any(type(cell[k]) is not int for k in ("x", "z")):
                _bad()
            if name == "set_area_cell_flags" and type(cell["enabled"]) is not bool:
                _bad()
        if name == "create_zone" and (value["kind"] not in {"stockpile", "growing"} or not cells):
            _bad()
        if "label" in value:
            _require_text(value["label"], 128)
    elif name in {"create_food_policy", "copy_food_policy"}:
        _fields(value, ("label",))
        _require_text(value["label"], 128)
        if name == "create_food_policy" and op["policy"] != "new":
            _bad()
    elif name == "set_food_policy_content":
        _fields(value, ("label", "filter", "impact"))
        _require_text(value["label"], 128)
        if not isinstance(value["filter"], dict) or not isinstance(value["impact"], dict):
            _bad()
    elif name == "delete_food_policy":
        _fields(value, ("impact",))
        if not isinstance(value["impact"], dict):
            _bad()
    else:
        required = {"plan_id", "element_id", "definition", "x", "z", "rotation", "kind"}
        if not required <= value.keys() or value.keys() - required - {"stuff"}:
            _bad()
        _require_text(value["element_id"], 128)
        _require_text(value["plan_id"], 128)
        _require_text(value["definition"])
        if any(type(value[k]) is not int for k in ("x", "z", "rotation")) or not 0 <= value["rotation"] <= 3 or value["kind"] not in {"building", "floor"}:
            _bad()
        if value.get("stuff") is not None:
            _require_text(value["stuff"])


def validate_result(raw):
    status = raw.get("status")
    if status not in ("ok", "failed", "stale_session", "unsupported", "busy") or raw.get("issues") != []:
        _bad()
    if status != "ok":
        _fields(raw, ("status", "error_code", "message", "issues"))
        _require_text(raw["error_code"], 128)
        _require_text(raw["message"], 2048)
        return
    _fields(raw, ("status", "data", "issues"))
    data = raw["data"]
    if not isinstance(data, dict):
        _bad()
    kind = data.get("status")
    if kind is None and "status" not in data:
        guarded = "expected_owner" in data
        _fields(data, ("paused", "role", "expected_owner") if guarded else ("paused", "role"))
        if type(data["paused"]) is not bool or data["role"] not in ("planner", "frontdesk", "policy"):
            _bad()
        if data["role"] in ("frontdesk", "policy") and not data["paused"]:
            _bad()
        if guarded and (data["paused"] or data["role"] != "planner" or data["expected_owner"] not in
                        ("planner", "frontdesk", "reflex", "automatic_letter")):
            _bad()
    elif kind in ("ok", "committed", "undone"):
        names = {"ok": ("status", "preview"), "committed": ("status", "changeset_id", "load_epoch", "readback", "undo_token"), "undone": ("status", "changeset_id", "load_epoch", "readback")}[kind]
        _fields(data, names)
        if kind != "ok":
            _require_text(data["changeset_id"], 2**31)
            _epoch(data["load_epoch"])
        if kind == "committed":
            _require_text(data["undo_token"], 2**31)
        rows = data["preview" if kind == "ok" else "readback"]
        if not isinstance(rows, list):
            _bad()
        for row in rows:
            _fields(row, ("op", "object", "field", "expect", "value") if kind == "ok" else ("object", "field", "value"))
            _require_text(row["object"])
            _require_text(row["field"])
            if kind == "ok" and (type(row["op"]) is not int or row["op"] < 0):
                _bad()
    elif kind == "conflict":
        _fields(data, ("status", "op", "expected", "observed", "last_writer", "at_tick"))
        if type(data["op"]) is not int or data["op"] < 0 or (data["at_tick"] is not None and (type(data["at_tick"]) is not int or data["at_tick"] < 0)):
            _bad()
        if data["last_writer"] is not None:
            _require_text(data["last_writer"], 2**31)
    elif kind in ("unknown_token", "already_undone"):
        _fields(data, ("status",))
    elif kind == "stale_epoch":
        _fields(data, ("status", "load_epoch"))
        _epoch(data["load_epoch"])
    else:
        _bad()
