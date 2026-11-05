"""Bounded journal diagnostics for a failed scheduler role dispatch."""

import asyncio
import ntpath
from collections import deque


async def record_dispatch_error(service, role, error, game, code, *, completion=None):
    """Record bounded location metadata without exception text or frame locals."""
    frames = deque(maxlen=8)
    frame_count = 0
    tb = error.__traceback__
    while tb is not None:
        code_object = tb.tb_frame.f_code
        frames.append({"file": ntpath.basename(code_object.co_filename)[:128],
                       "function": code_object.co_name[:128], "line": tb.tb_lineno})
        frame_count += 1
        tb = tb.tb_next
    connection = getattr(game, "connection_epoch", None)
    if not isinstance(connection, str) or not connection or len(connection) > 128:
        connection = None
    payload = {"role": role, "phase": "planner_result_persistence" if completion else "scheduler_dispatch",
               "exception_type": type(error).__name__[:128],
               "origin_connection_epoch": connection, "frames": list(frames), "frames_truncated": frame_count > 8}
    if completion is not None:
        payload.update(start_seq=completion.start_seq, context_tick=completion.context_tick,
                       run_id=completion.audit.get("run_id"))
    diagnostic_seq = None
    try:
        async with asyncio.timeout(1):
            value = await service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                       "plugin", "autoplay.scheduler_exception", payload)
        if type(value) is int and value > 0:
            diagnostic_seq = value
    except Exception:
        pass  # The original failure still owns the stop decision.
    return {"error_code": code, "phase": payload["phase"], "exception_type": payload["exception_type"],
            **({key: payload[key] for key in ("start_seq", "context_tick", "run_id")} if completion else {}),
            "diagnostic_seq": diagnostic_seq,
            "diagnostic_status": "recorded" if diagnostic_seq is not None else "unconfirmed"}
