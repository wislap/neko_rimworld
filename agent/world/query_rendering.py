"""Render native query payloads as plain facts, coverage, scope and semantics (no interning)."""

from __future__ import annotations

import copy

from .query_catalog import GROUP_PATHS, LISTING_FIELDS, PAWN_KINDS, SEMANTICS
from .query_validation import supported_aspects


def _measure_literal(value):
    if value.get("complete") is True and value.get("value") is not None:
        out = {"state": "exact", "value": value["value"]}
    elif value.get("complete") is True:
        out = {"state": "empty"}
    elif "known_value" in value:
        out = {"state": "lower_bound", "minimum": value.get("known_value")}
    else:
        out = {"state": "unavailable", "reason": "the scan was incomplete and no partial value was reported"}
    out["operation"] = value.get("op")
    out["unit"] = value.get("unit")
    if value.get("field") is not None:
        out["field"] = value["field"]
    return out


def _scope_literal(scope):
    out = {k: copy.deepcopy(v) for k, v in scope.items() if k not in ("description", "argument_semantics")}
    notes = []
    if scope.get("description"):
        notes.append(scope["description"])
    for key, text in (scope.get("argument_semantics") or {}).items():
        if text:
            notes.append(key + ": " + text)
    if notes:
        out["notes"] = notes
    return out


def _collection_literal(value):
    out = {"state": "complete" if value.get("scan_complete") is True else "partial"}
    if value.get("scan_complete") is not True:
        out["reason"] = "the source scan was incomplete"
    if "from" in value:
        out["from"] = value["from"]
    out["fields"] = "complete" if value.get("fields_complete") else "partial"
    out["matched"] = value.get("matched_count")
    out["known_matches"] = value.get("known_matches")
    page = value.get("page")
    if page is None:
        out["listing"] = "not_requested"
    else:
        out["listing"] = (
            "complete"
            if (value.get("scan_complete") is True and page.get("offset", 0) == 0 and page.get("has_more") is False)
            else "partial"
        )
        out["returned"] = page.get("returned")
        out["has_more"] = page.get("has_more")
        out["offset"] = page.get("offset")
    if value.get("aggregates"):
        out["aggregates"] = {k: _measure_literal(v) for k, v in value["aggregates"].items()}
    if value.get("scope") is not None:
        out["scope"] = _scope_literal(value["scope"])
    rows = value.get("rows")
    if rows is not None:
        out["rows"] = [_pack(row) for row in rows]
    return out


def _is_measure(value):
    return type(value.get("complete")) is bool and isinstance(value.get("op"), str) and {"value", "unit"} <= set(value)


def _is_collection(value):
    return type(value.get("scan_complete")) is bool and {"fields_complete", "matched_count", "known_matches"} <= set(
        value
    )


def _pack(value):
    if isinstance(value, list):
        return [_pack(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _is_measure(value):
        return _measure_literal(value)
    if _is_collection(value):
        return _collection_literal(value)
    return {k: _pack(v) for k, v in value.items() if not k.startswith("_")}


def pack(data, issues=()):
    """把原生 payload 变成明文事实；不生成任何 handle。"""
    packed = _pack(copy.deepcopy(data))
    for issue in issues or ():
        path = issue.get("path") or []
        if len(path) < 2 or path[0] != "results":
            continue
        _mark(packed, path, issue)
    return packed


def _mark(root, path, issue):
    """把未采集/不可用的槽位标成 unavailable。

    标量槽位只在确实为 null 时才替换；**对象子树同样要处理**：若整个子树都是
    null（例如整个 faction 未采集），必须把子树换成 unavailable 标记。
    留下裸 null 就是在制造"null 到底是什么意思"的歧义，而它正是
    “没读到却说成没有”那类错误的温床。
    """
    if not path:
        return
    current = root
    for part in path[:-1]:
        key = int(part) if isinstance(current, list) else part
        try:
            current = current[key]
        except (KeyError, IndexError, ValueError, TypeError):
            return
    last = path[-1]
    marker = {"state": "unavailable", "reason": issue.get("code"), "message": issue.get("message")}
    try:
        if isinstance(current, list):
            index = int(last)
            target = current[index] if 0 <= index < len(current) else KeyError
        elif isinstance(current, dict):
            target = current.get(last, KeyError)
        else:
            return
    except (ValueError, TypeError):
        return
    if target is KeyError:
        return
    if target is None:
        replacement = marker
    elif isinstance(target, dict) and target and all(value is None for value in target.values()):
        # 整个子树都没采集到：逐字段的裸 null 没有任何信息量，换成一次说明。
        replacement = marker
    elif isinstance(target, list) and target and all(value is None for value in target):
        replacement = marker
    else:
        return
    if isinstance(current, list):
        current[int(last)] = replacement
    else:
        current[last] = replacement


def coverage_of(result):
    """读取已 pack 的集合覆盖率明文（scan/fields/listing/matched）。"""
    return {
        "scan": result.get("state", "partial"),
        "fields": result.get("fields", "partial"),
        "listing": result.get("listing", "not_requested"),
        "matched": result.get("matched"),
        "known_matches": result.get("known_matches"),
    }


def _subtree(row, at):
    node = row
    if at:
        for segment in at.split("."):
            if not isinstance(node, dict):
                return None
            if _is_unavailable(node):
                # 中间层已经是 unavailable 标记时就此停下，不要把整个子树拆成 null。
                return node
            node = node.get(segment)
    return node


def _is_unavailable(value):
    return isinstance(value, dict) and value.get("state") == "unavailable"


def _merge(out, path, value):
    parts = path.split(".")
    node = out
    for index, part in enumerate(parts[:-1]):
        current = node.get(part)
        if _is_unavailable(current):
            # 子树已明确为不可用（如整个 faction 未采集）：保留标记，不再拆出裸 null。
            return out
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def _identity(row, fields):
    out = {}
    for name in fields:
        value = _subtree(row, name)
        if _is_unavailable(value) and "." in name:
            # 整个子树不可用（如 faction 未采集）：在顶层键上标一次，
            # 而不是拆成四个逐字段标记。
            out[name.split(".")[0]] = value
            continue
        _merge(out, name, value)
    return out


def _field_note(surface, root, at, name):
    kind = surface.path_type(root, at)
    note = None
    for segment in name.split("."):
        candidate = surface.notes.get(kind, {}).get(segment)
        if candidate:
            note = candidate
        kind = surface.child_type(kind, segment)
    return note


def _not_read(plan, surface, result, issues=()):
    """只列高信号缺口，不枚举整个原生字段空间。

    早期实现把根类型里每一个未覆盖的原生字段都倒出来，一次白银计数就产生 42 条
    路径字符串。那既淹没真正重要的缺口（未读到的方面、身份未知、扫描不完整），
    又把路径词汇重新灌回模型上下文。这里只保留会影响结论的项。
    """
    items = []
    root_type = surface.path_type(plan.root, "")
    # 收窄范围时必须把「被排除的东西」直接点名。实测中模型写了 include_held=false
    # （只算地图上生成的物），却把结果描述成“含被持有容器内容”——那就是范围被收窄而未被声明。
    if root_type == "Thing":
        if plan.object.get("include_held") is False:
            items.append(
                "things inside holders (inventory, containers, equipment) are EXCLUDED by include_held=false; "
                "the totals below are map-spawned objects only"
            )
        if plan.object.get("include_fogged") is False:
            items.append(
                "things under fog are EXCLUDED by include_fogged=false; the totals below are currently visible only"
            )
    # 未请求的 aspect 折叠成一行，不再每个占一行（那是噪音，不是信号）。
    missing = [a for a in supported_aspects(plan.kind) if a not in plan.aspects]
    if missing:
        items.append("aspects not requested: " + ", ".join(missing))
    # 身份现在属于“寻址”的一部分（见 _identity_fields），所以这里要查 **主语字段**
    # 而不是条目覆盖；否则会在已经读到 faction 时仍然声称身份未读。
    if root_type == "Pawn" and plan.kind != "colonists" and not any(f.startswith("faction.") for f in plan.subject):
        items.append(
            "faction/colonist-status (not read; the schema exposes no faction field; "
            "identity is never inferred from kind, name or container)"
        )
    for entry in plan.structure:
        if entry.get("limit") == 0 and entry["at"]:
            items.append(entry["at"] + " detail rows (count only; detail=0)")
    if plan.grouping:
        items.append(root_type + " per-instance rows (a grouped read returns group aggregates)")
    coverage = coverage_of(result)
    if coverage["scan"] == "partial":
        items.append(root_type + " population remainder (scan incomplete: no exact total)")
    if coverage["listing"] == "partial" and not plan.grouping:
        items.append(root_type + " detail rows beyond the returned page")
    if coverage["fields"] == "partial":
        items.append("selected fields that failed to read (see issues)")
    for issue in issues or ():
        path = issue.get("path") or []
        label = issue.get("code", "issue")
        items.append(
            ("results." + ".".join(str(p) for p in path[2:]) if len(path) > 2 else "result") + " (" + label + ")"
        )
    return sorted(dict.fromkeys(items))


def _semantics(plan, surface, result, resolution=None):
    notes = []
    kind = plan.kind
    if kind in PAWN_KINDS or kind in ("thing", "things"):
        notes.append(SEMANTICS["containment"])
    # 字段是点分路径（held_by.id），必须用子串判断而不是全等。
    if kind in ("thing", "things") and any(
        any(f.startswith("held_by") or f.startswith("faction") for f in (e.get("fields") or [])) for e in plan.structure
    ):
        notes.append(SEMANTICS["holder"])
    if kind in PAWN_KINDS:
        notes.append(SEMANTICS["pawn"])
    # 字段是点分路径（parent_holder.runtime_type），必须用子串判断而不是全等。
    if any(any(f.startswith("parent_holder") for f in (entry.get("fields") or [])) for entry in plan.structure):
        notes.append(SEMANTICS["parent_holder"])
    if any(
        any(f.startswith("hit_points") or f.startswith("max_hit_points") for f in (entry.get("fields") or []))
        for entry in plan.structure
    ):
        notes.append(SEMANTICS["durability"])
    notes.append(SEMANTICS["unknown"])
    coverage = coverage_of(result)
    states = _states(result)
    if coverage["scan"] == "partial" or "lower_bound" in states:
        notes.append(SEMANTICS["lower_bound"])
    if plan.metrics.get("units") or "quantity" in plan.aspects:
        notes.append(SEMANTICS["units"])
    if resolution is not None:
        notes.append(resolution["note"])
    for entry in plan.structure:
        for name in entry.get("fields") or []:
            note = _field_note(surface, plan.root, entry["at"], name)
            if note:
                notes.append(name.split(".")[-1] + ": " + note)
    return list(dict.fromkeys(notes))


def _states(value, found=None):
    found = set() if found is None else found
    if isinstance(value, list):
        for item in value:
            _states(item, found)
    elif isinstance(value, dict):
        if value.get("state") in ("lower_bound", "unavailable", "empty", "partial") and "operation" in value:
            found.add(value["state"])
        for item in value.values():
            _states(item, found)
    return found


def _facts(plan, result):
    if plan.root_limit == 0:
        return []
    rows = result.get("rows") or []
    subject = list(plan.subject)
    root_entries = [
        item for item in plan.structure if not item["at"] and item.get("fields") and not item.get("listing")
    ]
    facts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = {"object": _identity(row, subject), "facts": {}}
        # 根级别条目必须同样渲染：只请求 identity 时全部字段都在根上，
        # 早期实现只处理 at != '' 的条目，导致这类 look 返回空事实。
        for item in root_entries:
            entry["facts"].setdefault(item["aspect"], {})[item["key"]] = _identity(row, item["fields"])
        for item in plan.structure:
            if item["at"]:
                entry["facts"].setdefault(item["aspect"], {})[item["key"]] = _subtree(row, item["at"])
        facts.append(entry)
    return facts


def _scope(plan, surface, result):
    raw = result.get("scope") or {}
    args = raw.get("args") or {}
    resolved = {}
    if "map_id" in args:
        resolved["map_id"] = args["map_id"]
    if "id" in args:
        resolved["id"] = args["id"]
    if "def_name" in args:
        resolved["definition"] = args["def_name"]
    if "def_names" in args:
        resolved["definitions"] = list(args["def_names"])
    if "ids" in args:
        resolved["ids"] = list(args["ids"])
    # 回显模型侧的同一套名字与极性，另附原生参数以便审计；不让两种极性同时出现在结果里。
    if "include_held" in args:
        resolved["include_held"] = args["include_held"]
    if "visible_only" in args:
        resolved["include_fogged"] = not args["visible_only"]
        resolved["native_visible_only"] = args["visible_only"]
    # 打包后的结果只带 `notes`（_scope_literal 已经把 description 与
    # argument_semantics 合并进去并删掉旧键）。旧实现只读被删掉的旧键，
    # 于是把原生范围/参数说明静默丢弃——实测：30 次工具结果、覆盖 12/12
    # 场候选咨询的 scope.notes 变成空列表。两种形状都要认。
    notes = [note for note in (raw.get("notes") or []) if note]
    if raw.get("description"):
        notes.append(raw["description"])
    for text in (raw.get("argument_semantics") or {}).values():
        if text:
            notes.append(text)
    notes = list(dict.fromkeys(str(note) for note in notes))
    return {
        "object": {k: v for k, v in plan.object.items() if v is not None},
        "resolved": resolved,
        "native_root": plan.root,
        "native_type": surface.path_type(plan.root, ""),
        "notes": notes,
    }


def _units_or_zero(measure):
    """An exact measure's value; an empty complete measure (no matched stacks) is zero."""
    if isinstance(measure, (int, float)):
        return measure
    if measure.get("state") == "exact":
        return measure["value"]
    if measure.get("state") == "empty":
        return 0
    return None


def _forbidden_availability(totals, forbidden):
    """Split the counted totals by the native forbidden flag of the same selection."""
    result = forbidden["data"]["results"][0]
    parts = result.get("aggregates") or {}
    split = {}
    for name, total in totals.items():
        part = parts.get(name, {})
        whole, forbidden_value = _units_or_zero(total), _units_or_zero(part)
        unit = total.get("unit") if isinstance(total, dict) else None
        if whole is None or forbidden_value is None:
            not_forbidden = {"state": "unavailable", "reason": "total or forbidden measure is not exact"}
        else:
            not_forbidden = {"state": "exact", "value": whole - forbidden_value, "operation": "total_minus_forbidden",
                             "unit": unit}
        split[name] = {"forbidden": copy.deepcopy(part), "not_forbidden": not_forbidden}
    return {
        "forbidden_flag": split,
        "basis": "Native CompForbiddable.Forbidden of the same selection. Not forbidden does not establish "
                 "reachability, allowed area, food policy or reservation.",
        "forbidden_read": {"observation_id": forbidden.get("observation_id"), "status": forbidden["status"],
                           "coverage": coverage_of(result)},
    }


def render(plan, native, resolution=None, forbidden=None):
    """把一次原生读数渲染成明文结果。

    forbidden is the same selection's forbidden-only native read, when one was made.
    """
    if plan.blocked is not None:
        return plan.blocked
    surface = plan.surface
    result = native["data"]["results"][0]
    sample = native["data"].get("sample") or {}
    out = {
        "status": native["status"],
        "tool": plan.tool,
        "object": {k: v for k, v in plan.object.items() if v is not None},
        "scope": _scope(plan, surface, result),
        "sample": {
            "load_epoch": sample.get("load_epoch"),
            "game_tick": sample.get("game_tick"),
            "sequence": sample.get("sequence"),
        },
        "coverage": coverage_of(result),
        "not_read": _not_read(plan, surface, result, native.get("issues")),
        "semantics": _semantics(plan, surface, result, resolution),
        "issues": [
            {"reason": issue.get("code"), "message": issue.get("message"), "path": list(issue.get("path") or [])}
            for issue in native.get("issues") or ()
        ],
        "observation_id": native.get("observation_id"),
    }
    if isinstance(resolution, dict) and "name_resolution" in resolution:
        # Deterministic Def-name recovery is disclosed on the delivered result:
        # the author and review see the requested term and the canonical term.
        out["name_resolution"] = copy.deepcopy(resolution["name_resolution"])
    if plan.metrics:
        if surface.path_type(plan.root, "") == "Thing":
            # Keep the meaning next to the numbers, once per returned set.
            # Physical collection membership is not ownership or accessibility;
            # this adds no new qualification read and does not alter defaults.
            resolved = out["scope"]["resolved"]
            out["quantity_basis"] = {
                "population": "physical_objects",
                "include_held": resolved.get("include_held", plan.object.get("include_held", True)),
                "include_fogged": resolved.get("include_fogged", plan.object.get("include_fogged", True)),
                "availability": ("not_established_by_this_read" if forbidden is None
                                 else _forbidden_availability(result.get("aggregates") or {}, forbidden)),
            }
        out["measures"] = dict(result.get("aggregates") or {})
    if plan.grouping:
        by = plan.grouping[0]
        native = GROUP_PATHS[plan.kind][by]
        out["groups"] = [
            {"key": {by: (row.get("key") or {}).get(native)}, "measures": dict(row.get("aggregates") or {})}
            for row in (result.get("rows") or [])
            if isinstance(row, dict)
        ]
    else:
        out["facts"] = _facts(plan, result)
        # detail>0 时明细也是根行；单独回一份，不混进 aspects 事实。
        if any(item.get("listing") for item in plan.structure):
            out["listing"] = _listing_rows(plan, result)
    if resolution is not None:
        out["resolution"] = resolution
    return out


def _listing_rows(plan, result):
    rows = [row for row in (result.get("rows") or []) if isinstance(row, dict)]
    fields = list(LISTING_FIELDS.get(plan.kind) or ())
    return [_identity(row, fields) for row in rows]


def render_blocked(tool, obj, resolution, native):
    """歧义/缺失/不完整时只回候选，绝不回事实。"""
    result = (native["data"]["results"] or [{}])[0]
    return {
        "status": "unavailable",
        "tool": tool,
        "object": {k: v for k, v in obj.items() if v is not None},
        "resolution": resolution,
        "lookup": {
            "source": result.get("from"),
            "scope": copy.deepcopy(result.get("scope", {})),
            "coverage": coverage_of(result),
        },
        "coverage": {
            "scan": "not_run",
            "fields": "not_run",
            "listing": "not_run",
            "matched": None,
            "known_matches": None,
        },
        "execution": "not_run",
        "explanation": (
            "Definition lookup did not resolve the requested definition. Instance counting/reading was not executed; "
            "zero matching definitions does not establish absence of an instance ID or display name."
            if resolution.get("field") == "def_name"
            else "Object lookup did not resolve the requested identity. The requested reading/counting was not executed."
        ),
        "measures": {},
        "facts": [],
        "not_read": ["object facts (the object was not resolved to exactly one match)"],
        "semantics": [
            SEMANTICS["unknown"],
            "A display name or definition that is ambiguous, missing or only partially scanned is "
            "never resolved to a single object.",
        ],
        "issues": [
            {"reason": issue.get("code"), "message": issue.get("message"), "path": list(issue.get("path") or [])}
            for issue in native.get("issues") or ()
        ],
        "observation_id": native.get("observation_id"),
    }
