"""Resolve selected plan elements from saved native creation and transition IDs."""


def native_element(plan_id, plan, element):
    origin = plan["origin"]
    result = {"plan_id": plan_id, "element_id": element["id"],
              "kind": "floor" if element["layer"] == "floor" else "building",
              "definition": element["definition"], "x": origin["x"] + element["position"]["x"],
              "z": origin["z"] + element["position"]["z"], "rotation": element["rotation"]}
    if element.get("stuff") is not None:
        result["stuff"] = element["stuff"]
    return result


def target_rotation_matches(native, *, committed=None):
    """Keep fixed rotations exact and allow the game's scoped 1×1 door orientation."""
    element, target = native.get("element") or {}, native.get("target") or {}
    requested, observed = element.get("rotation"), target.get("rotation")
    if any(type(value) is not int or value not in range(4) for value in (requested, observed)):
        return False
    if requested == observed:
        return True
    if (native.get("rotation_policy") != "native_door" or element.get("kind") != "building"
            or committed is not None and committed.get("rotation_policy") != "native_door"):
        return False
    rect = target.get("occupied_rect")
    lineage = native.get("lineage")
    return (isinstance(rect, dict)
            and all(type(rect.get(edge)) is int and rect[edge] == element.get(edge[-1])
                    for edge in ("min_x", "max_x", "min_z", "max_z"))
            and isinstance(lineage, list) and bool(lineage)
            and all(isinstance(identity, str) and bool(identity) for identity in lineage)
            and len(set(lineage)) == len(lineage)
            and lineage[0] == native.get("source_id") and lineage[-1] == target.get("id")
            and (native.get("stage") == "blueprint" and len(lineage) == 1 and native.get("provenance") == "native_creation"
                 or native.get("stage") in {"blueprint", "frame", "built"} and len(lineage) > 1 and native.get("provenance") == "native_transition"))


def facts(state, data, *, observation_id, load_epoch):
    if data.get("format") != "construction_intents.v1":
        return []
    rows = {(row.get("plan_id"), row["element_id"]): row for row in data.get("objects", [])}
    prepared = []
    for plan in state["plans"]:
        if plan["plan"]["map_id"] != data.get("map_id"):
            continue
        definitions = {row["id"]: row for row in plan["plan"]["elements"]}
        for current in plan["elements"]:
            definition = definitions.get(current["id"])
            native = rows.get((plan["plan_id"], current["id"]))
            if definition is None or native is None:
                continue
            element = native.get("element", {})
            origin = plan["plan"]["origin"]
            if (element.get("definition") != definition["definition"]
                    or element.get("x") != definition["position"]["x"] + origin["x"]
                    or element.get("z") != definition["position"]["z"] + origin["z"]
                    or element.get("rotation") != definition["rotation"]
                    or element.get("stuff") != definition.get("stuff")):
                continue
            if native.get("provenance") not in {"native_creation", "native_transition"}:
                continue
            stage = native.get("stage")
            target = native.get("target") or {}
            object_id = target.get("id")
            if stage in {"blueprint", "frame", "built"} and (
                    any(target.get(key) != element.get(key) for key in ("definition", "x", "z", "stuff"))
                    or not target_rotation_matches(native)):
                stage = "missing"
            if stage == "floor_complete":
                if native.get("floor_observed") is not True:
                    stage = "missing"
                else:
                    stage = "built"
                object_id = None
            elif stage == "unknown":
                stage = "missing"
            if stage not in {"blueprint", "frame", "built", "missing", "destroyed", "user_cancelled", "model_cancelled", "user_replaced"}:
                continue
            previous = current.get("binding", {}).get("object_id")
            lineage = native.get("lineage", [])
            if previous and previous not in lineage and previous not in {native.get("source_id"), target.get("id"), native.get("previous_object_id")}:
                continue
            if previous and previous != object_id and stage in {"blueprint", "frame", "built"}:
                if previous not in lineage and native.get("previous_object_id") != previous:
                    continue
            prepared.append({"plan_id": plan["plan_id"], "element_id": current["id"], "stage": stage,
                             "object_id": object_id, "previous_object_id": previous,
                             "observation_id": observation_id, "load_epoch": load_epoch,
                             "field_identity": native.get("field_identity")})
    return prepared
