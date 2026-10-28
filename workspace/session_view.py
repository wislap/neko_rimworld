"""Frontdesk session as a view over the event log: recent turns verbatim, older tool results left to recall."""

from ..serialization import canonical as encode
from .ledger import RUN_EVENT, UTTERANCE_EVENT


def frontdesk_messages(events, *, now_tick, window_ticks=180_000):
    """OpenAI-format messages for the turns inside the window; tool results there are placeholders."""
    cutoff = now_tick - window_ticks
    runs = _runs_by_utterance(events)
    messages = []
    for event in events:
        if event["kind"] != UTTERANCE_EVENT or event["game_tick"] < cutoff:
            continue
        messages.append({"role": "user", "content": event["payload"]["text"]})
        for run in runs.get(event["seq"], []):
            names = _call_names(run)
            messages.extend(_view_message(message, run, names) for message in run["payload"]["messages"])
    return messages


def recall(events, event_seq, tool_call_id=None, *, progress=()):
    """Read retained utterances or the unpruned tool result, never a projection placeholder."""
    event = _event_at(events, event_seq)
    if event["kind"] == UTTERANCE_EVENT and tool_call_id is None:
        return event["payload"]["text"]
    if event["kind"] == RUN_EVENT and tool_call_id is not None:
        for item in progress:
            payload = item["payload"]
            if payload.get("tool_call_id") == tool_call_id and "tool_result" in payload:
                return encode({"tool_result": payload["tool_result"], "observation": payload.get("observation")})
        from ..reports import plain
        for item in (event["payload"].get("audit") or {}).get("events", []):
            data = plain(item.get("data", {}))
            if item.get("event") in {"tool_completed", "tool_failed"} and data.get("call_id") == tool_call_id and "result" in data:
                return encode({"tool_result": data["result"], "observation": None})
        raise KeyError("Original tool result is unavailable; the model projection is not the original")
    raise KeyError(f"No recallable original at event {event_seq} for tool_call_id {tool_call_id!r}")


def pinned_utterances(events, board, *, now_tick, window_ticks=180_000):
    """Utterances behind open intents that have fallen outside the window; in-window ones are already shown."""
    cutoff = now_tick - window_ticks
    pinned = {
        entry["source"]["event_seq"]
        for entry in board
        if entry["kind"] == "intent" and entry["status"] != "closed"
    }
    return [
        {"event_seq": event["seq"], "text": event["payload"]["text"]}
        for event in events
        if event["kind"] == UTTERANCE_EVENT and event["seq"] in pinned and event["game_tick"] < cutoff
    ]


def _runs_by_utterance(events):
    runs = {}
    for event in events:
        if event["kind"] == RUN_EVENT:
            runs.setdefault(event["payload"]["utterance_seq"], []).append(event)
    return runs


def _call_names(run):
    return {
        call["id"]: call["function"]["name"]
        for message in run["payload"]["messages"]
        for call in message.get("tool_calls", [])
    }


def _view_message(message, run, names):
    if message["role"] != "tool":
        return dict(message)
    placeholder = {
        "recall": {"event_seq": run["seq"], "tool_call_id": message["tool_call_id"]},
        "tool": names[message["tool_call_id"]],
        "game_tick": run["game_tick"],
        "note": "recall original by event_seq and tool_call_id when retained",
    }
    return {**message, "content": encode(placeholder)}


def _event_at(events, event_seq):
    for event in events:
        if event["seq"] == event_seq:
            return event
    raise KeyError(f"No event {event_seq} in this colony")
