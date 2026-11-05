"""Shared policy permission and takeover impact, separate from content CAS.

An impact record describes the model's chosen scope and observed dependents. The
native mutation must repeat this check against actual dependents at apply time.
"""

from copy import deepcopy

FORMAT = "policy_scope.v1"
KINDS = frozenset({"policy.impact_selected"})


def _identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _members(value, name):
    if not isinstance(value, list) or any(not isinstance(identity, str) or not identity for identity in value) or len(value) != len(set(value)):
        raise ValueError(f"{name} must be a list of unique member ids")
    return set(value)


def check_scope(permission, current, *, protections=(), action="edit", pawn_ids=()):
    """Evaluate permitted effects; reference protection does not freeze content.

    permission: policy_id, scope_id, mode ('members' or 'shared'), members for a
    fixed member scope. current: policy_id, scope_id, members, complete. An
    effective_rules protection binds a person to the rules they actually use.
    """
    for name in ("policy_id", "scope_id"):
        _identity(permission.get(name), name)
        _identity(current.get(name), name)
    if permission.get("mode") not in {"members", "shared"} or action not in {"edit", "assign"}:
        raise ValueError("Expected a member/shared permission and edit/assign action")
    actual = _members(current.get("members"), "current.members")
    allowed = _members(permission.get("members", []), "permission.members")
    pawns = list(pawn_ids)
    _members(pawns, "pawn_ids")
    if action == "assign" and not pawns:
        raise ValueError("Assignment must identify affected people")
    affected = actual if action == "edit" else set(pawns)
    reasons = []
    if permission["policy_id"] != current["policy_id"] or permission["scope_id"] != current["scope_id"]:
        reasons.append("scope_identity_changed")
    if current.get("complete") is not True:
        reasons.append("membership_incomplete")
    extras = affected - allowed if permission["mode"] == "members" else set()
    if extras:
        reasons.append("additional_members")
    blocked = []
    for protection in protections:
        kind = protection.get("kind")
        if kind not in {"reference", "policy_content", "effective_rules"}:
            raise ValueError("Unsupported policy protection kind")
        policy_matches = protection.get("policy_id") in {None, current["policy_id"]}
        pawn = protection.get("pawn_id")
        if kind == "reference":
            _identity(pawn, "protected pawn_id")
            hit = action == "assign" and pawn in affected
        elif kind == "policy_content":
            _identity(protection.get("policy_id"), "protected policy_id")
            hit = action == "edit" and policy_matches
        else:
            _identity(pawn, "protected pawn_id")
            hit = pawn in affected and (action == "assign" or policy_matches)
        if hit:
            blocked.append(deepcopy(protection))
    if blocked:
        reasons.append("protected_effect")
    status = ("uncovered" if "membership_incomplete" in reasons else
              "scope_conflict" if any(reason in reasons for reason in ("scope_identity_changed", "additional_members")) else
              "protected" if blocked else "allowed")
    return {"allowed": not reasons, "status": status, "mode": permission["mode"],
            "actual_members": sorted(actual), "affected_members": sorted(affected), "extra_members": sorted(extras),
            "blocked_by": blocked, "reasons": reasons, "requires_apply_time_check": True}


def project(events):
    impacts = {}
    for event in events:
        payload = event.get("payload", {})
        if event.get("kind") in KINDS and payload.get("format") == FORMAT:
            row = deepcopy(payload)
            previous = impacts.get(row["impact_id"], {})
            row["history"] = [*previous.get("history", []), {"seq": event.get("seq"), "revision": row["revision"], "status": row["assessment"]["status"]}]
            impacts[row["impact_id"]] = row
    return {"impacts": list(impacts.values())}


def prepare_impact(events, *, impact_id, expected_revision, permission, current, protections=(), action="edit", pawn_ids=(), task_id=None):
    _identity(impact_id, "impact_id")
    old = next((row for row in project(events)["impacts"] if row["impact_id"] == impact_id), None)
    if type(expected_revision) is not int or expected_revision != (old["revision"] if old else 0):
        raise ValueError("Policy impact revision conflict")
    protections, pawn_ids = list(protections), list(pawn_ids)
    assessment = check_scope(permission, current, protections=protections, action=action, pawn_ids=pawn_ids)
    return {"kind": "policy.impact_selected", "source": "planner", "payload": {
        "format": FORMAT, "impact_id": impact_id, "task_id": task_id, "revision": expected_revision + 1,
        "permission": deepcopy(permission), "current": deepcopy(current), "protections": deepcopy(protections),
        "action": action, "pawn_ids": pawn_ids, "assessment": assessment}}
