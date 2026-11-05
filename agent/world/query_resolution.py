"""Resolve exact display names and definitions to native ids before planning; never pick one silently."""

from __future__ import annotations

import copy

from .query_catalog import DEF_KINDS, PAWN_KINDS, SINGLE_ONLY_KINDS, THING_KINDS
from .query_rendering import coverage_of
from .query_surface import SurfaceError


def lookup_spec(surface, obj):
    """需要二次原生解析时返回 lookup 描述，否则 None。"""
    kind = obj["kind"]
    if "names" in obj:
        names = list(obj["names"])
        if kind in ("pawn", "pawns"):
            root, field, args = "pawns", "name", ({"map_id": obj["map"]} if "map" in obj else {})
        elif kind == "colonists":
            root, field, args = "spawned_free_colonists", "name", {"map_id": obj.get("map", "current")}
        elif kind in ("map", "maps"):
            root, field, args = "maps", "name", {}
        elif kind in ("thing", "things"):
            root, field = "things", "label"
            args = {
                "map_id": obj.get("map", "current"),
                "include_held": bool(obj.get("include_held", True)),
                "visible_only": not bool(obj.get("include_fogged", True)),
            }
        else:
            raise SurfaceError("kind " + kind + " has no display-name population to resolve")
        block = {"at": "", "fields": list(dict.fromkeys(["id", field] + (["map_id"] if kind in PAWN_KINDS else [])))}
        if args:
            block["args"] = args
        block["where"] = {field: {"in": names}}
        if obj.get("capabilities"):
            block.setdefault("args", {})["capabilities"] = list(obj["capabilities"])
        block["limit"] = 100
        return {
            "kind": "names",
            "field": field,
            "names": names,
            "match_fields": (field,),
            "case_insensitive": False,
            "request": surface.compile({"source": root, "blocks": [block]}),
        }
    if "definition" in obj and kind in DEF_KINDS + THING_KINDS:
        text = obj["definition"]
        block = {
            "at": "",
            "fields": ["def_name", "label"],
            "limit": 100,
            "where": {
                "any": [
                    {"def_name": {"eq": text, "case_sensitive": False}},
                    {"label": {"eq": text, "case_sensitive": False}},
                ]
            },
        }
        # Negotiated native candidate narrowing preserves the same union of
        # canonical names and localized labels, including ambiguity. Older
        # directories keep the existing complete-scan path.
        arguments = surface.argument_schemas.get("thing_defs", {}).get("properties", {})
        if "exact_names" in arguments:
            block["args"] = {"exact_names": [text]}
        return {
            "kind": "definition",
            "field": "def_name",
            "names": [text],
            "match_fields": ("def_name", "label"),
            "case_insensitive": True,
            "request": surface.compile({"source": "thing_defs", "blocks": [block]}),
        }
    return None


def interpret_lookup(lookup, packed):
    """把解析查询的结果翻成 resolution：绝不静默选一个。"""
    result = packed["results"][0]
    resolution = coverage_of(result)
    rows = result.get("rows") or []
    field = lookup["field"]
    match_fields = lookup.get("match_fields") or (field,)
    complete = (
        resolution["scan"] == "complete" and resolution["listing"] == "complete" and resolution["fields"] == "complete"
    )
    wanted = lookup["names"]
    folder = str.casefold if lookup.get("case_insensitive") else (lambda text: text)
    matches, missing, ambiguous = [], [], []

    def equal(row, name):
        return any(isinstance(row.get(key), str) and folder(row[key]) == folder(name) for key in match_fields)

    if not complete:
        rows = [dict(row) for row in rows if any(equal(row, name) for name in wanted)]
    else:
        for name in wanted:
            found = [row for row in rows if equal(row, name)]
            if not found:
                missing.append(name)
            elif len(found) > 1:
                ambiguous.append(name)
            matches.extend(found)
    if not complete:
        status = "incomplete"
    elif ambiguous:
        status = "ambiguous"
    elif missing:
        status = "missing_in_scope"
    else:
        status = "resolved"
    return {
        "status": status,
        "field": field,
        "requested": wanted,
        "source": result.get("from"),
        "scope": copy.deepcopy(result.get("scope", {})),
        "ids": [row.get("id") or row.get("def_name") for row in matches],
        "candidates": [_candidate(row) for row in rows],
        "ambiguous": ambiguous,
        "missing": missing,
        "coverage": resolution,
        "note": (
            "Exact display names and definitions are resolved against a complete scan; an incomplete "
            "scan or several exact matches never resolves to one object."
        ),
    }


def _candidate(row):
    return {k: row.get(k) for k in ("id", "name", "label", "def_name", "map_id") if row.get(k) is not None}


def apply_lookup(obj, lookup, resolution):
    """把解析结果写回 object。

    对**总体**（can read many）：歧义（同名多个）不是失败——题目的要求往往正是
    “逐个列出”；回全部候选不等同“擅自选一个”。
    对**单对象**（map/pawn/thing）：歧义时不能选其中一个（`route_object` 只会取
    ids[0]，结果会同时声称“多义”又以 ok 回第一个），必须交回候选。
    只有缺失/扫描不完整才同样拒绝。
    """
    if resolution["status"] not in ("resolved", "ambiguous"):
        return None
    resolved = dict(obj)
    resolved.pop("names", None)
    if lookup["field"] == "def_name":
        if resolution["status"] == "ambiguous":
            return None
        resolved["definition"] = resolution["ids"][0]
        return resolved
    if resolution["status"] == "ambiguous" and obj["kind"] in SINGLE_ONLY_KINDS:
        # 实测（v1.10）：名称解析已正确报 ambiguous 并回两个 id，`map` 路由却取第一个，
        # 以 status=ok 只回一个的事实。公开承诺是“同名返回候选，绝不擅自选一个”。
        # pawn/thing 有总体根，多候选逐个返回是**有意保留**的行为，不走这里。
        return None
    resolved["ids"] = list(resolution["ids"])
    return resolved
