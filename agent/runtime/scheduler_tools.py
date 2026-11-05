"""Tools whose state the colony scheduler owns: attention, pause reflex and planner run controls."""

from ...tool_contracts import ToolExecutionError, ToolSpec
from ...workspace.board import entry_schema
from ...workspace.ledger import PLANNER
from ..write.planner_completion import validate_planner_completion


def attention_tools(service, game, role, *, binding_check, planner_signal, attention_available, intent_sources=None):
    """Planner reads/declares attention; frontdesk requests focus and sets the pause reflex."""
    async def attention(args):
        current = service.game_session()
        if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
            raise ToolExecutionError("world_changed", "The active colony changed.", recoverable=False)
        try:
            action = args.get("action")
            if role == "planner" and action == "read":
                state = await service.call("attention_state", current.colony_id, current.load_epoch,
                                           current.game_tick, command_binding_check=binding_check)
                state["attention_available"] = attention_available()
                planner_signal.set()
                return state
            if role == "planner" and action == "declare":
                seq = await service.call("attention_declare", current.colony_id, current.load_epoch,
                                         current.game_tick, "planner", args["level"], args["entry_id"],
                                         args["reason"], command_binding_check=binding_check)
                planner_signal.set()
                return {"event_seq": seq}
            if role == "frontdesk" and action == "declare":
                seq = await service.call("attention_declare", current.colony_id, current.load_epoch,
                                         current.game_tick, "user", "focus", args["utterance_seq"],
                                         "User requested attention", user_sources=tuple(intent_sources or ()),
                                         command_binding_check=binding_check)
                planner_signal.set()
                return {"event_seq": seq}
            if role == "frontdesk" and action == "revoke":
                revoked = await service.call("attention_revoke", current.colony_id, current.load_epoch,
                                             current.game_tick, args["utterance_seq"], tuple(intent_sources or ()),
                                             command_binding_check=binding_check)
                return {"revoked": revoked}
            raise ValueError("Unknown attention action")
        except (KeyError, ValueError, TypeError) as error:
            raise ToolExecutionError("attention_invalid", str(error)) from error

    async def pause_policy(args):
        current = service.game_session()
        if current is None:
            raise ToolExecutionError("world_changed", "The active colony changed.", recoverable=False)
        try:
            seq = await service.call("set_wake_policy", current.colony_id, current.load_epoch,
                                     current.game_tick, "frontdesk", {"pause_on_letter_defs": args["letter_defs"]},
                                     command_binding_check=binding_check)
            return {"event_seq": seq}
        except (KeyError, ValueError, TypeError) as error:
            raise ToolExecutionError("scheduler_invalid", str(error)) from error

    if role == "planner":
        return (ToolSpec("attention", "Read attention cadence or declare a temporary increase for an open task.",
                         {"type": "object", "properties": {"action": {"type": "string", "enum": ["read", "declare"]},
                          "level": {"type": "string", "enum": ["focus", "urgent"]}, "entry_id": {"type": "string"},
                          "reason": {"type": "string", "minLength": 1}}, "required": ["action"],
                          "additionalProperties": False}, attention),)
    return (ToolSpec("attention_request", "Request or revoke focus only when the cited user utterance was received in this run.",
                     {"type": "object", "properties": {"action": {"type": "string", "enum": ["declare", "revoke"]},
                      "utterance_seq": {"type": "integer"}}, "required": ["action", "utterance_seq"],
                      "additionalProperties": False}, attention),
            ToolSpec("pause_policy", "Set the native letter pause-reflex definitions.",
                     {"type": "object", "properties": {"letter_defs": {"type": "array", "items": {"type": "string"}}},
                      "required": ["letter_defs"], "additionalProperties": False}, pause_policy))


def planner_tools(service, colony_id, load_epoch, *, current_tick, binding_check, event_provider, observations,
                  message_frontdesk):
    """Board, yield and frontdesk-message tools of one planner run, in model-visible order."""
    async def board_tool(args):
        try:
            if args.get("action") == "read":
                return {"entries": await service.call("board", colony_id)}
            if args.get("action") != "write":
                raise ValueError("Unknown action")
            entry = args["entry"]
            if not isinstance(entry, dict):
                raise ValueError("Board entry must be an object")
            if entry.get("status") in ("committed", "verified"):
                setup = await event_provider() if event_provider else None
                await validate_planner_completion(service, entry, colony_id, load_epoch, setup, observations())
            seq = await service.call("submit", colony_id, load_epoch, current_tick(), "planner", entry,
                                     command_binding_check=binding_check)
            return {"event_seq": seq}
        except (ValueError, KeyError) as error:
            raise ToolExecutionError("board_invalid", str(error)) from error

    async def yield_tool(args):
        handoff = args.get("handoff")
        if not isinstance(handoff, str) or not handoff.strip():
            raise ToolExecutionError("handoff_required", "A nonempty handoff is required.")
        return {"handoff": handoff}

    async def frontdesk_message(args):
        text = args.get("text")
        purpose = args.get("purpose")
        if (set(args) != {"text", "purpose"} or not isinstance(text, str) or not text.strip()
                or purpose not in {"user_decision", "urgent_risk"}):
            raise ToolExecutionError("invalid_tool_arguments", "Provide text and purpose user_decision or urgent_risk; record routine updates on the board.")
        return {"wake_seq": await message_frontdesk(text, purpose)}

    return (
        ToolSpec("message_frontdesk", "Wake the frontdesk only for a user decision or urgent risk; record routine updates on the board.",
                 {"type": "object", "properties": {"text": {"type": "string", "minLength": 1},
                  "purpose": {"type": "string", "enum": ["user_decision", "urgent_risk"]}},
                  "required": ["text", "purpose"], "additionalProperties": False}, frontdesk_message),
        ToolSpec("planner_board", "Read or revise strategy and task entries on the colony board. "
                 "For BOTH creation and revision use {action: 'write', entry: {the full entry skeleton}}; "
                 "never flatten entry_id, status or other entry fields onto the top level. "
                 "Use actual changeset IDs from this task's commit receipts for committed. "
                 "Verified needs matching observations from a later game tick; yield and recheck in a later run if time must advance.",
                 {"type": "object", "properties": {"action": {"type": "string", "enum": ["read", "write"]},
                  "entry": entry_schema(PLANNER)}, "required": ["action"], "additionalProperties": False}, board_tool),
        ToolSpec("planner_yield", "Finish this run with a handoff of progress, next steps and undecided thoughts. "
                 "The handoff is saved and does not wake the frontdesk. Record decisions and progress on the board; "
                 "routine confirmations need no frontdesk message.",
                 {"type": "object", "properties": {"handoff": {"type": "string", "minLength": 1}},
                  "required": ["handoff"], "additionalProperties": False}, yield_tool, terminal=True),
    )
