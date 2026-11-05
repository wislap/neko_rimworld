"""Bind durable colony work to this run's real evidence and planner authority."""

from __future__ import annotations

import copy

from ...tool_contracts import ToolExecutionError, ToolSpec, binding_is_current


def _evidence(session, identity, observation_id, operation, *, allow_partial=False):
    item = next((row for row in session.observations if row.get("observation_id") == observation_id), None)
    if item is None or item.get("request", {}).get("operation") != operation:
        raise ValueError("Read the named native observation in this run first: " + operation)
    response = item.get("response", {})
    if tuple(response.get(key) for key in ("connection_epoch", "colony_id", "load_epoch")) != tuple(identity):
        raise ValueError("The observation belongs to a different loaded world")
    payload = response.get("payload", {})
    if payload.get("status") not in ({"ok", "partial"} if allow_partial else {"ok"}) or (payload.get("issues") and not allow_partial):
        raise ValueError("The native observation is incomplete or unavailable")
    return copy.deepcopy(payload.get("data", {}))


def resource_snapshot(session, identity, observation_id, sources):
    from ...workspace.resource_evidence import snapshot

    data = _evidence(session, identity, observation_id, "inspect_resources")
    original = next(row for row in session.observations if row["observation_id"] == observation_id)
    return snapshot(data.get("rows", []), data["scope"], observation_id=observation_id, load_epoch=identity[2],
                    game_tick=original["response"]["game_tick"], sources=sources)


def order_batches(operations, map_id):
    """Bound native batches and serialize structural zone creations."""
    groups, pending, field_count = [], [], 0
    for operation in operations:
        fields = len(operation["value"]["cells"]) if operation["op"] == "set_area_cell_flags" else 1
        if fields > 64:
            raise ValueError("A local area operation must select at most 64 native cell fields")
        if operation["op"] == "create_zone":
            if pending:
                groups.append(pending)
                pending, field_count = [], 0
            groups.append([operation])
        else:
            if pending and field_count + fields > 64:
                groups.append(pending)
                pending, field_count = [], 0
            pending.append(operation)
            field_count += fields
            if len(pending) == 64 or field_count == 64:
                groups.append(pending)
                pending, field_count = [], 0
    if pending:
        groups.append(pending)
    result = []
    for index, group in enumerate(groups):
        reads = {}
        for op in group:
            if op["op"] in {"cancel_blueprint", "restart_blueprint"}:
                value = op if op["op"] == "cancel_blueprint" else op["value"]
                reads.setdefault(value["plan_id"], []).append(value["element_id"])
        result.append({"batch_index": index, "operations": group, "operation_count": len(group), "status": "not_dispatched",
                       "field_count": sum(len(op["value"]["cells"]) if op["op"] == "set_area_cell_flags" else 1 for op in group),
                       "area_reads": [{"tool": "inspect_areas", "arguments": {"map": map_id, "ids": [op["area"]],
                           "cells": [{"x": cell["x"], "z": cell["z"]} for cell in op["value"]["cells"]]}}
                           for op in group if op["op"] == "set_area_cell_flags"],
                       "intent_reads": [{"tool": "inspect_construction_intents", "arguments": {
                           "map": map_id, "plan_id": plan_id, "ids": ids}} for plan_id, ids in reads.items()],
                       "preview": ({"tool": "preview_construction", "arguments": {"map": map_id,
                          "elements": [op["value"] for op in group if op["op"] == "place_blueprint"]}}
                         if any(op["op"] == "place_blueprint" for op in group) else None)})
    return result


def render_summary(spec):
    if spec is None:
        return None
    return {**{key: spec[key] for key in ("format", "map_id", "bounds", "sample", "coverage", "source_sha256", "plan_sha256")
               if key in spec}, "observed_cell_count": len(spec.get("cells", [])),
            "plan_element_count": len(spec.get("elements", [])), "model_view": "drawing_metadata_only"}


def enabled_area_cells(row):
    flags = row.get("write_flags", {}).get("cells")
    if isinstance(flags, list):
        return {(cell["x"], cell["z"]) for cell in flags if cell.get("enabled") is True}
    return {(cell["x"], cell["z"]) for cell in row.get("cells", [])}


def work_tools(setup, session):
    service = getattr(session, "colony_work_service", None)
    game = getattr(session, "colony_work_game", None)
    if service is None or game is None:
        return ()
    if ("colony_management" not in (setup.directory or {})
            and not any(spec.name == "construction_catalog" for spec in setup.tools)):
        return ()

    async def work(args):
        try:
            if set(args) != {"action", "arguments"} or not isinstance(args["arguments"], dict):
                raise ValueError("Supply action and its arguments object")
            action, values = args["action"], copy.deepcopy(args["arguments"])
            if setup.observation_source != "native" and action not in {"read", "preview", "suggest", "assess_resources"}:
                raise ToolExecutionError("native_source_required", "Live colony decisions require the native observation source")
            if not binding_is_current(session.binding_check):
                raise ToolExecutionError("world_changed", "This run is no longer current", recoverable=False)
            identity = setup.write_identity() if setup.write_identity is not None else None
            if identity is None:
                identity = (getattr(game, "connection_epoch", None), game.colony_id, game.load_epoch)
            if tuple(identity[1:]) != (game.colony_id, game.load_epoch):
                raise ValueError("The loaded world changed")
            if action == "read":
                state = await service.call("colony_work", game.colony_id, game.load_epoch, identity[0])
                if not values:
                    from ...workspace.work_state import summary
                    return summary(state)
                if set(values) - {"section", "id"} or values.get("section") not in {
                        "construction", "resources", "policies", "observations", "suggestions"}:
                    raise ValueError("Read accepts section and optional id")
                section = values["section"]
                result = state[section]
                if values.get("id"):
                    id_key = {"construction": "plan_id", "resources": "allocation_id", "policies": "impact_id",
                              "observations": "observation_id", "suggestions": "suggestion_id"}[section]
                    if isinstance(result, list):
                        result = [row for row in result if row.get(id_key) == values["id"]]
                    else:
                        result = {key: [row for row in rows if row.get(id_key) == values["id"]] for key, rows in result.items()}
                groups = [result] if isinstance(result, list) else list(result.values())
                for rows in groups:
                    for row in rows:
                        if "render_spec" in row:
                            row["render_spec"] = render_summary(row["render_spec"])
                return {section: result}
            if action in {"preview", "suggest", "adopt", "revise"}:
                from ..world.construction_geometry import compile_plan, inspect_layout, observation_bounds

                if action == "adopt" and "suggestion_event_seq" in values:
                    suggestion_seq = values.pop("suggestion_event_seq")
                    saved = await service.call("event", game.colony_id, suggestion_seq)
                    if saved is None or saved["kind"] != "construction.suggestion":
                        raise ValueError("Adoption must cite a retained read-only suggestion in this colony")
                    if "plan" in values or "source" in values:
                        raise ValueError("Adopt either a saved suggestion or an explicit plan and source")
                    values.update(plan=saved["payload"]["plan"], source={"event_seq": suggestion_seq})
                catalog = _evidence(session, identity, values.pop("catalog_observation_id", None), "construction_catalog")
                compiled = compile_plan(values["plan"], catalog)
                observation_id = values.pop("spatial_observation_id", None)
                observation = (_evidence(session, identity, observation_id, "inspect_construction", allow_partial=True)
                               if observation_id else None)
                check = inspect_layout(compiled, observation)
                values["plan"] = compiled
                attachments = []
                rendering = None
                if observation is not None:
                    from ..world.spatial_rendering import build_render_spec, render_scene_png

                    rendering = build_render_spec(observation, compiled, check)
                    if session.media_store is not None:
                        png = render_scene_png(observation, compiled, check)
                        metadata = {
                            "colony_id": game.colony_id, "load_epoch": game.load_epoch,
                            "map_id": compiled["map_id"], "observation_id": observation_id,
                            "plan_sha256": check["plan_sha256"]}
                        if action in {"adopt", "revise"}:
                            metadata.update(plan_id=values["plan_id"], revision=1 if action == "adopt" else values["expected_revision"] + 1,
                                            scope="construction_command_candidate", command=action)
                            owner = "plan:" + values["plan_id"]
                        elif action == "suggest":
                            metadata.update(suggestion_id=values["suggestion_id"], scope="read_only_suggestion")
                            owner = "suggestion:" + values["suggestion_id"]
                        else:
                            metadata.update(candidate_run_id=session.ledger.run_id, scope="read_only_candidate")
                            owner = "run:" + session.ledger.run_id
                        ref = session.media_store.put(png, metadata=metadata, owner="run:" + session.ledger.run_id)
                        session.attach_media(ref, caption="Current sampled world and proposed construction overlay",
                                             replace_active=True, activate=session.image_input.enabled)
                        attachments.append(ref)
                if action == "preview":
                    return {"plan": compiled, "check": check, "render": render_summary(rendering), "attachments": attachments,
                            "native_eligibility": "not_checked", "image_input": session.image_input.public_dict(),
                            "actual_image_input": False,
                            "spatial_read": {"tool": "inspect_construction", "arguments": {
                                "map": compiled["map_id"], "bounds": observation_bounds(compiled)}}}
                if action == "suggest":
                    values["attachments"] = attachments
                    values["render_spec"] = rendering
                elif action in {"adopt", "revise"}:
                    values["render_spec"] = rendering
                    values["attachments"] = attachments
                if action in {"adopt", "revise"} and check["status"] == "failed":
                    raise ToolExecutionError("construction_invalid", "Resolve the layout findings before adoption",
                                             details={"check": check})
            if action == "assess_resources":
                from ...workspace.resource_allocation import assess

                snapshot = resource_snapshot(session, identity, values.pop("snapshot_observation_id", None),
                                             values.pop("resource_sources", None))
                allocation_id = values.pop("allocation_id", None)
                if "selection" in values:
                    raise ValueError("Read a saved allocation_id to compare; do not author a replacement selection")
                if allocation_id is not None:
                    state = await service.call("colony_work", game.colony_id, game.load_epoch, identity[0])
                    selected = next((row for row in state["resources"]["decisions"] if row["allocation_id"] == allocation_id), None)
                    if selected is None:
                        raise ValueError("Unknown resource allocation")
                    values["selection"] = selected
                    await service.call("record_resource_observation", game.colony_id, game.load_epoch,
                        service.game_session().game_tick, allocation_id=allocation_id, expected_revision=selected["revision"],
                        snapshot=snapshot, demands=None, command_binding_check=session.binding_check)
                return {"snapshot": snapshot, "assessment": assess(snapshot, **values)}
            if action == "orders":
                if (session.role != "planner" or not {"plan_id", "task_id", "element_ids"} <= values.keys()
                        or values.keys() - {"plan_id", "task_id", "element_ids", "area_ids", "areas_observation_id", "mode", "reason"}):
                    raise ValueError("Orders require the planner's plan_id, task_id and chosen element_ids")
                state = await service.call("colony_work", game.colony_id, game.load_epoch, identity[0])
                selected = next((row for row in state["construction"]["plans"] if row["plan_id"] == values["plan_id"]), None)
                if selected is None or selected["task_id"] != values["task_id"]:
                    raise ValueError("The adopted plan must belong to the named task")
                ids = values["element_ids"]
                area_ids = values.get("area_ids", [])
                if (not isinstance(ids, list) or not isinstance(area_ids, list) or not ids + area_ids
                        or len(set(ids + area_ids)) != len(ids + area_ids)):
                    raise ValueError("Choose unique current construction elements")
                definitions = {row["id"]: row for row in selected["plan"]["elements"]}
                statuses = {row["id"]: row for row in selected["elements"]}
                elements = []
                mode = values.get("mode", "place")
                if mode not in {"place", "cancel", "restart"} or (mode != "place" and area_ids):
                    raise ValueError("Select place, cancel or restart; cancellation/restart applies to native construction elements")
                if mode == "restart" and (not isinstance(values.get("reason"), str) or not values["reason"].strip()):
                    raise ValueError("An explicit restart decision requires a reason; missing evidence never automatically authorizes rebuilding")
                from ...workspace.construction_evidence import native_element
                for element_id in ids:
                    row, status = definitions.get(element_id), statuses.get(element_id)
                    stages = {"place": {"unobserved"}, "cancel": {"blueprint"}, "restart": {"model_cancelled", "destroyed", "user_cancelled"}}[mode]
                    intent = "cancelled" if mode == "cancel" else "continue"
                    if row is None or status["intent"] != intent or status["stage"] not in stages:
                        raise ValueError("The selected native operation does not match the element's current intent and stage")
                    elements.append(native_element(selected["plan_id"], selected["plan"], row))
                if mode != "cancel":
                    await service.call("construction_dispatch_authority", game.colony_id, game.load_epoch, selected["plan_id"], ids + area_ids)
                operations = ([{"op": "cancel_blueprint", "map": selected["plan"]["map_id"], "plan_id": selected["plan_id"],
                                "element_id": row["element_id"], "value": False} for row in elements] if mode == "cancel" else
                              [{"op": "restart_blueprint" if mode == "restart" else "place_blueprint",
                                "map": selected["plan"]["map_id"], "value": row} for row in elements])
                uncovered = []
                area_patches = {}
                if area_ids:
                    areas = _evidence(session, identity, values.get("areas_observation_id"), "inspect_areas")
                    if areas.get("map_id") != selected["plan"]["map_id"]:
                        raise ValueError("Native areas must belong to the adopted plan map")
                    definitions = {row["id"]: row for row in selected["plan"].get("areas", [])}
                    origin = selected["plan"]["origin"]
                    for area_id in area_ids:
                        area = definitions.get(area_id)
                        status = statuses.get(area_id)
                        if area is None or status is None or status["intent"] != "continue":
                            raise ValueError("Choose a continuing area of the current plan")
                        cells = {(cell["x"] + origin["x"], cell["z"] + origin["z"]) for cell in area["cells"]}
                        native_kind = {"roof": "build_roof", "remove_roof": "no_roof", "home": "home"}.get(area["kind"])
                        if native_kind:
                            matches = [row for row in areas.get("objects", []) if row.get("kind") == native_kind
                                       and row.get("complete") is True and row.get("status") == "observed"]
                            if len(matches) != 1:
                                raise ValueError("Read the complete native " + native_kind + " area before preparing its cells")
                            native = matches[0]
                            cells -= enabled_area_cells(native)
                            opposing_kind = {"build_roof": "no_roof", "no_roof": "build_roof"}.get(native_kind)
                            for opposing in areas.get("objects", []):
                                if (opposing.get("kind") == opposing_kind and opposing.get("complete") is True
                                        and opposing.get("status") == "observed"):
                                    conflict = cells & enabled_area_cells(opposing)
                                    if conflict:
                                        uncovered.append({"area_id": area_id, "cells": [{"x": x, "z": z} for x, z in sorted(conflict)],
                                            "reason": "Opposing roof instruction is active; any removal requires a separate explicit authorized operation"})
                                        cells -= conflict
                            area_patches.setdefault(native["id"], set()).update(cells)
                        elif area["kind"] in {"stockpile", "growing"}:
                            operations.append({"op": "create_zone", "map": areas["map_id"], "value": {
                                "kind": area["kind"], "cells": [{"x": x, "z": z} for x, z in sorted(cells)]}})
                        else:
                            uncovered.append({"area_id": area_id, "reason": "Choose an existing native allowed-area identity explicitly through the normal area write tools"})
                    for native_id, points in area_patches.items():
                        cells = [{"x": x, "z": z, "enabled": True} for x, z in sorted(points, key=lambda point: (point[1], point[0]))]
                        operations.extend({"op": "set_area_cell_flags", "map": areas["map_id"], "area": native_id,
                                           "value": {"cells": cells[offset:offset + 64]}}
                                          for offset in range(0, len(cells), 64))
                return {"plan_id": selected["plan_id"], "revision": selected["revision"], "task_id": selected["task_id"],
                        "operations": operations, "uncovered": uncovered,
                        "batches": order_batches(operations, selected["plan"]["map_id"]),
                        "native_eligibility": "not_checked", "writes_dispatched": False}
            if action not in {"suggest", "adopt", "revise", "intent", "resources", "policy", "assess_purpose", "observe", "revoke"}:
                raise ValueError("Unknown colony work action")
            if action != "suggest" and session.role != "planner":
                raise ValueError("This role may only preview or suggest; adoption requires the colony planner")
            if action == "assess_purpose":
                from ...workspace.purpose_evidence import evidence_ref

                state = await service.call("colony_work", game.colony_id, game.load_epoch, identity[0])
                plan = next((row for row in state["construction"]["plans"] if row["plan_id"] == values.get("plan_id")), None)
                if plan is None:
                    raise ValueError("Assess an adopted plan")
                if "evidence_refs" in values or "observations" in values:
                    raise ValueError("Purpose facts come only from this run's original native observations")
                for identifier in values.get("evidence_ids", []):
                    original = next((row for row in session.observations if row.get("observation_id") == identifier), None)
                    if original is None:
                        raise ValueError("Purpose evidence must have been read in this run")
                    evidence_ref(original, identity=identity, map_id=plan["plan"]["map_id"], allow_failed=values.get("judgment") == "unknown")
            if action == "resources":
                if "snapshot" in values:
                    raise ValueError("Use snapshot_observation_id and resource_sources; a model-authored stock snapshot is not evidence")
                values["snapshot"] = resource_snapshot(session, identity,
                    values.pop("snapshot_observation_id", None), values.pop("resource_sources", None))
            elif action == "policy":
                if "current" in values or "protections" in values:
                    raise ValueError("Policy members and protections come from native evidence and original user protections")
                policy_observation_id = values.pop("current_observation_id", None)
                data = _evidence(session, identity, policy_observation_id, "inspect_food_policies")
                policy_id = values["permission"]["policy_id"]
                policy = next((row for row in data.get("objects", []) if row.get("policy_id") == policy_id), None)
                if policy is None:
                    raise ValueError("The policy was not returned in the native observation")
                values["current"] = {"policy_id": policy_id, "scope_id": "food_policy:" + policy_id,
                                     "members": policy["users"], "complete": policy.get("users_complete") is True,
                                     "observation_id": policy_observation_id, "load_epoch": game.load_epoch,
                                     "game_tick": next(row["response"]["game_tick"] for row in session.observations
                                                       if row["observation_id"] == policy_observation_id)}
                events = await service.call("events_by_kind", game.colony_id,
                                            ("user.policy_protection", "user.policy_protection_released"))
                released = {row["payload"].get("protection_seq") for row in events
                            if row["kind"] == "user.policy_protection_released" and row["source"] == "frontdesk"}
                values["protections"] = [row["payload"]["protection"] for row in events
                                         if row["kind"] == "user.policy_protection" and row["source"] == "frontdesk"
                                         and row["seq"] not in released]
            directory = (setup.directory or {}).get("colony_observation", {})
            supported = directory.get("supported_kinds", ())
            if action == "observe" and not supported:
                raise ToolExecutionError("unsupported", "Native bounded observation is unavailable")
            result = await service.call("colony_work_command", game.colony_id, game.load_epoch,
                                      service.game_session().game_tick, session.role, action, values,
                                      connection_epoch=identity[0], supported_kinds=supported,
                                      command_binding_check=session.binding_check)
            if action in {"suggest", "adopt", "revise"} and attachments and session.media_store is not None:
                session.media_store.retain(owner, attachments)
            from ...workspace.work_state import summary
            return {"event_seq": result["event_seq"], "colony_work": summary(result["colony_work"])}
        except (ValueError, KeyError, TypeError) as error:
            raise ToolExecutionError("colony_work_invalid", str(error)) from error

    return (ToolSpec("colony_work", "Read durable colony work; preview a house using current native catalog and spatial evidence, "
        "or save a read-only suggestion. Only the existing planner may adopt/revise an explicit plan or record operating decisions. "
        "These commands do not issue native game orders: execute accepted changes through the existing dry_run/commit tools. "
        "Arguments: read {} for a compact index or {section,id?} for original domain data; preview {plan,catalog_observation_id,spatial_observation_id?}; "
        "suggest adds suggestion_id,reason; adopt {task_id,plan_id,plan,source,catalog_observation_id,spatial_observation_id?}; "
        "or adopt {task_id,plan_id,suggestion_event_seq,catalog_observation_id,spatial_observation_id?} explicitly chooses one retained proposal without copying its whole drawing; "
        "revise {task_id,plan_id,expected_revision,plan,catalog_observation_id,spatial_observation_id?}; "
        "intent {task_id,plan_id,element_ids,intent,reason,source_seq?}; "
        "orders {task_id,plan_id,element_ids,area_ids?,areas_observation_id?,mode?:place|cancel|restart,reason?} returns native-coordinate operations and preview arguments; "
        "cancel requires recorded planner cancellation, restart requires explicit reason and continuing intent after a known cancelled/destroyed generation; "
        "with no game dispatch; roof/home additions patch only selected native cells in groups of at most 64, "
        "retaining other cells and leaving an opposing roof instruction for a separate explicit authorized decision. "
        "assess_resources {snapshot_observation_id,resource_sources,demands,allocation_id?} compares fresh stock with a saved decision; "
        "resources {task_id,allocation_id,expected_revision,snapshot_observation_id,resource_sources,demands,shares,window,reason}; "
        "stock comes only from the selected observed source buckets; trader goods and held goods have distinct scopes. "
        "policy {task_id,impact_id,expected_revision,permission,current_observation_id,action?,pawn_ids?}; "
        "permission.scope_id must be food_policy:<policy_id>; current members and user protections are read internally. "
        "assess_purpose {task_id,plan_id,expected_revision,goal,judgment:supported|unmet|unknown,findings:[text],evidence_ids:[current-run native IDs]} "
        "records a dated planner assessment of the selected purpose; it never certifies native facts, completes construction, or grants write authority. "
        "observe {task_id,observation_id,expected_revision,conditions}; revoke {task_id,observation_id,expected_revision,reason}. "
        "Observation conditions follow the native colony_observation directory. Suggestions confer no execution authority; "
        "placement is distinct from finished construction and purpose evaluation.",
        {"type": "object", "properties": {"action": {"type": "string", "enum": [
        "read", "preview", "suggest", "adopt", "revise", "intent", "orders", "assess_resources", "resources", "policy", "assess_purpose", "observe", "revoke"]},
            "arguments": {"type": "object"}}, "required": ["action", "arguments"], "additionalProperties": False},
        work, exclusive=True),)
