"""Adopted construction intent and native evidence, rebuilt from the colony log.

The caller appends prepared events in its Journal transaction and checks planner
authority there. Native evidence is a separate, trusted ingestion path; a drawing
or a planner statement cannot establish that an object exists in the world.
"""

import hashlib
import json
from copy import deepcopy

from .ledger import RETURN_EVENT, TAKEOVER_EVENT, is_integer

FORMAT = "construction_state.v1"
KINDS = frozenset({"construction.adopted", "construction.revised", "construction.intent", "construction.fact", "construction.restarted", "construction.purpose_assessed"})
STAGES = frozenset({"blueprint", "frame", "designated", "built", "missing", "destroyed", "model_cancelled", "user_cancelled", "user_replaced"})
STOPPED = frozenset({"cancelled", "taken_over"})


def _identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _plan(plan):
    if not isinstance(plan, dict) or plan.get("format") != "construction_plan.v1" or plan.get("compiled") is not True:
        raise ValueError("A compiled construction_plan.v1 is required")
    _identity(plan.get("map_id"), "map_id")
    origin = plan.get("origin", {})
    if any(not is_integer(origin.get(axis)) for axis in ("x", "z")):
        raise ValueError("Plan origin must contain integer x,z")
    rows = [*plan.get("elements", []), *plan.get("areas", [])]
    if not rows:
        raise ValueError("An adopted plan must contain elements or areas")
    ids = [_identity(row.get("id"), "element id") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Element and area ids must be unique")
    for row in plan.get("elements", []):
        _identity(row.get("definition"), "definition")
        if any(not is_integer(row.get("position", {}).get(axis)) for axis in ("x", "z")):
            raise ValueError("Element position must contain integer x,z")
        if type(row.get("rotation")) is not int or row["rotation"] not in range(4):
            raise ValueError("Element rotation must be 0..3")
    for row in plan.get("areas", []):
        _identity(row.get("kind"), "area kind")
        if not isinstance(row.get("cells"), list) or not row["cells"] or any(not isinstance(cell, dict) or any(not is_integer(cell.get(axis)) for axis in ("x", "z")) for cell in row["cells"]):
            raise ValueError("Plan areas require explicit integer cells")
    # Sorting object keys makes plan identity independent of JSON object order.
    encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return deepcopy(plan), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event(kind, actor, **payload):
    return {"kind": kind, "source": actor, "payload": {"format": FORMAT, **deepcopy(payload)}}


def _rows(plan):
    return {row["id"]: row for row in [*plan.get("elements", []), *plan.get("areas", [])]}


def _target(plan, row):
    """An intended target in a drawing, never an inferred native object identity."""
    origin = plan["origin"]
    if "position" in row:
        target = {field: row.get(field) for field in ("definition", "rotation", "stuff")}
        target["layer"] = row.get("layer", "building")
        target["position"] = {axis: origin[axis] + row["position"][axis] for axis in ("x", "z")}
    else:
        target = {"kind": row["kind"], "cells": sorted((origin["x"] + cell["x"], origin["z"] + cell["z"]) for cell in row["cells"])}
    return json.dumps({"map_id": plan["map_id"], **target}, sort_keys=True, separators=(",", ":"))


def _purpose_basis(current):
    """Relevant construction state, excluding identical observation refreshes."""
    basis = {"revision": current["revision"], "plan_hash": current["plan_hash"], "elements": []}
    elements = current.get("elements")
    if elements is None:
        elements = [current["_elements"][identity] for identity in _rows(current["plan"]) if identity in current["_elements"]]
    for element in elements:
        basis["elements"].append({**{field: element.get(field) for field in ("id", "intent", "stage", "binding", "generation")},
                                  "native_action_id": (element.get("stop") or {}).get("native_action_id"),
                                  "fact_epoch": (element.get("fact") or {}).get("load_epoch")})
    encoded = json.dumps(basis, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def project(events, *, load_epoch):
    """Return current revision fulfillment; historical evidence survives as stale.

    No absent/unknown/destroyed state grants permission to dispatch a replacement.
    The native write coordinator remains responsible for world preconditions.
    """
    plans = {}
    takeovers = {}
    for event in events:
        kind, payload = event.get("kind"), event.get("payload", {})
        if kind == TAKEOVER_EVENT:
            takeovers[event.get("seq")] = payload
            continue
        if kind == RETURN_EVENT and event.get("source") in {"user", "frontdesk"}:
            takeover = takeovers.get(payload.get("takeover_seq"))
            if takeover:
                for current in plans.values():
                    for element in current["_elements"].values():
                        stop = element.get("stop")
                        scope = {"object": takeover.get("object"), "field": takeover.get("field")}
                        scopes = (stop.get("scopes") or ([stop["scope"]] if stop.get("scope") else [])) if stop else []
                        if scope in scopes:
                            returns = stop.setdefault("scope_returns", [])
                            if not any({"object": row["object"], "field": row["field"]} == scope for row in returns):
                                returns.append({**scope, "return_seq": event.get("seq")})
                            if all(any(row["object"] == selected["object"] and row["field"] == selected["field"] for row in returns) for selected in scopes):
                                stop["released_by"] = event.get("seq")
            continue
        if kind not in KINDS or payload.get("format") != FORMAT:
            continue
        plan_id = payload["plan_id"]
        if kind == "construction.adopted":
            plans[plan_id] = {"plan_id": plan_id, "task_id": payload["task_id"], "revision": 1,
                              "plan": deepcopy(payload["plan"]), "plan_hash": payload["plan_hash"],
                              "source": deepcopy(payload.get("source", {})), "history": [], "_elements": {}, "state_version": 0}
        if plan_id not in plans:
            continue
        current = plans[plan_id]
        previous_basis = _purpose_basis(current)
        current["history"].append({"kind": kind, "seq": event.get("seq"), "revision": payload.get("revision"),
                                   "load_epoch": event.get("load_epoch", payload.get("load_epoch"))})
        if kind in {"construction.adopted", "construction.revised"}:
            old = _rows(current["plan"]) if kind == "construction.revised" else {}
            old_origin = current["plan"]["origin"]
            current.update(revision=payload["revision"], plan=deepcopy(payload["plan"]), plan_hash=payload["plan_hash"])
            for identity, definition in _rows(current["plan"]).items():
                if identity not in current["_elements"] or old.get(identity) != definition or old_origin != current["plan"]["origin"]:
                    # A changed geometry/definition needs new evidence. A stop remains
                    # in force until the explicit user-return path releases it.
                    previous = current["_elements"].get(identity, {})
                    current["_elements"][identity] = {"id": identity, "intent": previous.get("intent", "continue"),
                                                       "stage": "unobserved", "fact": None,
                                                       "stop": deepcopy(previous.get("stop")), "generation": previous.get("generation", 1)}
        elif kind == "construction.purpose_assessed" and event.get("source") == "planner":
            current["_purpose"] = {**deepcopy(payload), "assessed_seq": event.get("seq"),
                                   "assessed_tick": event.get("game_tick"),
                                   "event_load_epoch": event.get("load_epoch", payload["load_epoch"])}
        elif payload.get("revision") == current["revision"]:
            if kind == "construction.intent":
                for identity in payload["element_ids"]:
                    element = current["_elements"][identity]
                    element["intent"] = payload["intent"]
                    element["intent_reason"] = payload["reason"]
                    if payload["intent"] == "continue":
                        if element.get("stop") and element["stop"]["source"] == "native":
                            if payload.get("source_seq") is not None:
                                element["stop"]["released_by"] = payload["source_seq"]
                        else:
                            element["stop"] = None
                    elif not element.get("stop") or element["stop"]["source"] != "native":
                        element["stop"] = {"source": "planner", "object_id": element.get("binding", {}).get("object_id"),
                                           "reason": payload["reason"], "seq": event.get("seq")}
            elif kind in {"construction.fact", "construction.restarted"} and payload["element_id"] in _rows(current["plan"]):
                element = current["_elements"][payload["element_id"]]
                if kind == "construction.restarted":
                    element["generation"] = payload["generation"]
                element.update(stage=payload["stage"], fact={**deepcopy(payload), "seq": event.get("seq"),
                                                           "load_epoch": event.get("load_epoch", payload["load_epoch"])})
                if payload["stage"] in {"blueprint", "frame", "designated", "built"}:
                    element["binding"] = {"object_id": payload["object_id"], "stage": payload["stage"]}
                if payload["stage"] in {"user_cancelled", "user_replaced"}:
                    stop = {"source": "native", "object_id": payload.get("object_id") or element.get("binding", {}).get("object_id"),
                            "reason": payload["stage"], "seq": event.get("seq"), "generation": element["generation"],
                            "native_action_id": payload.get("native_action_id"),
                            "target": json.loads(_target(current["plan"], _rows(current["plan"])[payload["element_id"]])),
                            "scope": deepcopy(payload.get("field_identity")),
                            "scopes": deepcopy(payload.get("field_identities") or ([payload["field_identity"]] if payload.get("field_identity") else [])),
                            "scope_returns": []}
                    previous_stop = element.get("stop") or {}
                    same_stop = all(previous_stop.get(field) == stop[field] for field in ("source", "object_id", "reason", "generation", "target", "native_action_id")) and sorted(
                        (scope["object"], scope["field"]) for scope in previous_stop.get("scopes", [])) == sorted(
                        (scope["object"], scope["field"]) for scope in stop["scopes"])
                    if not same_stop:
                        element["intent"] = "cancelled" if payload["stage"] == "user_cancelled" else "taken_over"
                        element["stop"] = stop
                elif payload["stage"] == "model_cancelled":
                    first_cancellation = element.get("model_cancelled_generation") != element["generation"]
                    element["model_cancelled_generation"] = element["generation"]
                    if first_cancellation and (not element.get("stop") or element["stop"]["source"] != "native" or element["stop"].get("released_by")):
                        element["intent"] = "cancelled"
                        element["stop"] = {"source": "planner", "object_id": element.get("binding", {}).get("object_id"),
                                           "reason": "model_cancelled", "seq": event.get("seq")}
        if _purpose_basis(current) != previous_basis:
            current["state_version"] += 1
    output = []
    for current in plans.values():
        current["protected_targets"] = [{"element_id": identity, **deepcopy(element["stop"])}
                                         for identity, element in current["_elements"].items()
                                         if element.get("stop") and element["stop"]["source"] == "native" and not element["stop"].get("released_by")]
        elements = []
        for identity in _rows(current["plan"]):
            element = deepcopy(current["_elements"][identity])
            fact = element["fact"]
            element["fresh"] = bool(fact and fact["load_epoch"] == load_epoch)
            element["object_id"] = fact.get("object_id") if fact else None
            element["fulfilled"] = element["intent"] == "continue" and element["fresh"] and element["stage"] == "built"
            element["continuation"] = ("stopped" if element["intent"] in STOPPED else
                                       "reconcile" if not element["fresh"] or element["stage"] in {"missing", "destroyed", "unobserved", "model_cancelled", "user_cancelled", "user_replaced"} else
                                       "native_progress" if element["stage"] in {"blueprint", "frame", "designated"} else "satisfied")
            elements.append(element)
        current.pop("_elements")
        current["elements"] = elements
        current["counts"] = {"required": len(elements), "fulfilled": sum(row["fulfilled"] for row in elements),
                             "stopped": sum(row["intent"] in STOPPED for row in elements),
                             "unknown": sum(not row["fresh"] or row["stage"] in {"missing", "unobserved"} for row in elements)}
        current["construction_complete"] = bool(elements) and all(row["fulfilled"] for row in elements)
        current["placement_complete"] = bool(elements) and all(row["fresh"] and row["intent"] == "continue" and
                                                              row["stage"] in {"blueprint", "frame", "designated", "built"} for row in elements)
        current["purpose_status"] = "not_evaluated"
        assessment = current.pop("_purpose", None)
        if assessment is not None:
            reasons = []
            if assessment["revision"] != current["revision"]:
                reasons.append("revision_changed")
            if assessment["task_id"] != current["task_id"]:
                reasons.append("task_changed")
            if assessment["load_epoch"] != load_epoch or assessment["event_load_epoch"] != load_epoch:
                reasons.append("load_epoch_changed")
            if assessment["basis_fingerprint"] != _purpose_basis(current) or assessment["facts_version"] != current["state_version"]:
                reasons.append("related_facts_changed")
            assessment.update(stale=bool(reasons), stale_reasons=reasons, freshness="stale" if reasons else "current",
                              current_revision=current["revision"], current_load_epoch=load_epoch)
            current["purpose_status"] = "stale" if reasons else assessment["judgment"]
        current["purpose_assessment"] = assessment
        current["phase"] = "complete" if current["construction_complete"] else "stopped" if current["counts"]["stopped"] else "in_progress" if current["placement_complete"] else "reconcile"
        output.append(current)
    return {"plans": output}


def _current(events, plan_id, load_epoch):
    for plan in project(events, load_epoch=load_epoch)["plans"]:
        if plan["plan_id"] == plan_id:
            return plan
    raise ValueError("Unknown adopted plan")


def prepare_adoption(events, *, plan_id, task_id, plan, source, load_epoch):
    _identity(plan_id, "plan_id")
    _identity(task_id, "task_id")
    _identity(load_epoch, "load_epoch")
    if any(row["plan_id"] == plan_id for row in project(events, load_epoch=load_epoch)["plans"]):
        raise ValueError("Plan is already adopted")
    compiled, plan_hash = _plan(plan)
    if compiled.get("plan_id", plan_id) != plan_id:
        raise ValueError("Compiled plan identity must match the adopted plan")
    if not isinstance(source, dict):
        raise ValueError("Adoption source must be a provenance object")
    return _event("construction.adopted", "planner", plan_id=plan_id, task_id=task_id, revision=1,
                  plan=compiled, plan_hash=plan_hash, source=source, load_epoch=load_epoch)


def prepare_purpose_assessment(events, *, plan_id, task_id, expected_revision, load_epoch, goal, judgment, findings, evidence_ids):
    """Persist the planner's evidenced purpose judgment without certifying it.

    Journal validates native evidence ownership, epoch, scope and retention.
    This function validates the business binding and never marks construction
    complete. The judgment remains explicitly tied to the observed state.
    """
    events = list(events)
    current = _current(events, plan_id, load_epoch)
    _identity(task_id, "task_id")
    _identity(load_epoch, "load_epoch")
    _identity(goal, "goal")
    if task_id != current["task_id"] or type(expected_revision) is not int or expected_revision != current["revision"]:
        raise ValueError("Purpose assessment requires the current adopted task and plan revision")
    authority = [event for event in events if event.get("kind") in {"colony.load", "colony.observed", "autoplay.activation", "game.event"}
                 or event.get("kind") in {"construction.fact", "construction.restarted"} and event.get("source") == "mod"]
    if authority:
        latest_epoch = authority[-1].get("load_epoch", authority[-1].get("payload", {}).get("load_epoch"))
        if latest_epoch is not None and latest_epoch != load_epoch:
            raise ValueError("Purpose assessment cannot use an older world epoch")
    if judgment not in {"supported", "unmet", "unknown"}:
        raise ValueError("Purpose judgment must be supported, unmet or unknown")
    if not isinstance(findings, list) or not findings or any(not isinstance(row, str) or not row.strip() for row in findings):
        raise ValueError("Purpose assessment requires nonempty findings")
    if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(identity, str) or not identity.strip() for identity in evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("Purpose assessment requires unique retained native evidence ids")
    facts_through_seq = max((row["seq"] for row in current["history"] if row["kind"] in {"construction.fact", "construction.restarted"} and is_integer(row.get("seq"))), default=0)
    return _event("construction.purpose_assessed", "planner", plan_id=plan_id, task_id=task_id, revision=expected_revision,
                  load_epoch=load_epoch, source="planner_assessment", goal=goal, judgment=judgment,
                  findings=findings, evidence_ids=evidence_ids, facts_through_seq=facts_through_seq,
                  facts_version=current["state_version"], basis_fingerprint=_purpose_basis(current))


def prepare_revision(events, *, plan_id, expected_revision, plan, load_epoch):
    current = _current(events, plan_id, load_epoch)
    if type(expected_revision) is not int or current["revision"] != expected_revision:
        raise ValueError("Construction revision conflict")
    compiled, plan_hash = _plan(plan)
    if compiled.get("plan_id", plan_id) != plan_id:
        raise ValueError("Compiled plan identity must match the adopted plan")
    if compiled["map_id"] != current["plan"]["map_id"]:
        raise ValueError("A plan revision cannot change maps")
    previous_targets = {_target(current["plan"], row): identity for identity, row in _rows(current["plan"]).items()}
    for identity, row in _rows(compiled).items():
        previous_identity = previous_targets.get(_target(compiled, row))
        if previous_identity is not None and previous_identity != identity:
            raise ValueError("An unchanged construction target must preserve its stable element id")
    return _event("construction.revised", "planner", plan_id=plan_id, revision=expected_revision + 1,
                  expected_revision=expected_revision, plan=compiled, plan_hash=plan_hash, load_epoch=load_epoch)


def prepare_intent(events, *, plan_id, element_ids, intent, reason, load_epoch=None, source_seq=None, validated_release=False):
    current = _current(events, plan_id, load_epoch)
    if intent not in {"continue", *STOPPED}:
        raise ValueError("Unknown construction intent")
    _identity(reason, "reason")
    ids = list(element_ids)
    by_id = {row["id"]: row for row in current["elements"]}
    if not ids or len(set(ids)) != len(ids) or any(identity not in by_id for identity in ids):
        raise ValueError("Intent must name unique current elements")
    if intent == "continue" and any(by_id[identity]["intent"] in STOPPED and (by_id[identity].get("stop") or {}).get("source") != "planner"
                                     and not (by_id[identity].get("stop") or {}).get("released_by") for identity in ids):
        if any(len((by_id[identity].get("stop") or {}).get("scopes", [])) > 1 and not (by_id[identity].get("stop") or {}).get("released_by") for identity in ids):
            raise ValueError("Every native area scope requires an explicit matching user return")
        # Journal resolves the user.return takeover_seq to the same native object.
        releases = [event for event in events if event.get("seq") == source_seq and event.get("kind") == "user.return" and event.get("source") in {"user", "frontdesk"}]
        if not releases or validated_release is not True:
            raise ValueError("Stopped elements require an explicit matching user return")
    return _event("construction.intent", "planner", plan_id=plan_id, revision=current["revision"],
                  element_ids=ids, intent=intent, reason=reason, source_seq=source_seq)


def prepare_fact(events, *, plan_id, element_id, stage, object_id, observation_id, load_epoch,
                 previous_object_id=None, origin="native", field_identity=None, field_identities=None, native_action_id=None):
    current = _current(events, plan_id, load_epoch)
    elements = {row["id"]: row for row in current["elements"]}
    if element_id not in elements or stage not in STAGES or origin != "native":
        raise ValueError("Expected a current element and a supported native fact")
    is_area = element_id in {row["id"] for row in current["plan"].get("areas", [])}
    definition = _rows(current["plan"])[element_id]
    is_terrain = definition.get("layer") in {"floor", "terrain"}
    if native_action_id is not None:
        if not is_area or stage not in {"user_cancelled", "user_replaced"}:
            raise ValueError("Native manual action ids only identify player area cancellation/replacement facts")
        _identity(native_action_id, "native_action_id")
    if stage == "designated" and not is_area:
        raise ValueError("Cell designations belong to plan areas")
    if is_area and stage in {"blueprint", "frame"}:
        raise ValueError("Areas use designated/built cell evidence, not thing blueprints")
    _identity(observation_id, "observation_id")
    _identity(load_epoch, "load_epoch")
    if field_identities is not None and (not is_area or field_identity is not None):
        raise ValueError("Native identity lists are area-only and cannot accompany a single identity")
    if field_identities is not None and (not isinstance(field_identities, list) or not field_identities):
        raise ValueError("Native area identities must be a nonempty list")
    identities = field_identities if field_identities is not None else [field_identity] if field_identity is not None else []
    seen = set()
    for identity in identities:
        if not isinstance(identity, dict) or set(identity) != {"object", "field"}:
            raise ValueError("Native field identity must contain object and field")
        for name in ("object", "field"):
            _identity(identity[name], f"field_identity.{name}")
        key = (identity["object"], identity["field"])
        if key in seen:
            raise ValueError("Native area field identities must be unique")
        seen.add(key)
        if not is_area and identity["object"] != current["plan"]["map_id"]:
            raise ValueError("Native construction field identity must belong to the plan map")
    if stage in {"blueprint", "frame"} or stage == "built" and not (is_area or is_terrain):
        _identity(object_id, "object_id")
    previous = elements[element_id].get("binding")
    if previous and stage in {"blueprint", "frame", "built"}:
        prior_id = previous.get("object_id")
        if prior_id and object_id != prior_id:
            if previous_object_id != prior_id or (previous["stage"], stage) not in {("blueprint", "frame"), ("frame", "built"), ("blueprint", "built"), ("frame", "blueprint")}:
                raise ValueError("Object replacement needs explicit native lifecycle lineage")
        elif prior_id and previous["stage"] in {"blueprint", "frame", "built"} and {"blueprint": 0, "frame": 1, "built": 2}[stage] < {"blueprint": 0, "frame": 1, "built": 2}[previous["stage"]]:
            raise ValueError("Native lifecycle cannot move backwards")
    return _event("construction.fact", "mod", plan_id=plan_id, revision=current["revision"], element_id=element_id,
                  stage=stage, object_id=object_id, observation_id=observation_id, load_epoch=load_epoch,
                  previous_object_id=previous_object_id, origin=origin, field_identity=field_identity, field_identities=identities,
                  native_action_id=native_action_id, evidence_kind="cells" if is_area or is_terrain and stage == "built" else "thing")


def assert_dispatch_authority(events, *, plan_id, element_ids, load_epoch):
    """Reject reissuing a specifically cancelled target under another plan/id.

    This compares explicit drawing targets, not arbitrary occupied/blocked cells.
    A different building at that location is a different proposal. Native CAS,
    placement checks and the current user-intent guard still run at submission.
    """
    projects = project(events, load_epoch=load_epoch)["plans"]
    current = next((row for row in projects if row["plan_id"] == plan_id), None)
    if current is None:
        raise ValueError("Dispatch requires a formally adopted construction plan")
    ids = list(element_ids)
    elements = {row["id"]: row for row in current["elements"]}
    if not ids or len(ids) != len(set(ids)) or any(identity not in elements for identity in ids):
        raise ValueError("Dispatch must name unique current plan elements")
    if any(elements[identity]["intent"] != "continue" for identity in ids):
        raise ValueError("Dispatch cannot continue a stopped construction element")
    targets = {identity: json.loads(_target(current["plan"], _rows(current["plan"])[identity])) for identity in ids}
    protections = [stop for row in projects for stop in row["protected_targets"]]
    for identity, target in targets.items():
        for stop in protections:
            if stop.get("target") == target:
                raise ValueError(f"Construction target {identity} remains stopped by native user action")
    return {"plan_id": plan_id, "revision": current["revision"], "element_ids": ids, "checked_protections": len(protections)}


def prepare_restart(events, *, plan_id, element_id, load_epoch, previous_object_id, object_id, observation_id, field_identity=None):
    """Record an explicitly confirmed native restart as a new object generation.

    This trusted receipt path is separate from observation: seeing a same-cell
    new blueprint cannot prove that an authorized restart occurred.
    """
    current = _current(events, plan_id, load_epoch)
    element = next((row for row in current["elements"] if row["id"] == element_id), None)
    if element is None or element_id in {row["id"] for row in current["plan"].get("areas", [])}:
        raise ValueError("Restart requires a current building or floor element")
    if element["intent"] != "continue" or element["stage"] not in {"model_cancelled", "destroyed", "user_cancelled"}:
        raise ValueError("Restart requires explicit continued intent and a known cancelled/destroyed generation")
    if element["stage"] == "user_cancelled" and not (element.get("stop") or {}).get("released_by"):
        raise ValueError("Player cancellation must be returned before restart")
    _identity(previous_object_id, "previous_object_id")
    _identity(object_id, "object_id")
    _identity(observation_id, "observation_id")
    _identity(load_epoch, "load_epoch")
    if element.get("binding", {}).get("object_id") != previous_object_id or previous_object_id == object_id:
        raise ValueError("Restart must replace the exact known previous object with a new native object")
    assert_dispatch_authority(events, plan_id=plan_id, element_ids=[element_id], load_epoch=load_epoch)
    if field_identity is not None:
        if not isinstance(field_identity, dict) or set(field_identity) != {"object", "field"}:
            raise ValueError("Native field identity must contain object and field")
        for name in ("object", "field"):
            _identity(field_identity[name], f"field_identity.{name}")
        if field_identity["object"] != current["plan"]["map_id"]:
            raise ValueError("Native construction field identity must belong to the plan map")
    return _event("construction.restarted", "mod", plan_id=plan_id, revision=current["revision"], element_id=element_id,
                  stage="blueprint", object_id=object_id, previous_object_id=previous_object_id,
                  observation_id=observation_id, load_epoch=load_epoch, field_identity=field_identity,
                  generation=element["generation"] + 1, origin="native_restart_receipt", evidence_kind="thing")
