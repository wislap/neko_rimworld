"""Original user words behind each active board intent, read from the event log."""

from .ledger import UTTERANCE_EVENT


def intent_source_history(ledger, colony_id, *, referenced=(), pending=()):
    """Original words for each active intent since its most recent closure.

    Prior sources are history, not independently active permissions; the
    current board revision and later user words determine their meaning.
    """
    histories = {}
    for revision in ledger.revisions(colony_id, kind="intent"):
        entry_id = revision["entry_id"]
        if revision["status"] == "closed":
            histories.pop(entry_id, None)
            continue
        history = histories.setdefault(entry_id, [])
        history.append((revision["source"]["event_seq"], revision["revision"]))
    emitted = set(referenced)
    pending = set(pending) - emitted
    result = []
    for entry_id, revisions in histories.items():
        sources = []
        by_id = {}
        for event_seq, revision in revisions:
            if event_seq in by_id:
                by_id[event_seq]["revisions"].append(revision)
                continue
            source = {"event_seq": event_seq, "revisions": [revision]}
            event = ledger.event(colony_id, event_seq)
            if event is None or event["kind"] != UTTERANCE_EVENT or not isinstance(event["payload"].get("text"), str):
                if event_seq == revisions[-1][0]:
                    raise ValueError("Current intent source refers to unavailable user words")
                source["status"] = "unavailable"
            else:
                source.update(status="pending" if event_seq in pending else "referenced" if event_seq in emitted else "available",
                              load_epoch=event["load_epoch"], game_tick=event["game_tick"])
                if event_seq not in emitted and event_seq not in pending:
                    source["text"] = event["payload"]["text"]
                    emitted.add(event_seq)
            by_id[event_seq] = source
            sources.append(source)
        result.append({"entry_id": entry_id, "sources": sources,
                       "meaning": "Original user sources since the last closure; earlier words may have been changed or withdrawn. They are history, not independent current authorization. Referenced words are supplied elsewhere in this input; pending words have not yet been delivered."})
    return result
