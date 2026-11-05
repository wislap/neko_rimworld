"""The colony's durable construction, operating decisions and observation intent.

This is one projection of the existing event log, shared by the model and UI.
It has no dispatch or authority of its own.
"""

from . import construction, observations, policy_scope, resource_allocation

KINDS = (
    "construction.adopted", "construction.revised", "construction.intent", "construction.fact", "construction.restarted",
    "construction.purpose_assessed",
    "resource.allocation_selected", "resource.observed", "policy.impact_selected", "observation.requested", "observation.revoked",
    "observation.coverage", "observation.fact", "construction.suggestion", "autoplay.activation", "autoplay.disabled",
    "construction.preview", "construction.batch", "user.return", "user.takeover",
    "colony.observed", "colony.load",
)


def project(events, *, load_epoch, connection_epoch=None, colony_id=None):
    lifecycle = [e for e in events if e["kind"] in {"autoplay.activation", "autoplay.disabled"}]
    enabled = bool(lifecycle and lifecycle[-1]["kind"] == "autoplay.activation")
    binding = {"colony_id": colony_id, "load_epoch": load_epoch, "connection_epoch": connection_epoch or "unavailable"}
    projects = construction.project(events, load_epoch=load_epoch)
    for plan in projects["plans"]:
        previews = [e["payload"] for e in events if e["kind"] == "construction.preview"
                    and e["load_epoch"] == load_epoch and e["payload"].get("plan_id") == plan["plan_id"]
                    and e["payload"].get("revision") == plan["revision"]]
        if previews:
            plan.update(render_spec=previews[-1]["render_spec"], attachments=previews[-1]["attachments"])
        attempts = [{"event_seq": e["seq"], **e["payload"]} for e in events if e["kind"] == "construction.batch"
                   and e["load_epoch"] == load_epoch and e["payload"].get("plan_id") == plan["plan_id"]
                   and e["payload"].get("revision") == plan["revision"]]
        batches = list({row["changeset_id"]: row for row in attempts}.values())
        committed = {identity for row in batches if row["status"] == "committed" for identity in row["element_ids"]}
        expected = {row["id"] for row in plan["plan"]["elements"]}
        plan["execution"] = {"batches": batches, "committed_element_ids": sorted(committed),
                             "rejected_batches": sum(row["status"] == "rejected" for row in batches),
                             "unknown_batches": sum(row["status"] == "unknown" for row in batches),
                             "status": "not_dispatched" if not batches else "unknown" if any(row["status"] == "unknown" for row in batches)
                             else "partial" if committed and (committed != expected or any(row["status"] == "rejected" for row in batches))
                             else "committed" if committed else "rejected"}
    policies = policy_scope.project(events)
    for impact in policies["impacts"]:
        impact["evidence_status"] = "current_epoch" if impact["current"].get("load_epoch") == load_epoch else "historical"
    return {"format": "colony_work.v1", "load_epoch": load_epoch,
            "construction": projects,
            "resources": resource_allocation.project(events, load_epoch=load_epoch),
            "policies": policies,
            "observations": observations.project(events, binding=binding, enabled=enabled),
            "suggestions": [{"event_seq": e["seq"], "load_epoch": e["load_epoch"],
                             **e["payload"]} for e in events if e["kind"] == "construction.suggestion"]}


def summary(state):
    """An index for routine planner input; full geometry/facts stay on demand."""
    return {"format": state["format"], "load_epoch": state["load_epoch"],
            "construction": {"plans": [{key: row[key] for key in (
                "plan_id", "task_id", "revision", "counts", "phase", "construction_complete", "placement_complete", "purpose_status")}
                for row in state["construction"]["plans"]]},
            "resources": {"decisions": [{key: row[key] for key in ("allocation_id", "task_id", "revision", "window", "evidence_status", "assessment")}
                                         for row in state["resources"]["decisions"]]},
            "policies": {"impacts": [{key: row[key] for key in ("impact_id", "task_id", "revision", "evidence_status", "assessment")}
                                     for row in state["policies"]["impacts"]]},
            "observations": {"coverage": [{key: row[key] for key in (
                "observation_id", "revision", "status", "covered_condition_ids", "uncovered_condition_ids")}
                for row in state["observations"]["coverage"]]},
            "suggestions": [{"event_seq": row["event_seq"], "suggestion_id": row["suggestion_id"],
                             "load_epoch": row["load_epoch"], "reason": row["reason"]} for row in state["suggestions"]]}
