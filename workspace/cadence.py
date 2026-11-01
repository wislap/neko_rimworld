"""Replayable game-tick cadence projection.

Events use the journal envelope (seq, kind, load_epoch, game_tick, payload).
Derived items have stable IDs; the journal must append them atomically and
include the ID in their payload. This module performs no IO or dispatch.
"""

from .attention_policy import baseline_changes, classify, merge_observation, normalize_signal, target_identity

LEVELS = {"steady": 0, "focus": 1, "urgent": 2}
NAMES = ("steady", "focus", "urgent")


def validate_declaration(events, *, epoch, tick, actor, level, reference, reason, policy,
                         open_tasks=(), user_utterances=()):
    """Validate a proposed lease against durable history; return its fixed payload."""
    if actor not in ("planner", "user") or level not in ("focus", "urgent"):
        raise ValueError("Invalid actor or level")
    if actor == "user" and level != "focus":
        raise ValueError("User request is limited to focus")
    if not reference or not isinstance(reason, str) or not reason.strip():
        raise ValueError("A source reference and reason are required")
    current = [e for e in events if e["load_epoch"] == epoch and e["game_tick"] <= tick]
    if actor == "planner" and reference not in open_tasks:
        raise ValueError("Planner declaration requires an open planner task")
    if actor == "user" and reference not in user_utterances:
        raise ValueError("User request requires a received utterance")
    lifecycle = [e for e in events if e["kind"] in ("autoplay.activation", "autoplay.disabled")]
    if not lifecycle or lifecycle[-1]["kind"] != "autoplay.activation":
        raise ValueError("No activation")
    activation = lifecycle[-1]
    if actor == "planner":
        start = activation["game_tick"]
        day = max(0, (tick - start) // policy["ticks"]["day"])
        leases = [e for e in current if e["kind"] == "cadence.lease_declared"
                  and e["seq"] > activation["seq"] and e["payload"].get("actor") == actor]
        quota_leases = [e for e in events if e["seq"] > activation["seq"]
                        and e["kind"] == "cadence.lease_declared" and e["payload"].get("actor") == actor]
        if sum(max(0, (e["game_tick"] - start) // policy["ticks"]["day"]) == day
               for e in quota_leases) >= policy["ticks"]["planner_daily_limit"]:
            raise ValueError("Daily declaration quota exhausted")
        if leases:
            latest = leases[-1]
            ended = latest["payload"]["end_tick"]
            for event in current:
                if event["seq"] <= latest["seq"]:
                    continue
                data = event["payload"]
                if (event["kind"] == "cadence.lease_expired" and data.get("lease_id") == latest["seq"]):
                    ended = min(ended, data.get("expired_tick", event["game_tick"]))
                elif event["kind"] == "task.closed" and data.get("reference") == latest["payload"]["reference"]:
                    ended = min(ended, event["game_tick"])
            if tick < ended + policy["ticks"]["cooldown"]:
                raise ValueError("Declaration is active or cooling down")
    duration = policy["ticks"]["user_declaration" if actor == "user" else level + "_declaration"]
    return {"actor": actor, "level": level, "reference": reference, "reason": reason,
            "start_tick": tick, "end_tick": tick + duration}


def project(events, *, epoch, now_tick, policy):
    """Return current state and deterministic append-only work from ordered events.

    Normalized attention.signal payloads contain category, source_id, edge,
    priority/runtime_type/def as applicable. `planner.started` carries work_ids
    and actually received signal_seqs; `planner.received` can acknowledge later
    inbox delivery. `planner.finished` carries start_seq. Lease invalidation uses reference on
    task.closed/attention.revoked. Caller validates ownership of those references.
    """
    ordered = sorted(events, key=lambda e: e["seq"])
    lifecycle = [e for e in ordered if e["kind"] in ("autoplay.activation", "autoplay.disabled")]
    if not lifecycle:
        return {"active": False, "items": []}
    if lifecycle[-1]["kind"] == "autoplay.disabled":
        disabled = lifecycle[-1]
        starts = [e for e in lifecycle[:-1] if e["kind"] == "autoplay.activation"]
        if not starts:
            return {"active": False, "items": []}
        start = starts[-1]["seq"]
        existing_ids = {e["payload"].get("id") for e in ordered}
        result = []
        for declaration in (e for e in ordered if start < e["seq"] < disabled["seq"]
                            and e["load_epoch"] == disabled["load_epoch"]
                            and e["kind"] == "cadence.lease_declared"):
            lease_id = declaration["seq"]
            identity = f"lease-expired:{lease_id}"
            if identity in existing_ids or any(e["seq"] > lease_id and e["seq"] < disabled["seq"]
                    and e["kind"] in ("task.closed", "attention.revoked")
                    and e["payload"].get("reference") == declaration["payload"]["reference"] for e in ordered):
                continue
            end = min(declaration["payload"]["end_tick"], disabled["game_tick"])
            result.append({"kind": "cadence.lease_expired", "game_tick": now_tick,
                           "payload": {"id": identity, "lease_id": lease_id, "effective_tick": end,
                                       "expired_tick": end, "reason": "autoplay_disabled"}})
        return {"active": False, "items": result}
    activation = lifecycle[-1]
    rows = [e for e in ordered if e["load_epoch"] == epoch
            and e["seq"] >= activation["seq"] and e["game_tick"] <= now_tick]
    if any(a["game_tick"] > b["game_tick"] for a, b in zip(rows, rows[1:])):
        raise ValueError("Event ticks must be ordered within an epoch")
    # The epoch's load event anchors which daily boundaries will be crossed;
    # older boundaries are not retroactively created on load.
    epoch_start = rows[0]["game_tick"] if rows else now_tick
    ticks = policy["ticks"]
    existing = {e["payload"].get("id") for e in rows if isinstance(e.get("payload"), dict)}
    items, facts, leases, consumed = [], {}, {}, set()
    object_absent = {}
    pending_signals = []
    current_map = None
    running, last_finish = False, None
    focus_window, focus_ids, pending_focus = None, [], None
    focus_started, deferred_signals = {}, []
    level = 0
    reasons = []
    last_tick = epoch_start
    anchor = activation["game_tick"]
    next_day = anchor + max(1, (epoch_start - anchor) // ticks["day"] + 1) * ticks["day"]
    next_review = None
    pending_daily = []
    active_work_ids, active_signal_seqs = [], []

    def emit(kind, identity, tick, **payload):
        if identity not in existing:
            items.append({"kind": kind, "game_tick": now_tick,
                          "payload": {"id": identity, "effective_tick": tick, **payload}})
            existing.add(identity)

    # Loading invalidates old-epoch leases. Derive the expiry from recorded
    # declarations and closures, while keeping its append tick in this epoch.
    if activation["load_epoch"] != epoch and rows:
        prior = [e for e in ordered if activation["seq"] <= e["seq"] < rows[0]["seq"]]
        old_epoch = prior[-1]["load_epoch"] if prior else None
        old_rows = [e for e in prior if e["load_epoch"] == old_epoch]
        for declaration in (e for e in old_rows if e["kind"] == "cadence.lease_declared"):
            declaration_id = declaration["seq"]
            if any(e["kind"] == "cadence.lease_expired" and e["payload"].get("lease_id") == declaration_id
                   for e in prior):
                continue
            if any(e["seq"] > declaration_id and e["kind"] in ("task.closed", "attention.revoked")
                   and e["payload"].get("reference") == declaration["payload"]["reference"] for e in old_rows):
                continue
            old_tick = old_rows[-1]["game_tick"]
            natural_end = declaration["payload"]["end_tick"]
            expired_at = natural_end if natural_end <= old_tick else epoch_start
            emit("cadence.lease_expired", f"lease-expired:{declaration_id}", expired_at,
                 lease_id=declaration_id, expired_tick=expired_at,
                 reason="deadline" if natural_end <= old_tick else "epoch_changed")

    def advance_at(tick):
        nonlocal next_day, focus_window, focus_ids, pending_focus, level, next_review, reasons
        while next_day <= tick:
            day = (next_day - activation["game_tick"]) // ticks["day"]
            identity = f"daily:{activation['seq']}:{epoch}:{day}"
            emit("cadence.review_due", identity, next_day, review_type="daily", day=day, due_tick=next_day)
            if identity not in consumed:
                pending_daily.append(identity)
            next_day += ticks["day"]
        for lease_id, lease in list(leases.items()):
            if lease["end_tick"] <= tick:
                emit("cadence.lease_expired", f"lease-expired:{lease_id}", lease["end_tick"], lease_id=lease_id, expired_tick=lease["end_tick"])
                del leases[lease_id]
        if focus_window is not None and focus_window <= tick:
            day = (focus_window - activation["game_tick"]) // ticks["day"]
            deferred_signals.extend(focus_ids)
            if pending_focus is None and focus_started.get(day, 0) < ticks["focus_daily_limit"]:
                pending_focus = f"focus:{focus_ids[0]}"
                emit("cadence.review_due", pending_focus, focus_window,
                     review_type="signal", signal_seqs=list(focus_ids))
            focus_window, focus_ids = None, []
        candidates = []
        for fact in facts.values():
            age = tick - fact["since"]
            if fact["active"]:
                if fact["level"] == 2:
                    value = 2 if age < ticks["urgent_lifetime"] else (1 if age < ticks["urgent_lifetime"] + ticks["focus_lifetime"] else 0)
                else:
                    value = 1 if age < ticks["focus_lifetime"] else 0
            else:
                # One cooling interval per level, starting at confirmed disappearance.
                elapsed = tick - fact["gone_tick"]
                value = max(0, fact["gone_level"] - elapsed // ticks["cooldown"])
            if value:
                candidates.append((value, fact["seq"], fact["source_id"]))
        for lease_id, lease in leases.items():
            candidates.append((LEVELS[lease["level"]], lease_id, "lease"))
        new_level = max((x[0] for x in candidates), default=0)
        reasons = [{"event_seq": seq, "source_id": source} for value, seq, source in candidates if value == new_level]
        interval = ticks["urgent_review"] if new_level == 2 else ticks["focus_review"] if new_level == 1 else None
        next_review = last_finish + interval if interval is not None and last_finish is not None else None
        if new_level != level:
            identity = f"level:{activation['seq']}:{epoch}:{tick}:{new_level}"
            emit("cadence.changed", identity, tick, level=NAMES[new_level], previous=NAMES[level],
                 next_review_tick=next_review,
                 reasons=reasons)
            level = new_level
        if not running and next_review is not None and next_review <= tick:
            emit("cadence.review_due", f"escalated:{last_finish}", next_review,
                 review_type="escalated", level=NAMES[level], due_tick=next_review)

    def advance(tick):
        nonlocal last_tick
        # Replay transitions at their own deadlines, even when the next observed
        # native event arrives much later.
        boundaries = {tick}
        if next_day <= tick:
            boundaries.update(range(next_day, tick + 1, ticks["day"]))
        if focus_window is not None and last_tick < focus_window <= tick:
            boundaries.add(focus_window)
        for lease in leases.values():
            if last_tick < lease["end_tick"] <= tick:
                boundaries.add(lease["end_tick"])
        for fact in facts.values():
            if fact["active"]:
                deadlines = ([fact["since"] + ticks["urgent_lifetime"], fact["since"] + ticks["urgent_lifetime"] + ticks["focus_lifetime"]]
                             if fact["level"] == 2 else [fact["since"] + ticks["focus_lifetime"]])
            else:
                deadlines = [fact["gone_tick"] + n * ticks["cooldown"] for n in range(1, fact["gone_level"] + 1)]
            boundaries.update(d for d in deadlines if last_tick < d <= tick)
        if last_finish is not None:
            boundaries.update(d for d in (last_finish + ticks["urgent_review"], last_finish + ticks["focus_review"]) if last_tick < d <= tick)
        for boundary in sorted(boundaries):
            advance_at(boundary)
        last_tick = tick

    def mark_gone(fact, tick):
        age = tick - fact["since"]
        fact["gone_level"] = ((2 if age < ticks["urgent_lifetime"] else
                              1 if age < ticks["urgent_lifetime"] + ticks["focus_lifetime"] else 0)
                             if fact["level"] == 2 else int(age < ticks["focus_lifetime"]))
        fact.update(active=False, gone_tick=tick)

    for event in rows:
        tick, kind, data, seq = event["game_tick"], event["kind"], event["payload"], event["seq"]
        advance(tick)
        if kind == "attention.signal":
            data = normalize_signal(data)
            source, edge = data["source_id"], data["edge"]
            if edge == "snapshot":
                map_id = data.get("map_id")
                if current_map is not None and current_map != map_id:
                    facts.clear()
                    object_absent.clear()
                current_map = map_id
                for fact in facts.values():
                    part = data["partitions"].get(fact.get("partition"), {})
                    if (fact["active"] and part.get("status") == "ok" and not part.get("truncated")
                            and fact["source_id"] not in part.get("source_ids", [])):
                        mark_gone(fact, tick)
                advance(tick)
                continue
            pending_signals.append(seq)
            original = data
            prior_observation = facts.get(source, {}).get("observed", {})
            changes = [data]
            if edge == "baseline":
                old = facts.get(source)
                if old and old["active"]:
                    changes = baseline_changes(old.get("observed", {}), data)
                else:
                    changes = [{**data, "edge": "appeared"}]
            for data in changes:
                source, edge = data["source_id"], data["edge"]
                classified = classify(policy, data)
                if edge == "removed":
                    for target in data.get("removed", []):
                        object_absent[(source, target_identity(target))] = tick
                if edge == "added" and data.get("added"):
                    eligible = [target for target in data["added"]
                                if tick - object_absent.get((source, target_identity(target)), -ticks["cooldown"]) >= ticks["cooldown"]]
                    if not eligible:
                        continue
                if edge in ("appeared", "added", "upgraded"):
                    old = facts.get(source)
                    # Reappearance inside the quiet period is the same generation,
                    # not a fresh interrupt or a renewed lifetime.
                    if edge == "appeared" and old and not old["active"] and tick - old["gone_tick"] < ticks["cooldown"]:
                        old["active"] = True
                        advance(tick)
                        continue
                    fact_level = 2 if (data["category"] == "threat" or data.get("priority") == "Critical") else 1 if classified["level"] == "focus" and data["category"] in ("alert", "condition") else 0
                    if fact_level:
                        facts[source] = {"source_id": source, "seq": seq, "since": tick, "active": True, "level": fact_level,
                                         "partition": {"alert": "alerts", "condition": "conditions", "threat": "threats"}.get(data["category"]),
                                         "map_id": data.get("map_id"), "observed": dict(data)}
                    if classified["level"] == "interrupt":
                        emit("cadence.review_due", f"interrupt:{seq}", tick, review_type="signal", urgency="interrupt", signal_seqs=[seq])
                    elif classified["level"] == "focus":
                        if focus_window is None:
                            focus_window = tick + ticks["cooldown"]
                        focus_ids.append(seq)
                elif edge == "priority_decreased" and source in facts:
                    fact = facts[source]
                    if data.get("priority") == "High" and data.get("runtime_type") in policy["acute_high_alerts"]:
                        # This changes the fact, but is record-only for wake delivery.
                        fact.update(level=1, seq=seq)
                    elif fact["active"]:
                        mark_gone(fact, tick)
                elif edge in ("disappeared", "ended") and source in facts:
                    fact = facts[source]
                    if fact["active"]:
                        mark_gone(fact, tick)
            if source in facts and facts[source]["active"]:
                facts[source]["observed"] = merge_observation(prior_observation, original)
        elif kind == "cadence.lease_declared":
            if data["end_tick"] > tick:
                leases[seq] = dict(data)
        elif kind in ("task.closed", "attention.revoked"):
            for lease_id, lease in list(leases.items()):
                if lease["reference"] == data.get("reference"):
                    emit("cadence.lease_expired", f"lease-expired:{lease_id}", tick, lease_id=lease_id, expired_tick=tick)
                    del leases[lease_id]
        elif kind == "planner.started":
            running = True
            active_work_ids = list(data.get("work_ids", []))
            active_signal_seqs = [seq for seq in data.get("signal_seqs", []) if seq in pending_signals]
            consumed.update(data.get("work_ids", []))
            pending_daily[:] = [item for item in pending_daily if item not in consumed]
            if pending_focus in consumed:
                day = (tick - activation["game_tick"]) // ticks["day"]
                focus_started[day] = focus_started.get(day, 0) + 1
                pending_focus = None
            received = set(data.get("signal_seqs", []))
            pending_signals[:] = [signal for signal in pending_signals if signal not in received]
            deferred_signals[:] = [signal for signal in deferred_signals if signal not in received]
        elif kind == "planner.received":
            active_work_ids.extend(data.get("work_ids", []))
            active_signal_seqs.extend(seq for seq in data.get("signal_seqs", []) if seq in pending_signals)
            received = set(data.get("signal_seqs", []))
            pending_signals[:] = [signal for signal in pending_signals if signal not in received]
            deferred_signals[:] = [signal for signal in deferred_signals if signal not in received]
        elif kind == "planner.finished":
            running, last_finish = False, tick
            if not data.get("completed", True):
                for work_id in active_work_ids:
                    if work_id.startswith("daily:") and work_id not in pending_daily:
                        pending_daily.append(work_id)
                    elif work_id.startswith("focus:") and pending_focus is None:
                        pending_focus = work_id
                for signal_seq in active_signal_seqs:
                    if signal_seq not in pending_signals:
                        pending_signals.append(signal_seq)
            active_work_ids, active_signal_seqs = [], []
        elif kind == "autoplay.disabled":
            return {"active": False, "items": items}
        advance(tick)
        last_tick = tick
    advance(now_tick)
    planner_leases = [e for e in ordered if e["seq"] > activation["seq"] and e["kind"] == "cadence.lease_declared" and e["payload"].get("actor") == "planner" and (e["game_tick"] - activation["game_tick"]) // ticks["day"] == (now_tick - activation["game_tick"]) // ticks["day"]]
    return {"active": True, "level": NAMES[level], "reasons": reasons, "facts": facts, "leases": leases,
            "next_daily_tick": next_day, "pending_daily": pending_daily,
            "pending_focus": pending_focus, "deferred_signal_seqs": deferred_signals,
            "pending_signal_seqs": pending_signals,
            "next_escalated_tick": next_review, "planner_quota_remaining": max(0, ticks["planner_daily_limit"] - len(planner_leases)),
            "items": items}
