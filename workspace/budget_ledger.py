"""Cumulative model-attempt reservations and settlements saved in the event log.

Writers run inside the transaction the journal opened.
"""

from .ledger import PLUGIN

KINDS = ("budget.reserve", "budget.settle")


def status(ledger, colony_id):
    events = ledger.events_by_kind(colony_id, KINDS)
    reservations = {e["payload"]["id"]: e["payload"] for e in events if e["kind"] == "budget.reserve"}
    settlements = {e["payload"]["id"]: e["payload"] for e in events if e["kind"] == "budget.settle"}
    input_total = known_prompt = known_completion = 0
    cost_total = known_cost = reserved_cost = 0.0
    unknown_usage = unknown_cost = unpriced = not_dispatched = attempted = 0
    for ident, item in reservations.items():
        receipt = settlements.get(ident, {})
        if receipt.get("outcome") == "not_dispatched":
            not_dispatched += 1
            continue
        if receipt.get("outcome") == "attempted":
            attempted += 1
        usage = receipt.get("usage", {})
        prompt, output, cached = (usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cached_tokens"))
        def valid(n):
            return type(n) is int and n >= 0
        input_total += prompt if valid(prompt) else item["input_estimate"]
        known_prompt += prompt if valid(prompt) else 0
        known_completion += output if valid(output) else 0
        if not (valid(prompt) and valid(output)):
            unknown_usage += 1
        prices = item["prices"]
        priced = all(prices[k] is not None for k in ("input_price", "output_price", "cache_read_price"))
        exact = priced and valid(prompt) and valid(output) and valid(cached) and cached <= prompt
        if exact:
            cost = ((prompt - cached) * prices["input_price"] + cached * prices["cache_read_price"]
                    + output * prices["output_price"]) / 1_000_000
            known_cost += cost
        else:
            unknown_cost += 1
            if not priced:
                unpriced += 1
                continue
            # Use known quantities even if they exceed the reservation. Unknown quantities
            # retain their reservation; unknown cache split uses the higher configured rate.
            input_count = prompt if valid(prompt) else item["input_estimate"]
            output_count = output if valid(output) else item["output_limit"]
            cost = (input_count * max(prices["input_price"], prices["cache_read_price"])
                    + output_count * prices["output_price"]) / 1_000_000
            reserved_cost += cost
        cost_total += cost
    charged = len(reservations) - not_dispatched
    return {"requests": charged, "reservations": len(reservations), "not_dispatched": not_dispatched,
            "attempted": attempted, "dispatch_unknown": charged - attempted,
            "input_tokens": input_total, "cost": cost_total,
            "unknown_usage": unknown_usage, "unknown_cost": unknown_cost,
            "known_prompt_tokens": known_prompt, "known_completion_tokens": known_completion,
            "known_cost": known_cost, "reserved_cost": reserved_cost,
            "unpriced_reservations": unpriced}


def reserve(ledger, colony_id, epoch, tick, payload, max_requests, max_input_tokens, max_cost):
    state = status(ledger, colony_id)
    if (state["requests"] + 1 > max_requests
            or state["input_tokens"] + payload["input_estimate"] > max_input_tokens
            or (max_cost is not None and (payload["reserved_cost"] is None
                or state["unpriced_reservations"] or state["cost"] + payload["reserved_cost"] > max_cost))):
        return False
    ledger.append(colony_id, epoch, tick, PLUGIN, "budget.reserve", payload)
    return True


def settle(ledger, colony_id, epoch, tick, payload):
    events = ledger.events_by_kind(colony_id, KINDS)
    if not any(e["kind"] == "budget.reserve" and e["payload"]["id"] == payload["id"] for e in events):
        raise ValueError("Unknown model reservation")
    for event in events:
        if event["kind"] == "budget.settle" and event["payload"]["id"] == payload["id"]:
            if event["payload"] == payload:
                return
            raise ValueError("Model reservation already settled differently")
    ledger.append(colony_id, epoch, tick, PLUGIN, "budget.settle", payload)
