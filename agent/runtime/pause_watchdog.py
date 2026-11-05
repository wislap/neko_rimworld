"""Pause watchdog: wake the planner, then notify, while an agent-owned pause persists."""

import asyncio
import time


async def run_pause_watchdog(scheduler):
    try:
        await _watch_pause(scheduler)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        scheduler.error = error
        if scheduler.failure_handler is not None:
            await scheduler.failure_handler("watchdog", {"audit": {"error_code": "pause_watchdog_failed"}},
                                            scheduler.service.game_session())


async def _watch_pause(scheduler):
    loop = asyncio.get_running_loop()
    while True:
        scheduler._watchdog_event.clear()
        state, episode = scheduler.pause_state, scheduler._pause_episode
        game = scheduler.service.game_session()
        bound = scheduler._bind_game(game) if game is not None else None
        same_scope = (bound is not None and episode is not None
                      and (getattr(bound, "connection_epoch", None), bound.colony_id, bound.load_epoch) == episode[:3]
                      and scheduler._binding_current(bound))
        eligible = (state is not None and episode is not None and game is not None and same_scope
                    and state["paused"] and state["speed_paused"] and not state["force_paused"]
                    and not state["windows_force_paused"]
                    and state["owner"] in {"planner", "frontdesk", "reflex", "automatic_letter"}
                    and scheduler._business_current(bound))
        delay = None
        if eligible and not scheduler._pause_restored:
            records = await scheduler.service.call("pause_watchdog_history", game.colony_id, game.load_epoch)
            prior = [event for event in records if event["kind"] == "pause.watchdog"
                     and event["payload"].get("episode") == list(episode)]
            wakes = [event for event in prior if event["payload"].get("action") == "wake"]
            if wakes:
                last = wakes[-1]
                scheduler._pause_count = max(event["payload"].get("count", 0) for event in wakes)
                scheduler._pause_last_wake = loop.time() - max(0, time.time() - last["wall_time"])
                starts = {event["seq"] for event in records if event["kind"] == "planner.started"
                          and last["payload"].get("wake_seq") in event["payload"].get("wake_seqs", [])}
                finished = [event for event in records if event["kind"] == "planner.finished"
                            and event["payload"].get("start_seq") in starts
                            and event["payload"].get("completed") is True]
                scheduler._pause_waiting = not bool(finished)
                scheduler._pause_yielded = bool(finished) or scheduler._pause_yielded
            scheduler._pause_notified = any(event["payload"].get("action") in {"notify_attempt", "notify"}
                                            for event in prior)
            if scheduler._pause_episode != episode:
                continue
            scheduler._pause_restored = True
        if (eligible and not scheduler._planner_running and not scheduler._pause_waiting
                and (scheduler._pause_yielded or scheduler._pause_count == 0)):
            if scheduler._pause_count >= 3 and not scheduler._pause_notified:
                bound = scheduler._bind_game(game)
                allowed = await scheduler.service.call(
                    "pause_watchdog_notify", game.colony_id, game.load_epoch, game.game_tick, episode,
                    command_binding_check=lambda: scheduler._business_current(bound)
                    and scheduler._pause_episode == episode
                    and (getattr(bound, "connection_epoch", None), bound.colony_id, bound.load_epoch) == episode[:3])
                scheduler._pause_notified = allowed
                if allowed:
                    outcome = "unknown"
                    try:
                        if scheduler._pause_episode == episode:
                            receipt = await scheduler._watchdog_notify(bound)
                            if isinstance(receipt, dict) and receipt.get("submitted") is True:
                                outcome = "submitted"
                            elif isinstance(receipt, dict) and receipt.get("submitted") is False:
                                outcome = "rejected"
                    except asyncio.CancelledError:
                        await asyncio.shield(scheduler.service.call(
                            "record", game.colony_id, game.load_epoch, game.game_tick, "plugin", "pause.watchdog",
                            {"episode": list(episode), "action": "notify_result", "outcome": "unknown"}))
                        raise
                    except Exception:
                        pass
                    await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick, "plugin",
                                                 "pause.watchdog", {"episode": list(episode), "action": "notify_result",
                                                                    "outcome": outcome})
            elif scheduler._pause_count < 3:
                if scheduler._pause_last_wake is not None:
                    remaining = scheduler._watchdog_interval - (loop.time() - scheduler._pause_last_wake)
                elif scheduler._pause_yielded:
                    remaining = 0
                else:
                    remaining = scheduler._watchdog_interval - (loop.time() - scheduler._pause_seen_at)
                if remaining <= 0:
                    bound = scheduler._bind_game(game)
                    wake = await scheduler.service.call(
                        "pause_watchdog_wake", game.colony_id, game.load_epoch, game.game_tick,
                        episode, scheduler._pause_count + 1,
                        command_binding_check=lambda: scheduler._business_current(bound)
                        and scheduler._pause_episode == episode
                        and (getattr(bound, "connection_epoch", None), bound.colony_id, bound.load_epoch) == episode[:3])
                    if wake is None:
                        scheduler.pause_state = None
                        continue
                    if scheduler._pause_episode != episode:
                        continue
                    scheduler._pause_count += 1
                    scheduler._pause_last_wake = loop.time()
                    scheduler._pause_waiting = True
                    scheduler._games[game.colony_id] = bound
                    scheduler._signal.set()
                else:
                    delay = remaining
        try:
            await asyncio.wait_for(scheduler._watchdog_event.wait(), timeout=delay)
        except TimeoutError:
            pass
