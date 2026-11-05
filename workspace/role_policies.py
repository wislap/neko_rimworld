"""Frontdesk context window and scheduler wake policies saved in the event log."""

from .ledger import USER

CONTEXT_POLICY_EVENT = "frontdesk.context_policy"
WAKE_POLICY_EVENT = "scheduler.policy"
CONTEXT_POLICY_DEFAULTS = {"window_ticks": 180_000, "narrative_threshold": 60_000}
WAKE_POLICY_DEFAULTS = {
    "pause_on_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"],
    "frontdesk_letter_defs": ["ThreatBig", "ThreatSmall", "NegativeEvent"],
    "frontdesk_choice_letters": True,
    "frontdesk_paused_windows": True,
}


def context_policy(ledger, colony_id):
    policy = dict(CONTEXT_POLICY_DEFAULTS)
    for event in ledger.events_by_kind(colony_id, (CONTEXT_POLICY_EVENT,)):
        policy.update(event["payload"])
    return policy


def wake_policy(ledger, colony_id):
    policy = {key: value.copy() if isinstance(value, list) else value
              for key, value in WAKE_POLICY_DEFAULTS.items()}
    for event in ledger.events_by_kind(colony_id, (WAKE_POLICY_EVENT,)):
        if "pause_on_letter_defs" in event["payload"]:
            policy["pause_on_letter_defs"] = event["payload"]["pause_on_letter_defs"]
    return policy


def set_wake_policy(ledger, colony_id, load_epoch, game_tick, actor, changes):
    if actor != "frontdesk" or not isinstance(changes, dict) or set(changes) != {"pause_on_letter_defs"}:
        raise ValueError("Policy field is not writable by this role")
    definitions = changes["pause_on_letter_defs"]
    if not isinstance(definitions, list) or any(not isinstance(item, str) or not item for item in definitions):
        raise ValueError("Letter definitions must be nonempty strings")
    return ledger.append(colony_id, load_epoch, game_tick, actor, WAKE_POLICY_EVENT, changes)


def set_context_policy(ledger, colony_id, load_epoch, game_tick, changes):
    if not isinstance(changes, dict) or not changes or any(
        key not in CONTEXT_POLICY_DEFAULTS or type(value) is not int or not 1 <= value < 2**63
        for key, value in changes.items()
    ):
        raise ValueError("Context policy requires positive integer window_ticks or narrative_threshold")
    return ledger.append(colony_id, load_epoch, game_tick, USER, CONTEXT_POLICY_EVENT, changes)
