"""Frontdesk run messages and planner completions, each saved once per identity.

Writers run inside the transaction the journal opened.
"""

from ..serialization import identity
from .attention_cadence import append_cadence
from .ledger import FRONTDESK, RUN_EVENT, UTTERANCE_EVENT

RUN_ROLES = frozenset({"assistant", "tool"})


def record_run(ledger, colony_id, load_epoch, game_tick, role, utterance_seq, messages, *, result=None, audit=None):
    """Append the assistant and tool messages one frontdesk run added, answering a user utterance."""
    if role != FRONTDESK:
        raise ValueError("Only the frontdesk session records runs; the planner has no session")
    if not any(ledger.has_event(colony_id, kind, utterance_seq) for kind in (UTTERANCE_EVENT, "frontdesk.trigger")):
        raise ValueError("A run answers a user utterance or frontdesk trigger of this colony")
    _check_run_messages(messages)
    key = "utterance_seq" if ledger.has_event(colony_id, UTTERANCE_EVENT, utterance_seq) else "trigger_seq"
    payload = {key: utterance_seq, "messages": messages, "result": result, "audit": audit}
    run_id = audit.get("run_id") if isinstance(audit, dict) else None
    if not isinstance(run_id, str) or not run_id:
        return ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RUN_EVENT, payload)
    rows = ledger.db.execute(
        "SELECT seq,colony_id FROM events WHERE kind=? "
        "AND json_extract(payload,'$.audit.run_id')=? ORDER BY seq", (RUN_EVENT, run_id)
    ).fetchall()
    if rows:
        if len(rows) != 1:
            raise ValueError("Frontdesk run identity is ambiguous")
        prior = ledger.event(rows[0]["colony_id"], rows[0]["seq"])
        if (prior["colony_id"] != colony_id or prior["load_epoch"] != load_epoch
                or prior["game_tick"] != game_tick or prior["source"] != FRONTDESK
                or identity(prior["payload"]) != identity(payload)):
            raise ValueError("Frontdesk run identity conflicts with the saved outcome")
        return prior["seq"]
    return ledger.append(colony_id, load_epoch, game_tick, FRONTDESK, RUN_EVENT, payload)


def finish_planner(ledger, colony_id, load_epoch, game_tick, start_seq, cursor, result, audit):
    """Save one immutable completion, separating its context from append time."""
    if type(start_seq) is not int or start_seq <= 0 or type(cursor) is not int or cursor < 0:
        raise ValueError("Planner completion requires a valid start and consumed cursor")
    start = ledger.event(colony_id, start_seq)
    if start is None or start["kind"] != "planner.started" or start["load_epoch"] != load_epoch:
        raise ValueError("Planner completion scope conflicts with its start")
    # Keep the caller's original context stable across lost ACKs/retries.
    recorded_tick = ledger.recording_tick(colony_id, load_epoch, game_tick)
    terminal = result.get("terminal")
    payload = {"start_seq": start_seq, "context_tick": game_tick, "result": result, "audit": audit}
    saved = [event for event in ledger.events_by_kind(
        colony_id, ("planner.finished",), after_seq=start_seq)
        if event["payload"].get("start_seq") == start_seq]
    if saved:
        if len(saved) != 1:
            raise ValueError("Planner completion is ambiguous")
        prior = saved[0]
        consumed = ledger.event(colony_id, prior["seq"] + 1)
        # Existing completions used their envelope tick as context.
        expected = payload if "context_tick" in prior["payload"] else {key:value for key,value in payload.items() if key != "context_tick"}
        if prior["payload"].get("context_tick", prior["game_tick"]) != game_tick:
            raise ValueError("Planner completion conflicts with its original context")

        def matches(event, kind, content):
            if (event is None or event["kind"] != kind or event["source"] != "planner"
                    or event["load_epoch"] != load_epoch or event["game_tick"] != prior["game_tick"]):
                return False
            return identity(event["payload"]) == identity(content)

        if (not matches(prior, "planner.finished", expected)
                or not matches(consumed, "planner.consumed", {"through_seq": cursor})):
            raise ValueError("Planner completion conflicts with the saved outcome")
        if terminal and terminal.get("kind") == "yield":
            if not matches(ledger.event(colony_id, prior["seq"] - 1), "planner.yield",
                           {"start_seq": start_seq, "handoff": terminal["handoff"]}):
                raise ValueError("Planner completion conflicts with its saved handoff")
        return prior["seq"]
    if terminal and terminal.get("kind") == "yield":
        ledger.append(colony_id, load_epoch, recorded_tick, "planner", "planner.yield",
                      {"start_seq": start_seq, "handoff": terminal["handoff"]})
    seq = ledger.append(colony_id, load_epoch, recorded_tick, "planner", "planner.finished", payload)
    ledger.append(colony_id, load_epoch, recorded_tick, "planner", "planner.consumed", {"through_seq": cursor})
    append_cadence(ledger, colony_id, load_epoch, recorded_tick)
    return seq


def _check_run_messages(messages):
    """Only assistant and tool messages; each tool call is answered by its own tool message right after."""
    if not isinstance(messages, list):
        raise ValueError("A run records its messages as a list")
    pending = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in RUN_ROLES:
            raise ValueError("A run records only assistant and tool messages")
        if message["role"] == "assistant":
            if pending:
                raise ValueError("Each tool call is answered before the next message")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("An assistant message content is text or null")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or not all(_is_tool_call(call) for call in calls):
                raise ValueError("A tool call names an id and a function name")
            ids = [call["id"] for call in calls]
            if len(set(ids)) != len(ids):
                raise ValueError("Tool call ids are unique within a message")
            pending = set(ids)
        else:
            answer = message.get("tool_call_id")
            if not isinstance(answer, str) or answer not in pending or not isinstance(message.get("content"), str):
                raise ValueError("A tool message answers a tool call of the assistant message before it")
            pending.remove(answer)
    if pending:
        raise ValueError("Each tool call is answered by a tool message")


def _is_tool_call(call):
    function = call.get("function") if isinstance(call, dict) else None
    return (
        isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and bool(call["id"])
        and isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and bool(function["name"])
    )
