"""Plan current public-query reads for the look/count owners: native routing and request blocks.

Only query-backed identity, mood/profile, map resources and population measures
are planned here. Native Pawn health/skills/gear/work and physical state belong
to native_state. Public tools own their final schemas outside this planner.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .query_catalog import (
    DEF_KINDS,
    DEFAULT_GROUP,
    FACTION_FIELDS,
    GROUP_PATHS,
    HOLDER_FIELDS,
    IDENTITY_FIELDS,
    LISTING_FIELDS,
    MAP_KINDS,
    PAUSE_WINDOW_FIELDS,
    PAWN_KINDS,
    POPULATIONS,
    ROOTS,
    SUBJECT_FIELDS,
)
from .query_surface import SurfaceError
from .query_validation import supports_pause_windows

# --- 原生路由 -----------------------------------------------------------------


def route_object(obj):
    """kind + 精确选择器 → (native root, args, where)。"""
    kind = obj["kind"]
    args, where = {}, None
    if kind in ("game", "colony"):
        return ROOTS[kind], args, where
    if kind == "maps":
        if "ids" in obj:
            args["ids"] = list(obj["ids"])
        return ROOTS[kind], args, where
    if kind == "map":
        args["id"] = obj["ids"][0] if "ids" in obj else obj.get("map", "current")
        return ROOTS[kind], args, where
    if kind in ("pawn", "pawns"):
        if kind == "pawn" and "ids" in obj and len(obj["ids"]) == 1 and "map" not in obj:
            args["id"] = obj["ids"][0]
            return "pawn", args, where
        # pawns 覆盖人类/动物/机械族；pawn 多 id 时也落到同一根，避免伪造单 id。
        args["map_id"] = obj.get("map", "current")
        if "ids" in obj:
            where = {"id": {"in": list(obj["ids"])}}
        return "pawns", args, where
    if kind == "colonists":
        if "ids" in obj:
            args["ids"] = list(obj["ids"])
        args["map_id"] = obj.get("map", "current")
        return ROOTS[kind], args, where
    if kind in ("thing", "things"):
        if kind == "thing" and "ids" in obj and len(obj["ids"]) == 1 and "names" not in obj:
            args["id"] = obj["ids"][0]
            if "map" in obj:
                args["map_id"] = obj["map"]
            return "thing", args, where
        args["map_id"] = obj.get("map", "current")
        args["include_held"] = bool(obj.get("include_held", True))
        # 原生参数是反向的 visible_only（true = 排除迷雾），由程序翻转，模型不必知道。
        args["visible_only"] = not bool(obj.get("include_fogged", True))
        if "ids" in obj:
            where = {"id": {"in": list(obj["ids"])}}
        elif "definition" in obj:
            # 解析后的精确定义名走 def_names，避免子串匹配。
            args["def_names"] = [obj["definition"]]
        elif "category" in obj:
            where = {"definition.category": {"eq": obj["category"]}}
        if "capabilities" in obj:
            args["capabilities"] = list(obj["capabilities"])
        return "things", args, where
    if kind == "thing_def":
        args["def_name"] = obj["definition"]
        return "thing_def", args, where
    if kind == "thing_defs":
        if "category" in obj:
            args["category_def_name"] = obj["category"]
        if "definition" in obj:
            where = {"def_name": {"eq": obj["definition"]}}
        return "thing_defs", args, where
    raise SurfaceError("unsupported kind: " + kind)


# --- aspects → blocks ---------------------------------------------------------


def _entry(aspect, at, key, fields=None, limit=None, listing=False):
    value = {"aspect": aspect, "at": at, "key": key}
    if fields is not None:
        value["fields"] = list(fields)
    if limit is not None:
        value["limit"] = limit
    if listing:
        value["listing"] = True
    return value


def _pawn_entries(surface, aspect, detail):
    if aspect == "identity":
        return [_entry("identity", "", "object", _supported(surface, "pawn", IDENTITY_FIELDS["pawn"]))]
    if aspect == "mood":
        return [
            _entry("mood", "mood", "mood", ["level", "instant"]),
            _entry("mood", "mood.break_thresholds", "break_thresholds", ["minor", "major", "extreme"]),
            _entry(
                "mood",
                "mood.thought_groups",
                "thought_groups",
                ["def_name", "label", "stage_index", "mood_offset", "member_count"],
                detail,
            ),
            _entry(
                "mood", "mood.memories", "memories", ["ref", "def_name", "label", "mood_offset", "age_ticks"], detail
            ),
        ]
    if aspect == "profile":
        return [
            _entry("profile", "profile", "profile", ["gender", "race_def_name", "kind_def_name"]),
            _entry("profile", "profile.age", "age", ["biological_years", "chronological_years", "life_stage"]),
            _entry("profile", "profile.backstories", "backstories", ["slot", "def_name", "title"], detail),
            _entry(
                "profile",
                "profile.traits",
                "traits",
                ["def_name", "label", "degree", "suppressed"]
                + (["description"] if _supported(surface, "pawn", ["profile.traits.description"]) else []),
                detail,
            ),
        ]
    raise SurfaceError("unsupported aspect for this kind: " + aspect)


def _resource_entries(kind, detail):
    return [
        _entry(
            "resources",
            "resource_counter.all_counted_amounts",
            "all_counted_amounts",
            ["key.def_name", "key.label", "value"],
            detail,
        ),
        _entry("resources", "resource_counter", "resource_counter", ["total_human_edible_nutrition"]),
    ]


def _supported(surface, kind, fields):
    """只保留当前 schema 真实存在的字段路径。

    插件与 Mod 版本可能不一致（例如旧 Mod 没有派系字段）。硬写字段名会直接
    报 Unknown field；正确行为是改用可用的那部分，并把缺失的部分交给 not_read 点名。
    """
    root = ROOTS[kind]
    kept = []
    for name in fields:
        try:
            surface.path_type(root, name)
        except (SurfaceError, KeyError, TypeError):
            continue
        kept.append(name)
    return kept


def _entries_for(surface, kind, aspect, detail):
    if aspect == "pause_windows":
        if kind != "game" or not supports_pause_windows(surface):
            raise SurfaceError("Current pause window details are unavailable on this connection")
        return [_entry(aspect, "pause_windows", "pause_windows",
                       [path.removeprefix("pause_windows.") for path in PAUSE_WINDOW_FIELDS])]
    if aspect in ("quantity", "distribution"):
        return [_entry(aspect, "", aspect, LISTING_FIELDS.get(kind), listing=True)]
    if kind in PAWN_KINDS:
        return _pawn_entries(surface, aspect, detail)
    if aspect == "identity":
        if kind == "colony":
            return [
                _entry("identity", "game", "game", ["paused", "current_map_id"]),
                _entry(
                    "identity",
                    "maps",
                    "maps",
                    _supported(surface, "maps", ["id", "name", "is_current", "colonist_count"]),
                    detail,
                ),
            ]
        return [_entry("identity", "", "object", _supported(surface, kind, IDENTITY_FIELDS[kind]))]
    if aspect == "resources" and kind in MAP_KINDS:
        return _resource_entries(kind, detail)
    raise SurfaceError("unsupported aspect for kind " + kind + ": " + aspect)


def _identity_fields(surface, obj, entries):
    """实际会被选中的主语字段：SUBJECT_FIELDS 加上根级别 identity 条目，

    并且只保留当前 schema 真实存在的。未选中的字段不渲染，而不是渲染成 null。

    另外补上“寻址就该带上的身份事实”：对 Pawn 是无条件带上 faction；
    对单个 Thing 是带上持有者身份。原因是实测的失败场景——
    “看某人的装备 → 声称这是我们的物资”，“看一件东西 → 声称它属于我们”——
    模型读 gear 时拿不到 faction、读单件时拿不到 held_by，于是只能靠推。
    把这两项归到**寻址**而不是某个 aspect，才能让后面的事实有依据。
    """
    fields = list(SUBJECT_FIELDS.get(obj["kind"], ()))
    if obj["kind"] in PAWN_KINDS:
        fields += [f for f in FACTION_FIELDS if f not in fields]
    if obj["kind"] == "thing":
        fields += [f for f in HOLDER_FIELDS if f not in fields]
    for entry in entries:
        if not entry["at"] and entry.get("fields") and not entry.get("listing"):
            fields += [f for f in entry["fields"] if f not in fields]
    return tuple(_supported(surface, obj["kind"], fields))


def _covers(entries):
    covered = set()
    for entry in entries:
        if entry["at"]:
            covered.add(entry["at"])
        if entry.get("fields"):
            for name in entry["fields"]:
                covered.add(".".join(filter(None, [entry["at"], name])))
    return covered


# --- plan ---------------------------------------------------------------------


@dataclass
class Plan:
    tool: str
    kind: str
    root: str
    object: dict
    aspects: tuple
    detail: int
    structure: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    grouping: tuple = ()
    resolution: dict | None = None
    lookup: dict | None = None
    request: dict | None = None
    args: dict = field(default_factory=dict)
    where: dict | None = None
    root_limit: int = 0
    covers: set = field(default_factory=set)
    subject: tuple = ()
    blocked: dict | None = None
    surface: object = None


def _root_limit(kind, detail, grouping, needs_rows):
    if grouping:
        return detail if detail > 0 else 100
    if kind in POPULATIONS:
        if not needs_rows:
            return 0
        # detail 未指定时用默认根行上限；detail 已给出则尊重它。
        return detail if detail > 0 else DEFAULT_ROOT_ROWS
    return max(detail, 1) if needs_rows else 0


DEFAULT_ROOT_ROWS = 20


def plan_look(surface, obj, aspects, detail):
    """构造 look 的原生请求；未知组合已在 normalize/validate 阶段拒绝。"""
    entries = []
    for aspect in aspects:
        entries.extend(_entries_for(surface, obj["kind"], aspect, detail))
    metrics, grouping = {}, ()
    if "quantity" in aspects or "distribution" in aspects:
        metrics = _measures_for(obj["kind"])
    if "distribution" in aspects:
        grouping = DEFAULT_GROUP[obj["kind"]]
    # look 是"看这些东西"，不是"数这些东西"：只要请求里有任何非计量 aspect，
    # 就必须返回被寻址对象本身。否则模型问"殖民者是谁"只会得到"有 3 个"。
    # detail 只控制 mood/profile 等 query 关系的展开深度，
    # 不应该成为"能不能看到主语"的隐藏开关。
    rows_wanted = any(aspect not in ("quantity", "distribution") for aspect in aspects)
    needs_rows = (
        (obj["kind"] not in POPULATIONS)
        or detail > 0
        or rows_wanted
        # 按定义寻址就是"读这个定义"，默认只回计数会让"定义存在吗"无法回答。
        or (obj["kind"] in DEF_KINDS and "definition" in obj)
    )
    return _assemble(surface, "look", obj, aspects, detail, entries, metrics, grouping, needs_rows)


def plan_count(surface, obj, metric, by, detail):
    aspects = ("quantity",) if by is None else ("distribution",)
    entries = [_entry("quantity", "", "quantity", LISTING_FIELDS.get(obj["kind"]), listing=True)]
    metrics = _measures_for(obj["kind"], metric)
    grouping = (by,) if by else ()
    # Group output is selected independently by _root_limit. An ungrouped
    # count must not read instance fields unless the caller requested detail.
    return _assemble(surface, "count", obj, aspects, detail, entries, metrics, grouping, detail > 0)


# Native CompForbiddable.Forbidden only; things without the component are never forbidden.
FORBIDDEN_THINGS = {"all_comps": {"some": {"on": {"CompForbiddable": {"forbidden": {"eq": True}}}}}}


def plan_forbidden_count(surface, obj, metric):
    """Measure the same Thing selection restricted to forbidden stacks."""
    entries = [_entry("quantity", "", "quantity", LISTING_FIELDS.get(obj["kind"]), listing=True)]
    return _assemble(surface, "count", obj, ("quantity",), 0, entries, _measures_for(obj["kind"], metric), (), False,
                     extra_where=FORBIDDEN_THINGS)


def _measures_for(kind, metric="units"):
    measures = {"records": {"op": "count"}}
    if kind == "things" and metric == "units":
        measures["units"] = {"op": "sum", "field": "stack_count"}
    return measures


def _compose_blocks(blocks):
    """Coalesce compatible aspect selections before the strict query compiler.

    A dotted identity field and a facet's child block can select the same leaf
    (profile.gender). Canonicalize their parent path, not the public compiler's
    overlap rules. Different options and default/explicit selections still fail.
    """
    merged = {}

    def at(path):
        return merged.setdefault(path, {"at": path})

    for block in blocks:
        path = block["at"]
        target = at(path)
        for key, value in block.items():
            if key in ("at", "fields"):
                continue
            if key in target and target[key] != value:
                raise SurfaceError("Conflicting aspect option: " + path + "." + key)
            target[key] = copy.deepcopy(value)
        fields = block.get("fields", [])
        if fields is True:
            if "fields" in target and target["fields"] is not True:
                raise SurfaceError("Defaults cannot be merged with explicit children")
            target["fields"] = True
            continue
        for name in fields:
            parent, dot, leaf = name.rpartition(".")
            field_path = ".".join(filter(None, [path, parent])) if dot else path
            selected = at(field_path).setdefault("fields", [])
            if selected is True:
                raise SurfaceError("Defaults cannot be merged with explicit children")
            if leaf not in selected:
                selected.append(leaf)
    return list(merged.values())


def _assemble(surface, tool, obj, aspects, detail, entries, metrics, grouping, needs_rows, extra_where=None):
    root, args, where = route_object(obj)
    if extra_where is not None:
        where = extra_where if where is None else {"all": [where, extra_where]}
    plan = Plan(
        tool=tool,
        kind=obj["kind"],
        root=root,
        object=dict(obj),
        aspects=tuple(aspects),
        detail=detail,
        structure=entries,
        metrics=metrics,
        grouping=grouping,
        args=args,
        where=where,
        surface=surface,
    )
    root_limit = _root_limit(obj["kind"], detail, grouping, needs_rows)
    if needs_rows and obj.get("ids"):
        # 一次寻址到 N 个对象（含同名解析出的多个）时必须都能返回，
        # 否则明细/事实会静静丢掉其余候选。
        root_limit = max(root_limit, len(obj["ids"]))
    blocks = []
    root_block = {"at": ""}
    if args:
        root_block["args"] = copy.deepcopy(args)
    if where:
        root_block["where"] = copy.deepcopy(where)
    fields = [e for e in entries if e["at"] == "" and e.get("fields") and not e.get("listing")]
    listing = [e for e in entries if e.get("fields") and e.get("listing")]
    selected = [f for e in fields for f in e["fields"]]
    # 主语只算一次：同一个列表既用于**请求 select** 也用于**渲染**。
    # 两者分开算过一次错：渲染读了没被请求的字段，于是全是 null（“读了一个人的健康却不知
    # 道是谁”就是这类 bug）。这里把 subject 提前到请求构造之前，从结构上消掉分歧。
    plan.subject = _identity_fields(surface, obj, entries) if root_limit > 0 and not grouping else ()
    # 分组读取的行是**组行**（组键 + 度量），没有逐实例主语；把 id 之类加进
    # select 会让组内取值冲突，引擎正确地报 group_field_varies 并把整份读取标成
    # partial，而输出根本不用这些值（实测 v1.10：该题 21 条无谓 issue）。
    selected += [f for f in plan.subject if f not in selected]
    if listing and not grouping and root_limit > 0:
        selected += [f for e in listing for f in e["fields"]]
    if selected:
        root_block["fields"] = list(dict.fromkeys(selected))
    if metrics:
        root_block["aggregate"] = copy.deepcopy(metrics)
    if grouping:
        root_block["group_by"] = [GROUP_PATHS[obj["kind"]][g] for g in grouping]
    root_block["limit"] = root_limit
    blocks.append(root_block)
    for entry in entries:
        if not entry["at"]:
            continue
        block = {"at": entry["at"]}
        if entry.get("fields"):
            block["fields"] = list(entry["fields"])
        if entry.get("limit") is not None:
            block["limit"] = entry["limit"]
        blocks.append(block)
    plan.covers = _covers(entries)
    plan.request = surface.compile({"source": root, "blocks": _compose_blocks(blocks)})
    plan.root_limit = root_block["limit"]
    return plan
