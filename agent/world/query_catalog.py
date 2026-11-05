"""Closed look/count vocabulary: kinds, selectors, native field packs, meanings and the object schema."""

from __future__ import annotations

import json

# --- 封闭枚举 -----------------------------------------------------------------

ROOTS = {
    "game": "game",
    "maps": "maps",
    "map": "map",
    "colony": "colony",
    "pawn": "pawn",
    "pawns": "pawns",
    "colonists": "spawned_free_colonists",
    "thing": "thing",
    "things": "things",
    "thing_def": "thing_def",
    "thing_defs": "thing_defs",
}
KINDS = tuple(ROOTS)
ASPECTS = ("identity", "health", "skills", "gear", "work", "mood", "profile", "resources", "quantity", "distribution")
METRICS = ("units", "records")
GROUPINGS = ("definition", "category", "holder", "map")

# object 是两工具共用的封闭结构；每个 kind 只接受其适用的选择器。
OBJECT_SELECTORS = {
    "game": (),
    "maps": ("ids", "names"),
    "map": ("ids", "names", "map"),
    "colony": (),
    "pawn": ("ids", "names", "map"),
    "pawns": ("ids", "names", "map"),
    "colonists": ("ids", "names", "map"),
    "thing": ("ids", "names", "map"),
    "things": ("ids", "names", "definition", "category", "map", "include_held", "include_fogged", "capabilities"),
    "thing_def": ("definition", "include_held", "include_fogged"),
    "thing_defs": ("definition", "category", "include_held", "include_fogged"),
}
# 主语寻址：不管模型要哪个 aspect，事实都必须能说清楚"这是谁/这是哪个"。
# 少了这一层，就会出现「数字读对了、但没说清主语」的典型失败。
SUBJECT_FIELDS = {
    "game": (),
    "colony": (),
    "maps": ("id", "name"),
    "map": ("id", "name"),
    "pawn": ("id", "name", "map_id"),
    "pawns": ("id", "name", "map_id"),
    "colonists": ("id", "name", "map_id"),
    "thing": ("id", "definition.def_name", "definition.label", "map_held.id"),
    "things": ("id", "definition.def_name", "definition.label", "map_held.id"),
    "thing_def": ("def_name", "label"),
    "thing_defs": ("def_name", "label"),
}

POPULATIONS = ("maps", "pawns", "colonists", "things", "thing_defs")
PAWN_KINDS = ("pawn", "pawns", "colonists")
THING_KINDS = ("thing", "things")
DEF_KINDS = ("thing_def", "thing_defs")
MAP_KINDS = ("map", "maps")
# 多候选会落入一个**总体**根并逐个返回的 kind：pawn → pawns，thing → things。
# 只有 `map` 没有总体根，路由会直接取 ids[0]，所以它必须由歧义守卫拦住。
SINGLE_ONLY_KINDS = ("map",)

# 程序侧字段包：模型永远看不到这些原生路径。
# 身份事实：原生派系成员关系与殖民者状态。这是"这是谁的人"的直接答案，
# 不从种类、名字或容器关系推断。
FACTION_FIELDS = ["faction.def_name", "faction.name", "faction.is_player", "faction.player_relation"]
# 物品归属链：谁持有它、那个持有者的派系。沿真实持有关系合成，不是所有权推断。
HOLDER_FIELDS = [
    "held_by.id",
    "held_by.runtime_type",
    "held_by_faction.def_name",
    "held_by_faction.is_player",
    "held_by_faction.player_relation",
]
PAWN_IDENTITY_FIELDS = [
    "status.position.map_id",
    "status.position.x",
    "status.position.z",
    "is_free_colonist",
    "is_colonist",
    "is_prisoner_of_colony",
    "is_slave_of_colony",
    "guest_status",
    "profile.gender",
]
IDENTITY_FIELDS = {
    "game": ["paused", "current_map_id"],
    "maps": ["id", "name", "is_current", "is_player_home", "colonist_count", "has_visible_threat"],
    "map": ["id", "name", "is_current", "is_player_home", "colonist_count", "has_visible_threat"],
    "pawn": ["id", "name", "map_id"] + PAWN_IDENTITY_FIELDS + FACTION_FIELDS,
    "pawns": ["id", "name", "map_id"] + PAWN_IDENTITY_FIELDS + FACTION_FIELDS,
    "colonists": ["id", "name", "map_id"] + PAWN_IDENTITY_FIELDS + FACTION_FIELDS,
    "thing": [
        "id",
        "runtime_type",
        "label",
        "spawned",
        "destroyed",
        "definition.def_name",
        "definition.label",
        "map_held.id",
        "position.x",
        "position.z",
    ]
    + FACTION_FIELDS
    + HOLDER_FIELDS,
    "things": [
        "id",
        "runtime_type",
        "label",
        "spawned",
        "destroyed",
        "definition.def_name",
        "definition.label",
        "map_held.id",
        "position.x",
        "position.z",
    ]
    + FACTION_FIELDS
    + HOLDER_FIELDS,
    "thing_def": ["def_name", "label", "thing_class", "category"],
    "thing_defs": ["def_name", "label", "thing_class", "category"],
}
# count/look 明细行的最小身份列（仅为把读数对齐到对象）。
LISTING_FIELDS = {
    "game": ["paused"],
    "maps": ["id", "name"],
    "map": ["id", "name"],
    "colonists": ["id", "name", "map_id"],
    # v1.10 capability repair: native item durability belongs to generic detail,
    # not a task-specific extra read. Null/unavailable still use the shared pack.
    # 实测（v1.10）：只给不透明的 `parent_holder.ref`，模型把“共享同一个父引用”读成
    # “在同一个容器里”，而真相是两堆白银都由**地图**持有（spawned）。父对象的原生类型
    # 才能区分 地图 / 人物库存 / 容器 / 建筑；`spawned` 直接回答“在不在图上”。
    "things": [
        "id",
        "spawned",
        "stack_count",
        "hit_points",
        "max_hit_points",
        "definition.def_name",
        "definition.label",
        "map_held.id",
        "parent_holder.runtime_type",
    ],
    "thing_defs": ["def_name", "label", "category"],
}
# by → 原生分组路径（按 root 类型区分）。
GROUP_PATHS = {
    "things": {
        "definition": "definition.def_name",
        "category": "definition.category",
        "holder": "parent_holder.ref",
        "map": "map_held.id",
    },
    "colonists": {"map": "map_id"},
    # `pawns` 的 map 分组已在 BY_KINDS 里公开允许，映射表却漏了这一项，
    # 合法调用会直接抛 KeyError。
    "pawns": {"map": "map_id"},
    "thing_defs": {"category": "category"},
    "maps": {},
}
BY_KINDS = {
    "things": ("definition", "category", "holder", "map"),
    "pawns": ("map",),
    "colonists": ("map",),
    "maps": (),
    "thing_defs": ("category",),
}
# look 的 distribution 默认分组。
DEFAULT_GROUP = {"things": ("definition",), "colonists": ("map",), "thing_defs": ("category",)}

# 必须明文内联的语义句子（来自 Mod scope/字段说明；不能被压成代号）。
SEMANTICS = {
    "containment": "Holder/container membership selects current containment; it is not faction or property "
    "ownership, and it does not establish usable, tradable or colony property.",
    "pawn": "Pawn includes humans, animals and mechanoids regardless of faction; Pawn is not a human, colonist "
    "or ownership identity.",
    "holder": "held_by is the nearest containing Pawn (containment, not ownership); null means no Pawn holds this "
    "instance. held_by_faction is that Pawn's faction: null means the holder has no faction (or there is no "
    "holder) - it does NOT mean the read failed. Use held_by_faction.is_player instead of inferring "
    '"ours" from containment.',
    "unknown": "Unknown is not zero and not empty: an unavailable read, an incomplete scan and a legitimate zero "
    "or empty collection are different facts.",
    "lower_bound": "An incomplete scan yields a lower bound (or unavailable), never an exact total.",
    "units": "records counts matching objects; units sums stack_count of those objects. A record is not "
    "necessarily an item or a human.",
    # 父持有者必须能区分“地图”与“容器”，否则一个共享的不透明引用会被读成“同一个箱子”。
    "parent_holder": "parent_holder is this instance's immediate container, named by its native type: "
    "Verse.Map means the instance is spawned on that map rather than inside any container; "
    "a tracker/owner type (for example Verse.Pawn_InventoryTracker or Verse.ThingOwner) or a "
    "building type means it is inside that holder. Two instances sharing a parent_holder have "
    "the same immediate holder - containment, not ownership, and it never proves a location "
    "beyond what that type states.",
    "durability": "hit_points/max_hit_points are the native Thing getters for this instance. A null with no "
    "issue at its path is the game returning no value for this member (some definitions carry no "
    "durability); that is neither zero nor a failed read. An issue at the path means the read failed.",
}

# --- schema -------------------------------------------------------------------


def capability_values(directory):
    if not isinstance(directory, dict):
        return ()
    root = next((r for r in directory.get("roots", []) if r["from"] == "things"), {})
    if not any(a.get("name") == "capabilities" for a in root.get("args", [])) or "capabilities" not in directory.get(
        "field_index", {}
    ).get(root.get("type"), {}):
        return ()
    for tool in directory.get("world_observation", {}).get("tools", []):
        if tool.get("name") == "survey_space":
            return tuple(
                tool.get("parameters", {})
                .get("properties", {})
                .get("select", {})
                .get("properties", {})
                .get("capabilities", {})
                .get("items", {})
                .get("enum", [])
            )
    return ()


def object_schema(directory=None):
    capability_names = capability_values(directory)
    schema = {
        "type": "object",
        "description": "Closed object selector. Supported selectors by kind: "
        + "; ".join(
            kind
            + ": "
            + (", ".join(s for s in selectors if s != "capabilities" or capability_names) if selectors else "none")
            for kind, selectors in OBJECT_SELECTORS.items()
        )
        + ". id aliases one ids entry; name aliases one names entry. No field names, paths or native argument keys.",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "name": {
                "type": "string",
                "description": "Exact display name; ambiguity returns candidates, never a pick.",
            },
            "names": {"type": "array", "items": {"type": "string"}, "description": "Exact display names."},
            "id": {"type": "string", "description": "One exact native object ID; use id or ids, never both."},
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Exact native object IDs. kind thing/pawn/map accepts one; use things/pawns/maps for a set.",
            },
            "definition": {"type": "string", "description": "Exact def_name or localized definition label."},
            "category": {
                "type": "string",
                "description": "For things: native broad ThingCategory kind (e.g. Item, Building), not a gameplay use such as medicine. For thing_defs: exact ThingCategoryDef content-category name. These are distinct domains.",
            },
            "map": {
                "type": "string",
                "default": "current",
                "description": "Loaded map ID or current. For kind map this selects the map itself (alias for id); for Pawn/Thing populations it restricts their map scope.",
            },
            "include_held": {
                "type": "boolean",
                "default": True,
                "description": "true includes holder contents (inventory, containers, equipment); false keeps spawned map Things. "
                "Default true; use false only for caller-requested ground/map scope. Ignored for definition kinds.",
            },
            "include_fogged": {
                "type": "boolean",
                "default": True,
                "description": "true includes fogged Things; false keeps visible Things. "
                "Default true; use false only for caller-requested visible scope. Ignored for definition kinds.",
            },
        },
        "required": ["kind"],
        "additionalProperties": False,
    }

    values = capability_values(directory)
    if values:
        schema["properties"]["capabilities"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": len(values),
            "items": {"type": "string", "enum": list(values)},
            "description": "For kind things: match any listed native capability, intersected with other selectors. Same membership as survey_space, without reading environment. "
            + json.dumps(
                directory.get("world_observation", {}).get("capability_meanings", {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    # Preserve the existing content-category selector on thing_defs. Only the
    # physical things predicate uses the native ThingCategory enum.
    domains = directory.get("value_domains", {}) if isinstance(directory, dict) else {}
    values = domains.get("Verse.ThingCategory", {}).get("values")
    if isinstance(values, list) and values and all(isinstance(v, str) for v in values):
        schema["allOf"] = [
            {
                "if": {"properties": {"kind": {"const": "things"}}, "required": ["kind"]},
                "then": {"properties": {"category": {"enum": list(values)}}},
            }
        ]
    return schema


PAUSE_WINDOW_FIELDS = (
    "pause_windows.status", "pause_windows.sampled_tick", "pause_windows.count", "pause_windows.truncated",
    "pause_windows.windows.window_id", "pause_windows.windows.type", "pause_windows.windows.stack_index",
    "pause_windows.windows.opened_tick", "pause_windows.windows.opening_source.status",
    "pause_windows.windows.opening_source.frames",
)
