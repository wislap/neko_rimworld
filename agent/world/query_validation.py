"""Model input validation for the look/count object, aspects, metric, grouping and detail."""

from __future__ import annotations

from .query_catalog import (
    ASPECTS,
    BY_KINDS,
    DEF_KINDS,
    DEFAULT_GROUP,
    GROUPINGS,
    KINDS,
    MAP_KINDS,
    METRICS,
    OBJECT_SELECTORS,
    PAUSE_WINDOW_FIELDS,
    PAWN_KINDS,
    POPULATIONS,
    ROOTS,
)
from .query_surface import SurfaceError


def _nonempty_string(value, name):
    if not isinstance(value, str) or not value:
        raise SurfaceError(name + " must be a nonempty string")
    return value


def _nonempty_strings(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise SurfaceError(name + " must be a nonempty string array")
    return list(value)


def normalize_object(raw):
    """把模型输入变成规范化 object；未知 kind/选择器/类型一律拒绝。"""
    if not isinstance(raw, dict):
        raise SurfaceError("object must be an object")
    raw = dict(raw)
    if "id" in raw:
        if "ids" in raw:
            raise SurfaceError("use id or ids, not both")
        raw["ids"] = [_nonempty_string(raw.pop("id"), "id")]
    unknown = sorted(
        set(raw)
        - {
            "kind",
            "name",
            "names",
            "ids",
            "definition",
            "category",
            "map",
            "include_held",
            "include_fogged",
            "capabilities",
        }
    )
    if unknown:
        raise SurfaceError("object does not accept: " + ", ".join(unknown))
    kind = raw.get("kind")
    if kind not in ROOTS:
        raise SurfaceError("kind must be one of: " + ", ".join(KINDS))
    if "name" in raw and "names" in raw:
        raise SurfaceError("use either name or names, not both")
    if "ids" in raw and ("name" in raw or "names" in raw):
        raise SurfaceError("use exact ids or an exact display name, not both")
    allowed = OBJECT_SELECTORS[kind]
    for key in (
        "name",
        "names",
        "ids",
        "definition",
        "category",
        "map",
        "include_held",
        "include_fogged",
        "capabilities",
    ):
        if key in raw and key not in allowed and not (key == "name" and "names" in allowed):
            raise SurfaceError("kind " + kind + " does not accept " + key)
    obj = {"kind": kind}
    if "name" in raw:
        obj["names"] = [_nonempty_string(raw["name"], "name")]
    if "names" in raw:
        obj["names"] = _nonempty_strings(raw["names"], "names")
    if "ids" in raw:
        obj["ids"] = _nonempty_strings(raw["ids"], "ids")
    if "definition" in raw:
        obj["definition"] = _nonempty_string(raw["definition"], "definition")
    if "capabilities" in raw:
        obj["capabilities"] = list(dict.fromkeys(_nonempty_strings(raw["capabilities"], "capabilities")))
    if "category" in raw:
        obj["category"] = _nonempty_string(raw["category"], "category")
    if "map" in raw:
        obj["map"] = _nonempty_string(raw["map"], "map")
    # 两个范围标志同向：true = 包含更多。旧名 held/visible_only 极性相反、且未说明 false 的
    # 含义，实测导致模型写出 held=false 却把结果描述成"含被持有"（真实值 74，报 0）。
    for key in ("include_held", "include_fogged"):
        if key in raw:
            if type(raw[key]) is not bool:
                raise SurfaceError(key + " must be a boolean")
            # Definitions have no instances, holders or fog state. Accept an
            # otherwise valid shared selector without inventing a Def scope.
            if kind not in DEF_KINDS:
                obj[key] = raw[key]
    if kind == "map" and "map" in obj and ("ids" in obj or "names" in obj):
        raise SurfaceError("map reads one map: use map, id/ids, or name/names, not competing selectors")
    if "ids" in obj and ("definition" in obj or "category" in obj):
        raise SurfaceError("use exact ids or definition/category, not competing selectors")
    if "definition" in obj and "category" in obj:
        raise SurfaceError("use either definition or category, not both")
    if kind in ("colonists", "things", "pawns"):
        obj.setdefault("map", "current")
    if kind == "things":
        obj.setdefault("include_held", True)
        obj.setdefault("include_fogged", True)
    if "ids" in obj:
        if kind in ("pawn", "thing", "map") and len(obj["ids"]) != 1:
            raise SurfaceError("kind " + kind + " reads exactly one id")
    return obj


def supports_pause_windows(surface):
    try:
        return all(surface.path_type("game", path) for path in PAUSE_WINDOW_FIELDS)
    except (SurfaceError, KeyError, TypeError):
        return False


def validate_aspects(kind, aspects):
    if not isinstance(aspects, list) or not aspects or not all(isinstance(x, str) for x in aspects):
        raise SurfaceError("aspects must be a nonempty string array")
    supported = supported_aspects(kind)
    for aspect in aspects:
        if aspect == "pause_windows" and kind == "game":
            continue
        if aspect not in ASPECTS:
            raise SurfaceError("unknown aspect: " + aspect)
        if aspect not in supported:
            raise SurfaceError("kind " + kind + " supports: " + ", ".join(supported))
    return tuple(dict.fromkeys(aspects))


def validate_metric(kind, metric):
    if metric not in METRICS:
        raise SurfaceError("metric must be one of: " + ", ".join(METRICS))
    if kind not in POPULATIONS:
        raise SurfaceError("count measures a population: " + ", ".join(POPULATIONS) + " (got " + kind + ")")
    if metric == "units" and kind != "things":
        raise SurfaceError("units sums stack_count and is only defined for kind things")
    return metric


def validate_by(kind, by):
    if by is None:
        return None
    if by not in GROUPINGS:
        raise SurfaceError("by must be one of: " + ", ".join(GROUPINGS))
    if by not in BY_KINDS[kind]:
        allowed = BY_KINDS[kind] or ("nothing",)
        raise SurfaceError("kind " + kind + " cannot group by " + by + "; allowed: " + ", ".join(allowed))
    return by


def validate_detail(detail):
    if type(detail) is not int or not 0 <= detail <= 100:
        raise SurfaceError("detail must be an integer 0..100")
    return detail


def supported_aspects(kind):
    if kind in PAWN_KINDS:
        base = ["identity", "health", "skills", "gear", "work", "mood", "profile"]
    elif kind in MAP_KINDS:
        base = ["identity", "resources"]
    else:
        base = ["identity"]
    if kind in POPULATIONS:
        base = base + ["quantity"]
        if kind in DEFAULT_GROUP:
            base = base + ["distribution"]
    return tuple(base)
