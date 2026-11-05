"""Domain decisions and internal receipts applied to the colony's durable work.

Writers run inside the transaction the journal opened.
"""

import copy

from . import construction, observations, policy_scope, resource_allocation, work_state
from .ledger import RETURN_EVENT, TAKEOVER_EVENT
from .purpose_evidence import evidence_ref


def construction_dispatch_authority(ledger, colony_id, load_epoch, plan_id, element_ids):
    return construction.assert_dispatch_authority(ledger.events_by_kind(colony_id, work_state.KINDS), plan_id=plan_id,
                                                  element_ids=element_ids, load_epoch=load_epoch)


def colony_work_command(ledger, colony_id, load_epoch, game_tick, actor, command, arguments,
                        *, connection_epoch=None, supported_kinds=()):
    """Apply a domain decision under the current board task.

    Native facts and coverage use separate internal writers. A model can
    request observation, but cannot report that it was installed or built.
    """
    if not isinstance(arguments, dict):
        raise ValueError("Colony work arguments must be an object")
    args = copy.deepcopy(arguments)
    render_spec = args.pop("render_spec", None)
    attachments = args.pop("attachments", [])
    game_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    events = ledger.events_by_kind(colony_id, work_state.KINDS)
    if command == "suggest":
        if actor not in {"frontdesk", "planner", "investigator"}:
            raise ValueError("A suggestion requires an agent source")
        args["attachments"] = attachments
        if set(args) != {"suggestion_id", "plan", "reason", "attachments"}:
            raise ValueError("A suggestion requires its ID, plan, reason and attachments")
        if (not isinstance(args["suggestion_id"], str) or not args["suggestion_id"].strip()
                or not isinstance(args["plan"], dict) or not isinstance(args["reason"], str)
                or not args["reason"].strip() or not isinstance(args["attachments"], list)):
            raise ValueError("Invalid construction suggestion")
        spec = {"kind": "construction.suggestion", "source": actor, "payload": {**args, "render_spec": render_spec}}
    else:
        if actor != "planner":
            raise ValueError("Only the colony planner adopts or changes colony work")
        task_id = args.get("task_id")
        task = ledger.revision(task_id) if isinstance(task_id, str) else None
        if (task is None or task["colony_id"] != colony_id or task["kind"] != "task"
                or (task["status"] == "closed" and command != "revoke")
                or (task["status"] == "verified" and command not in {"revoke", "assess_purpose"})):
            raise ValueError("Colony work requires a current open planner task")
        if command == "adopt":
            if args.get("source", {}).get("event_seq") is not None:
                source = ledger.event(colony_id, args["source"]["event_seq"])
                if source is None:
                    raise ValueError("The adoption source is not retained in this colony")
            spec = construction.prepare_adoption(events, load_epoch=load_epoch, **args)
        elif command == "revise":
            plan = next((row for row in construction.project(events, load_epoch=load_epoch)["plans"]
                         if row["plan_id"] == args.get("plan_id")), None)
            if plan is None or plan["task_id"] != task_id:
                raise ValueError("A construction revision must use its adopted planner task")
            args.pop("task_id")
            spec = construction.prepare_revision(events, load_epoch=load_epoch, **args)
        elif command == "intent":
            plan = next((row for row in construction.project(events, load_epoch=load_epoch)["plans"]
                         if row["plan_id"] == args.get("plan_id")), None)
            if plan is None or plan["task_id"] != task_id:
                raise ValueError("An element intent must use its adopted planner task")
            release = ledger.event(colony_id, args.get("source_seq")) if type(args.get("source_seq")) is int else None
            takeover = (ledger.event(colony_id, release["payload"].get("takeover_seq"))
                        if release and release["kind"] == RETURN_EVENT else None)
            selected = [row for row in plan["elements"] if row["id"] in args.get("element_ids", [])
                        and row["intent"] in construction.STOPPED and (row.get("stop") or {}).get("source") != "planner"]
            args["validated_release"] = bool(takeover and takeover["kind"] == TAKEOVER_EVENT and selected
                and all((row.get("stop") or {}).get("scope") == {
                    "object": takeover["payload"]["object"], "field": takeover["payload"]["field"]} for row in selected))
            args.pop("task_id")
            spec = construction.prepare_intent(events, load_epoch=load_epoch, **args)
        elif command == "resources":
            existing = next((row for row in resource_allocation.project(events)["decisions"]
                             if row["allocation_id"] == args.get("allocation_id")), None)
            if existing is not None and existing["task_id"] != task_id:
                raise ValueError("A resource allocation remains attached to its planner task")
            spec = resource_allocation.prepare_selection(events, **args)
        elif command == "policy":
            existing = next((row for row in policy_scope.project(events)["impacts"]
                             if row["impact_id"] == args.get("impact_id")), None)
            if existing is not None and existing["task_id"] != task_id:
                raise ValueError("A policy impact remains attached to its planner task")
            spec = policy_scope.prepare_impact(events, **args)
        elif command == "assess_purpose":
            plan = next((row for row in construction.project(events, load_epoch=load_epoch)["plans"]
                         if row["plan_id"] == args.get("plan_id")), None)
            if plan is None or plan["task_id"] != task_id:
                raise ValueError("Purpose assessment requires its adopted planner task")
            spec = construction.prepare_purpose_assessment(events, load_epoch=load_epoch, **args)
            evidence = ledger.native_work_evidence(colony_id, args["evidence_ids"])
            originals = {}
            for event in evidence:
                original = event["payload"]["observation"]
                identifier = original["observation_id"]
                if identifier in originals and originals[identifier]["payload"]["observation"] != original:
                    raise ValueError("Purpose evidence has conflicting original observations")
                if event["source"] not in {"planner", "frontdesk"} or event["load_epoch"] != load_epoch:
                    raise ValueError("Purpose evidence must be retained native work from this epoch")
                originals[identifier] = event
            if set(originals) != set(args["evidence_ids"]):
                raise ValueError("Purpose evidence must cite retained original native observations")
            minimum_tick = max((event["game_tick"] for event in events if event["load_epoch"] == load_epoch
                and (event["kind"] in {"colony.observed", "colony.load"}
                     or event["kind"] in {"construction.adopted", "construction.revised", "construction.fact", "construction.restarted"}
                     and event["payload"].get("plan_id") == plan["plan_id"]
                     and event["payload"].get("revision") == plan["revision"])), default=0)
            refs = []
            for identifier in args["evidence_ids"]:
                original = originals[identifier]
                ref = evidence_ref(original["payload"]["observation"], identity=(connection_epoch, colony_id, load_epoch),
                    map_id=plan["plan"]["map_id"], minimum_tick=minimum_tick, allow_failed=args["judgment"] == "unknown")
                refs.append({**ref, "source_event_seq": original["seq"]})
            spec["payload"]["evidence_refs"] = refs
        elif command == "observe":
            binding = {"colony_id": colony_id, "load_epoch": load_epoch,
                       "connection_epoch": connection_epoch}
            existing = next((row for row in observations.project(events, binding=binding)["intents"]
                             if row["observation_id"] == args.get("observation_id")), None)
            if existing is not None and existing["task_id"] != task_id:
                raise ValueError("An observation remains attached to its planner task")
            plans = construction.project(events, load_epoch=load_epoch)["plans"]
            for condition in args.get("conditions", []):
                if condition.get("kind") == "construction":
                    plan = next((row for row in plans if row["plan_id"] == condition.get("plan_id")), None)
                    if (plan is None or plan["task_id"] != task_id or plan["plan"]["map_id"] != condition.get("map_id")
                            or not set(condition.get("target_ids", [])).issubset({row["id"] for row in plan["elements"]})):
                        raise ValueError("Construction observation must name this task's adopted plan and current elements")
            spec = observations.prepare_request(events, supported_kinds=supported_kinds,
                                                binding=binding, now_tick=game_tick, **args)
        elif command == "revoke":
            binding = {"colony_id": colony_id, "load_epoch": load_epoch, "connection_epoch": connection_epoch}
            existing = next((row for row in observations.project(events, binding=binding)["intents"]
                             if row["observation_id"] == args.get("observation_id")), None)
            if existing is None or existing["task_id"] != task_id:
                raise ValueError("Revoke the observation from its own planner task")
            args.pop("task_id")
            spec = observations.prepare_revoke(events, **args)
        else:
            raise ValueError("Unknown colony work command")
    seq = ledger.append(colony_id, load_epoch, game_tick, spec["source"], spec["kind"], spec["payload"])
    if command == "revise":
        binding = {"colony_id": colony_id, "load_epoch": load_epoch, "connection_epoch": connection_epoch or "unavailable"}
        for intent in observations.project(events, binding=binding)["intents"]:
            if intent["active"] and any(condition.get("kind") == "construction"
                    and condition.get("plan_id") == spec["payload"]["plan_id"] for condition in intent["conditions"]):
                revoke = observations.prepare_revoke(events, observation_id=intent["observation_id"],
                    expected_revision=intent["revision"], reason="construction_plan_revised")
                ledger.append(colony_id, load_epoch, game_tick, revoke["source"], revoke["kind"], revoke["payload"])
    if command in {"adopt", "revise"} and render_spec is not None:
        ledger.append(colony_id, load_epoch, game_tick, actor, "construction.preview",
                      {"plan_id": spec["payload"]["plan_id"], "revision": spec["payload"]["revision"],
                       "render_spec": render_spec, "attachments": attachments})
    return {"event_seq": seq, "colony_work": work_state.current(ledger, colony_id, load_epoch, connection_epoch)}


def observation_coverage(ledger, colony_id, load_epoch, game_tick, connection_epoch, **receipt):
    tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    binding = {"colony_id": colony_id, "load_epoch": load_epoch, "connection_epoch": connection_epoch}
    events = ledger.events_by_kind(colony_id, work_state.KINDS)
    current = next((row for row in observations.project(events, binding=binding)["intents"]
                    if row["observation_id"] == receipt.get("observation_id")), None)
    if current is None or not current["active"] or current["revision"] != receipt.get("revision"):
        return None
    spec = observations.prepare_coverage(events, binding=binding, **receipt)
    return ledger.append(colony_id, load_epoch, tick, spec["source"], spec["kind"], spec["payload"])


def record_resource_observation(ledger, colony_id, load_epoch, game_tick, **arguments):
    tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    if arguments.get("snapshot", {}).get("load_epoch") != load_epoch:
        raise ValueError("Resource snapshot must belong to the current epoch")
    spec = resource_allocation.prepare_observation(ledger.events_by_kind(colony_id, resource_allocation.KINDS), **arguments)
    return ledger.append(colony_id, load_epoch, tick, spec["source"], spec["kind"], spec["payload"])
