"""Deterministic scope drawings from native observation and selected plan geometry.

The JSON render spec is shared by the UI, SVG and optional host-Pillow PNG.
Colors describe layers and coverage; they do not infer unseen game appearance.
"""

from __future__ import annotations

import copy
import html
import io
from collections.abc import Mapping

from .construction_geometry import ConstructionPlanError, _integer, _point, plan_digest
from .spatial_observation import SpatialObservationError, observation_cells

RENDER_FORMAT = "construction_render.v1"
PALETTE = {
    "unknown": "#758092",
    "terrain": "#e4e9dc",
    "built": "#59626c",
    "blueprint": "#7b9ec7",
    "frame": "#c79750",
    "building": "#27a7b4",
    "floor": "#b2cabb",
    "conduit": "#d0a944",
    "roof": "#d5c3e0",
    "remove_roof": "#ecc8c8",
    "home": "#c4d8e8",
    "allowed": "#c4dfcf",
    "stockpile": "#dfd0ae",
    "growing": "#b0cc99",
    "error": "#d63847",
    "issue_unknown": "#ba801c",
}


class SpatialRenderingUnavailable(RuntimeError):
    """The installed host does not supply a supported PNG renderer."""


def build_render_spec(observation, plan=None, check=None):
    """Return a bounded JSON drawing specification using world x/z coordinates.

    A native sample and its coverage remain attached to the exact observation.
    Missing cells are explicitly unobserved. Plans outside this observed scope
    are retained in the spec but clipped by each renderer, never used to expand it.
    """
    if not isinstance(observation, Mapping) or observation.get("format") != "construction_observation.v1":
        raise ConstructionPlanError("Drawing requires a native construction observation")
    bounds = observation.get("bounds")
    if not isinstance(bounds, Mapping):
        raise ConstructionPlanError("Observation requires bounds")
    x0 = _integer(bounds.get("min_x"), "bounds.min_x")
    z0 = _integer(bounds.get("min_z"), "bounds.min_z")
    width = _integer(bounds.get("width"), "bounds.width", minimum=1, maximum=64)
    height = _integer(bounds.get("height"), "bounds.height", minimum=1, maximum=64)
    _integer(x0 + width - 1, "bounds.max_x")
    _integer(z0 + height - 1, "bounds.max_z")
    scope = {(x, z) for x in range(x0, x0 + width) for z in range(z0, z0 + height)}
    try:
        rows = observation_cells(observation)
    except SpatialObservationError as exc:
        raise ConstructionPlanError(str(exc)) from exc
    cells = {}
    for row in rows:
        pos = _point(row, "observed.cell")
        if pos not in scope or pos in cells:
            raise ConstructionPlanError("Observation cells must be unique and inside its bounds")
        cells[pos] = copy.deepcopy(dict(row))
    for x, z in scope - cells.keys():
        cells[x, z] = {"x": x, "z": z, "known": False, "status": "unobserved"}
    objects = []
    raw_objects = observation.get("objects", [])
    if not isinstance(raw_objects, list):
        raise ConstructionPlanError("Observation objects must be a list")
    for raw in raw_objects:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("occupied_rect"), Mapping):
            raise ConstructionPlanError("Observed objects require native occupied_rect")
        rect = raw["occupied_rect"]
        a, b = _integer(rect.get("min_x"), "occupied_rect.min_x"), _integer(rect.get("min_z"), "occupied_rect.min_z")
        c, d = _integer(rect.get("max_x"), "occupied_rect.max_x"), _integer(rect.get("max_z"), "occupied_rect.max_z")
        if not a <= c < a + 64 or not b <= d < b + 64:
            raise ConstructionPlanError("Observed object has invalid occupied_rect")
        item = copy.deepcopy(dict(raw))
        item["cells"] = [{"x": x, "z": z} for x in range(a, c + 1) for z in range(b, d + 1)]
        objects.append(item)
    result = {
        "format": RENDER_FORMAT,
        "map_id": observation.get("map_id"),
        "bounds": {"min_x": x0, "min_z": z0, "width": width, "height": height},
        "sample": copy.deepcopy(observation.get("sample")),
        "coverage": copy.deepcopy(observation.get("coverage", {})),
        "source_sha256": plan_digest(observation),
        "cells": [cells[p] for p in sorted(cells)],
        "objects": sorted(objects, key=lambda r: str(r.get("id", ""))),
        "elements": [],
        "areas": [],
        "issues": [],
        "legend": copy.deepcopy(PALETTE),
    }
    if plan is not None:
        if not isinstance(plan, Mapping) or plan.get("compiled") is not True or plan.get("map_id") != result["map_id"]:
            raise ConstructionPlanError("Drawing plan must be compiled for the observation map")
        ox, oz = _point(plan.get("origin"), "origin")

        def world(points):
            return [{"x": x + ox, "z": z + oz} for x, z in (_point(p, "local.cell") for p in points)]

        result["plan_sha256"] = plan_digest(plan)
        for key in ("plan_id", "revision"):
            if key in plan:
                result[key] = copy.deepcopy(plan[key])
        for raw in plan["elements"]:
            item = {key: copy.deepcopy(raw[key]) for key in ("id", "definition", "layer")}
            item["cells"] = world(raw["footprint"])
            if "interaction_cell" in raw:
                item["interaction_cell"] = world([raw["interaction_cell"]])[0]
            if "interaction_cells" in raw:
                item["interaction_cells"] = world(raw["interaction_cells"])
            result["elements"].append(item)
        for raw in plan.get("areas", []):
            result["areas"].append({"id": raw["id"], "kind": raw["kind"], "cells": world(raw["cells"])})
        if check is not None:
            if not isinstance(check, Mapping) or check.get("plan_sha256") != result["plan_sha256"]:
                raise ConstructionPlanError("Drawing check belongs to a different plan")
            if check.get("observation_sha256") not in (None, result["source_sha256"]):
                raise ConstructionPlanError("Drawing check belongs to a different observation")
            for raw in check.get("issues", []):
                item = copy.deepcopy(raw)
                item["cells"] = world(raw.get("cells", []))
                result["issues"].append(item)
    elif check is not None:
        raise ConstructionPlanError("Drawing a check requires its selected plan")
    return result


def _primitives(spec, cell_size):
    """One ordered scene description consumed by both image formats."""
    _integer(cell_size, "cell_size", minimum=8, maximum=40)
    bounds = spec["bounds"]
    x0, z0, width, height = (bounds[k] for k in ("min_x", "min_z", "width", "height"))
    margin = 32
    w, h = max(560, width * cell_size + margin * 2), height * cell_size + margin * 2
    primitives = []

    def rectangle(cell, color, *, inset=0, outline=None, label=None):
        x, z = _point(cell, "render.cell")
        if not x0 <= x < x0 + width or not z0 <= z < z0 + height:
            return
        left = margin + (x - x0) * cell_size + inset
        top = margin + (z0 + height - 1 - z) * cell_size + inset
        primitives.append(
            {
                "kind": "rect",
                "x": left,
                "y": top,
                "width": cell_size - inset * 2,
                "height": cell_size - inset * 2,
                "fill": color,
                "stroke": outline,
                "label": label,
            }
        )

    for cell in spec["cells"]:
        rectangle(cell, PALETTE["terrain"] if cell.get("known") is True else PALETTE["unknown"], outline="#c4c9c3")
    for area in spec["areas"]:
        for cell in area["cells"]:
            rectangle(cell, PALETTE.get(area["kind"], PALETTE["allowed"]), inset=cell_size // 3, label=area["id"])
    for item in spec["objects"]:
        for cell in item["cells"]:
            rectangle(cell, PALETTE.get(item.get("stage"), PALETTE["built"]), inset=2, label=str(item.get("id", "")))
    for item in spec["elements"]:
        for cell in item["cells"]:
            rectangle(cell, None, inset=1, outline=PALETTE[item["layer"]], label=item["id"])
        for interaction in item.get("interaction_cells", []):
            rectangle(
                interaction,
                PALETTE["building"],
                inset=max(3, cell_size // 3),
                label="interaction:" + item["id"],
            )
    for issue in spec["issues"]:
        for cell in issue["cells"]:
            rectangle(
                cell,
                None,
                inset=0,
                outline=PALETTE["error" if issue.get("severity") == "error" else "issue_unknown"],
                label=issue["code"],
            )
    primitives.extend(
        [
            {
                "kind": "text",
                "x": margin,
                "y": 13,
                "text": f"{spec['map_id']}  x:{x0}..{x0 + width - 1} z:{z0}..{z0 + height - 1}  N ^",
            },
            {"kind": "text", "x": margin, "y": 26, "text": _sample_label(spec)},
            {
                "kind": "text",
                "x": margin,
                "y": h - 12,
                "text": "Observed cells + selected plan | unknown=grey | plan=cyan | issue=red/amber",
            },
        ]
    )
    return w, h, primitives


def _sample_label(spec):
    sample = spec["sample"] if isinstance(spec["sample"], Mapping) else {}
    start = sample.get("started_tick", sample.get("game_tick", "?"))
    finish = sample.get("finished_tick", sample.get("game_tick", "?"))
    return f"sample tick:{start}..{finish} source:{spec['source_sha256'][:16]}"


def render_scene_svg(observation, plan=None, check=None, *, cell_size=20):
    """Return an escaped deterministic SVG containing no scripts or external assets."""
    spec = build_render_spec(observation, plan, check)
    width, height, primitives = _primitives(spec, cell_size)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        "<desc>"
        + html.escape(f"Native observation {spec['source_sha256']}; plan {spec.get('plan_sha256', 'none')}")
        + "</desc>",
    ]
    for item in primitives:
        if item["kind"] == "text":
            parts.append(
                f'<text x="{item["x"]}" y="{item["y"]}" font-family="monospace" font-size="10">{html.escape(item["text"])}</text>'
            )
        else:
            label = "<title>" + html.escape(item["label"]) + "</title>" if item["label"] else ""
            parts.append(
                f'<rect x="{item["x"]}" y="{item["y"]}" width="{item["width"]}" height="{item["height"]}" '
                f'fill="{item["fill"] or "none"}" stroke="{item["stroke"] or "none"}" stroke-width="2">{label}</rect>'
            )
    parts.append("</svg>")
    return "".join(parts)


def render_scene_png(observation, plan=None, check=None, *, cell_size=20):
    """Return deterministic PNG bytes through the existing N.E.K.O Pillow dependency.

    Plugin-only installations without Pillow retain SVG/spec support and receive
    an explicit unavailable result here; no image encoder is shipped or installed.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise SpatialRenderingUnavailable("PNG rendering requires the host's existing Pillow installation") from exc
    spec = build_render_spec(observation, plan, check)
    width, height, primitives = _primitives(spec, cell_size)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for item in primitives:
        if item["kind"] == "text":
            draw.text((item["x"], item["y"] - 10), item["text"], fill="#202830")
        else:
            draw.rectangle(
                (item["x"], item["y"], item["x"] + item["width"] - 1, item["y"] + item["height"] - 1),
                fill=item["fill"],
                outline=item["stroke"],
                width=2,
            )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=6)
    return output.getvalue()
