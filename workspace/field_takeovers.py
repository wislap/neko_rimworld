"""User field takeovers and their releases, recorded in the event log.

Writers run inside the transaction the journal opened.
"""

from .board import BoardError
from .ledger import FRONTDESK, PLUGIN, RETURN_EVENT, TAKEOVER_EVENT, UTTERANCE_EVENT

TAKEOVER_FIELDS = frozenset({"object", "field", "value_read", "ai_value", "changeset_id"})


def record_takeover(ledger, colony_id, load_epoch, game_tick, takeover):
    if not isinstance(takeover, dict) or set(takeover) != TAKEOVER_FIELDS:
        raise ValueError("A takeover records object, field, value_read, ai_value and changeset_id")
    return ledger.append(colony_id, load_epoch, game_tick, PLUGIN, TAKEOVER_EVENT, takeover)


def record_return(ledger, colony_id, load_epoch, game_tick, *, takeover_seq, utterance_seq, accepted_baseline=None):
    """Release a takeover, citing the user's own words; a takeover can be released once."""
    if not ledger.has_event(colony_id, TAKEOVER_EVENT, takeover_seq):
        raise BoardError("A return releases an open takeover of this colony")
    if ledger.has_return(colony_id, takeover_seq):
        raise BoardError("This takeover is already released")
    if not ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq):
        raise BoardError("A return cites a user utterance of this colony")
    payload = {"takeover_seq": takeover_seq, "utterance_seq": utterance_seq}
    if accepted_baseline is not None:
        if not isinstance(accepted_baseline, dict):
            raise ValueError("Accepted baseline must be an object")
        payload["accepted_baseline"] = accepted_baseline
    return ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RETURN_EVENT, payload)


def takeovers(ledger, colony_id):
    return open_takeovers(ledger.events_by_kind(colony_id, (TAKEOVER_EVENT, RETURN_EVENT)))


def open_takeovers(events):
    """Takeover events not yet released by a return event, from the event log alone."""
    opened = {}
    for event in events:
        if event["kind"] == TAKEOVER_EVENT:
            opened[event["seq"]] = event
        elif event["kind"] == RETURN_EVENT:
            opened.pop(event["payload"]["takeover_seq"], None)
    return list(opened.values())
