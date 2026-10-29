"""Validation of the Mod-owned colony_write.v1 wire contract."""

import re


def _bad():
    raise ValueError("Invalid write payload.")


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        _bad()


def _text(value, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _bad()


def _epoch(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        _bad()


def validate_request(mode, args):
    if mode in ("dry_run", "commit"):
        _fields(args, ("changeset_id", "author", "entry_id", "load_epoch", "idempotency_key", "ops"))
        for name in ("changeset_id", "author", "entry_id", "load_epoch", "idempotency_key"):
            _text(args[name])
        if args["author"] != "planner":
            _bad()
        _epoch(args["load_epoch"])
        ops = args["ops"]
        if not isinstance(ops, list) or not 1 <= len(ops) <= 64:
            _bad()
        for op in ops:
            if not isinstance(op, dict) or len(op) > 16 or not {"op", "evidence", "expect", "value"} <= op.keys():
                _bad()
            _text(op["op"])
            _text(op["evidence"])
            # The Mod's JObject null check treats JSON null as an existing token.
            targets = {k: v for k, v in op.items() if k not in {"op", "evidence", "expect", "value"}}
            if not targets.keys() & {"pawn", "bill", "thing", "worktable", "storage", "research", "map"}:
                _bad()
            if any(not isinstance(v, str) or not v.strip() for v in targets.values()):
                _bad()
            expected, value = op["expect"], op["value"]
            if op["op"] == "add_production_bill":
                _fields(op, ("op", "worktable", "value", "expect", "evidence"))
                _text(op['worktable'])
                _fields(expected, ("bill_ids",))
                _fields(value, ("recipe",))
                _text(value["recipe"])
                identifiers = expected["bill_ids"]
                if not isinstance(identifiers, list):
                    _bad()
                for identifier in identifiers:
                    _text(identifier)
                if len(set(identifiers)) != len(identifiers):
                    _bad()
            elif isinstance(expected, dict) or isinstance(value, dict):
                if not isinstance(expected, dict) or not isinstance(value, dict) or not expected or expected.keys() != value.keys():
                    _bad()
    elif mode == "undo":
        _fields(args, ("token",))
        _text(args["token"])
    elif mode == "set_paused":
        _fields(args, ("paused", "role"))
        if type(args["paused"]) is not bool or args["role"] not in ("planner", "frontdesk"):
            _bad()
        if args["role"] == "frontdesk" and not args["paused"]:
            _bad()
    else:
        _bad()


def validate_result(raw):
    status = raw.get("status")
    if status not in ("ok", "failed", "stale_session", "unsupported", "busy") or raw.get("issues") != []:
        _bad()
    if status != "ok":
        _fields(raw, ("status", "error_code", "message", "issues"))
        _text(raw["error_code"], 128)
        _text(raw["message"], 2048)
        return
    _fields(raw, ("status", "data", "issues"))
    data = raw["data"]
    if not isinstance(data, dict):
        _bad()
    kind = data.get("status")
    if kind is None and "status" not in data:
        _fields(data, ("paused", "role"))
        if type(data["paused"]) is not bool or data["role"] not in ("planner", "frontdesk"):
            _bad()
    elif kind in ("ok", "committed", "undone"):
        names = {"ok": ("status", "preview"), "committed": ("status", "changeset_id", "load_epoch", "readback", "undo_token"), "undone": ("status", "changeset_id", "load_epoch", "readback")}[kind]
        _fields(data, names)
        if kind != "ok":
            _text(data["changeset_id"], 2**31)
            _epoch(data["load_epoch"])
        if kind == "committed":
            _text(data["undo_token"], 2**31)
        rows = data["preview" if kind == "ok" else "readback"]
        if not isinstance(rows, list):
            _bad()
        for row in rows:
            _fields(row, ("op", "object", "field", "expect", "value") if kind == "ok" else ("object", "field", "value"))
            _text(row["object"])
            _text(row["field"])
            if kind == "ok" and (type(row["op"]) is not int or row["op"] < 0):
                _bad()
    elif kind == "conflict":
        _fields(data, ("status", "op", "expected", "observed", "last_writer", "at_tick"))
        if type(data["op"]) is not int or data["op"] < 0 or (data["at_tick"] is not None and (type(data["at_tick"]) is not int or data["at_tick"] < 0)):
            _bad()
        if data["last_writer"] is not None:
            _text(data["last_writer"], 2**31)
    elif kind in ("unknown_token", "already_undone"):
        _fields(data, ("status",))
    elif kind == "stale_epoch":
        _fields(data, ("status", "load_epoch"))
        _epoch(data["load_epoch"])
    else:
        _bad()
