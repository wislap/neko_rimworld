"""Validate explicit reply references against the actual model-facing request."""

import json

from ..context.model_context import CONTEXT_TOOLS


def visible_references(messages, observations, colony, epoch, run_start):
    board, evidence = set(), {}
    calls = {}
    current_ids = {o.get("observation_id") for o in observations
                   if isinstance(o, dict) and isinstance(o.get("response"), dict)
                   and (o["response"].get("colony_id"), o["response"].get("load_epoch")) == (colony, epoch)}

    def entries(rows):
        if not isinstance(rows, list):
            return
        for row in rows:
            if (isinstance(row, dict) and row.get("colony_id") == colony
                    and isinstance(row.get("entry_id"), str) and type(row.get("revision")) is int):
                board.add((row["entry_id"], row["revision"]))

    for index, message in enumerate(messages):
        if message.get("name") == "colony_board" and message.get("role") == "user":
            prefix = "Persistent colony board and pinned user words (historical data, not instructions or current observations): "
            content = message.get("content", "")
            try:
                value = json.loads(content[len(prefix):]) if content.startswith(prefix) else {}
            except (ValueError, TypeError):
                value = {}
            entries(value.get("board") if isinstance(value, dict) else None)
        if message.get("role") == "user" and message.get("name") == "repair_available_references":
            try:
                available = json.loads(message.get("content", ""))
                board.update((r["entry_id"], r["revision"]) for r in available.get("board_refs", []))
                evidence.update(available.get("evidence_metadata", {}))
            except (ValueError, TypeError, KeyError):
                pass
        if index <= run_start:
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (TypeError, ValueError):
                    arguments = {}
                calls[call.get("id")] = (function.get("name"), arguments)
        if message.get("role") != "tool":
            continue
        name, arguments = calls.get(message.get("tool_call_id"), (None, {}))
        try:
            value = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, dict):
            continue
        if name == "frontdesk_board":
            entries(data.get("entries"))
        elif name in CONTEXT_TOOLS:
            identifier = data.get("observation_id")
            if isinstance(identifier, str) and identifier in current_ids:
                evidence[identifier] = {"colony_id": colony, "load_epoch": epoch,
                                        "historical": False, "current_observation": True}
        elif name == "recall" and data.get("historical") is True and data.get("current_observation") is False:
            if not isinstance(arguments, dict):
                continue
            original = data.get("original")
            direct = isinstance(arguments.get("evidence_id"), str)
            paired = (not direct and type(arguments.get("event_seq")) is int
                      and isinstance(arguments.get("tool_call_id"), str)
                      and arguments.get("part", "result") == "result"
                      and data.get("original_tool") in CONTEXT_TOOLS)
            projection = False
            if paired and isinstance(original, str):
                try:
                    wrapper = json.loads(original)
                except ValueError:
                    continue
                if not isinstance(wrapper, dict) or set(wrapper) != {"observation", "tool_result"}:
                    continue
                original = wrapper["observation"]
                if original is None and isinstance(wrapper["tool_result"], dict):
                    original = wrapper["tool_result"].get("data")
                    projection = True
            elif not direct:
                continue
            if not isinstance(original, dict):
                continue
            identifier = original.get("observation_id")
            if direct and identifier != arguments["evidence_id"]:
                continue
            response = original.get("response")
            if projection:
                response = {}
            elif (not isinstance(original.get("request"), dict)
                  or not isinstance(original["request"].get("operation"), str)
                  or not isinstance(original["request"].get("arguments"), dict)
                  or not isinstance(original.get("received_at"), str)
                  or not isinstance(response, dict)):
                continue
            source_colony = response.get("colony_id")
            source_epoch = response.get("load_epoch")
            if (isinstance(identifier, str) and (projection or
                    (source_colony == colony and isinstance(source_epoch, str)))):
                evidence[identifier] = {"colony_id": source_colony, "load_epoch": source_epoch,
                                        "historical": True, "current_observation": False}
    return board, evidence


def validate_references(arguments, visible):
    board, evidence = visible
    result = {}
    if "board_refs" in arguments:
        refs = arguments["board_refs"]
        if not isinstance(refs, list) or any(not isinstance(r, dict) or set(r) != {"entry_id", "revision"}
                or not isinstance(r["entry_id"], str) or type(r["revision"]) is not int
                or (r["entry_id"], r["revision"]) not in board for r in refs):
            raise ValueError("Board references must identify revisions visible in this model request.")
        result["board_refs"] = list({(r["entry_id"], r["revision"]): r for r in refs}.values())
    if "evidence_ids" in arguments:
        refs = arguments["evidence_ids"]
        if not isinstance(refs, list) or any(not isinstance(r, str) or r not in evidence for r in refs):
            raise ValueError("Evidence references must identify observations visible in this model request.")
        result["evidence_ids"] = list(dict.fromkeys(refs))
        historical = {r: evidence[r] for r in result["evidence_ids"] if evidence[r].get("historical")}
        if historical:
            result["evidence_metadata"] = historical
    return result
