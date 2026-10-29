"""Frontdesk controls for releasing journal-derived field takeovers."""
from ..tool_contracts import ToolExecutionError, ToolSpec


def return_capability(service, game, received_utterances, setup_provider=None):
    async def release(args):
        try:
            takeover_seq = args.get("takeover_seq")
            utterance_seq = args.get("utterance_seq")
            if (set(args) != {"takeover_seq", "utterance_seq"}
                    or type(takeover_seq) is not int or type(utterance_seq) is not int
                    or utterance_seq not in received_utterances):
                raise ValueError("Return must cite user words received in this run")
            event = await service.call("event", game.colony_id, utterance_seq)
            if event is None or event["kind"] != "user.utterance":
                raise ValueError("Return must cite an original user utterance")
            if setup_provider is None:
                raise ValueError("A current ledger observation is required")
            setup = await setup_provider()
            if setup.write_identity is None:
                raise ValueError("A current world identity is required")
            control = TakeoverControl(service, setup.ledger_query, setup.write_identity, setup.write_fields_query)
            raw, data = await control.snapshot()
            if (raw["response"]["colony_id"], raw["response"]["load_epoch"]) != (game.colony_id, game.load_epoch):
                raise ValueError("The game changed before return")
            opened = await service.call("takeovers", game.colony_id)
            takeover = next((item for item in opened if item["seq"] == takeover_seq), None)
            if takeover is None:
                raise ValueError("The takeover is not open")
            key = (takeover["payload"]["object"], takeover["payload"]["field"])
            rows = [row for row in data["entries"] if (row.get("object"), row.get("field")) == key]
            if (len(rows) != 1 or rows[0].get("current_status") != "observed"
                    or "current_value" not in rows[0] or "value" not in rows[0]
                    or type(rows[0].get("may_change_by_game")) is not bool
                    or not isinstance(rows[0].get("changeset_id"), str)
                    or type(rows[0].get("tick")) is not int):
                raise ValueError("The taken-over field cannot be verified")
            if setup.write_identity() != (raw["response"]["connection_epoch"], game.colony_id, game.load_epoch):
                raise ValueError("The game changed before return")
            seq = await service.call("record_return", game.colony_id, game.load_epoch,
                                     raw["response"]["game_tick"], takeover_seq=takeover_seq,
                                     utterance_seq=utterance_seq,
                                     accepted_baseline={"field": rows[0], "observation": raw})
            return {"event_seq": seq}
        except (ValueError, KeyError) as error:
            raise ToolExecutionError("return_invalid", str(error)) from error

    return ToolSpec("return_takeover", "Return a taken-over field only when the user requests it in words received in this run.",
                    {"type": "object", "properties": {"takeover_seq": {"type": "integer"},
                     "utterance_seq": {"type": "integer"}}, "required": ["takeover_seq", "utterance_seq"],
                     "additionalProperties": False}, release, exclusive=True)


class TakeoverControl:
    """Use the journal as the sole durable source of open takeovers."""

    def __init__(self, service, provider, identity, fields_provider):
        self.service, self.provider, self.identity = service, provider, identity
        self.fields_provider = fields_provider

    async def snapshot(self):
        bound = self.identity()
        if not bound or self.provider is None:
            raise ToolExecutionError("takeover_unknown", "Current ledger observation is unavailable.")
        observation = await self.provider()
        raw = observation.to_dict()
        response = raw.get("response", {})
        payload = response.get("payload", {})
        data = payload.get("data", {})
        tick = response.get("game_tick")
        tick_unknown = type(tick) is not int or tick < 0

        async def invalid(message):
            await self.service.call("record", bound[1], bound[2], 0 if tick_unknown else tick,
                                    "plugin", "takeover.snapshot",
                                    {"valid": False, "observation": raw, "tick_unknown": tick_unknown})
            raise ToolExecutionError("takeover_unknown", message)

        if (not isinstance(data, dict) or self.identity() != bound or
                (response.get("connection_epoch"), response.get("colony_id"), response.get("load_epoch")) != tuple(bound)
                or payload.get("status") != "ok" or payload.get("issues") or data.get("ledger_status") != "valid"
                or not isinstance(data.get("entries"), list) or data.get("truncated") or data.get("complete") is not True
                or type(data.get("entry_count")) is not int or data["entry_count"] != len(data["entries"])):
            await invalid("Ledger snapshot is incomplete or belongs to another world.")
        keys = [(row.get("object"), row.get("field")) for row in data["entries"] if isinstance(row, dict)]
        if (len(keys) != len(data["entries"]) or any(not isinstance(x, str) or not x for key in keys for x in key)
                or len(set(keys)) != len(keys)):
            await invalid("Ledger field identities are invalid or duplicated.")
        if tick_unknown:
            await invalid("Ledger snapshot has no valid game tick.")
        return raw, data

    async def assess(self):
        import copy
        import json

        raw, data = await self.snapshot()
        response = raw["response"]
        colony, epoch, tick = response["colony_id"], response["load_epoch"], response["game_tick"]
        opened = await self.service.call("takeovers", colony)
        identities = {(event["payload"]["object"], event["payload"]["field"]) for event in opened}
        uncertain = set()
        events = await self.service.call("events_by_kind", colony, ("user.return", "handover.rebased"))
        baselines = {}
        previous = {}
        for event in events:
            p = event["payload"]
            baseline = p.get("accepted_baseline") if event["kind"] == "user.return" else p.get("baseline")
            if isinstance(baseline, dict) and isinstance(baseline.get("field"), dict):
                field = baseline["field"]
                key = (field["object"], field["field"])
                if event["kind"] == "user.return":
                    previous[key] = (event["seq"], event["load_epoch"])
                if event["load_epoch"] == epoch:
                    baselines[key] = field
        snapshot_seq = await self.service.call("record", colony, epoch, tick, "plugin", "takeover.snapshot", {"valid": True, "observation": copy.deepcopy(raw), "tick_unknown": False})
        for row in data["entries"]:
            if not isinstance(row, dict) or not isinstance(row.get("object"), str) or not isinstance(row.get("field"), str):
                raise ToolExecutionError("takeover_unknown", "Ledger contains an invalid field identity.")
            key = (row["object"], row["field"])
            complete = (row.get("current_status") == "observed" and "current_value" in row and "value" in row
                        and type(row.get("may_change_by_game")) is bool and isinstance(row.get("changeset_id"), str)
                        and type(row.get("tick")) is int)
            prior = previous.get(key)
            if (complete and key not in identities and key not in baselines and prior is not None
                    and prior[1] != epoch):
                await self.service.call("record", colony, epoch, tick, "plugin", "handover.rebased",
                    {"return_seq": prior[0], "snapshot_seq": snapshot_seq, "baseline": {"field": copy.deepcopy(row)}})
                baselines[key] = row
            baseline = baselines.get(key)
            same_version = baseline is not None and all(baseline.get(k) == row.get(k) for k in ("changeset_id", "tick", "value"))
            expected = baseline["current_value"] if same_version else row.get("value")
            different = complete and json.dumps(row["current_value"], sort_keys=True) != json.dumps(expected, sort_keys=True)
            if not complete:
                uncertain.add(key)
            decision = "takeover" if different and row["may_change_by_game"] is False else ("unknown" if not complete else "no_takeover")
            await self.service.call("record", colony, epoch, tick, "plugin", "takeover.assessment",
                                    {"decision": decision, "field": copy.deepcopy(row), "snapshot_seq": snapshot_seq,
                                     "comparison": {"complete": complete,
                                                    "expected": copy.deepcopy(expected) if complete else None,
                                                    "current_value": copy.deepcopy(row.get("current_value")) if complete else None,
                                                    "may_change_by_game": row.get("may_change_by_game") if complete else None,
                                                    "basis": ("accepted_baseline" if same_version else "ledger") if complete else None}})
            if decision == "takeover" and key not in identities:
                await self.service.call("record_takeover", colony, epoch, tick,
                    {"object": key[0], "field": key[1], "value_read": row["current_value"],
                     "ai_value": row["value"], "changeset_id": row["changeset_id"]})
                identities.add(key)
        return identities, uncertain

    async def guard(self, fields):
        identities, uncertain = await self.assess()
        if not isinstance(fields, list) or not fields or any(
                not isinstance(row, dict) or not isinstance(row.get("object"), str)
                or not isinstance(row.get("field"), str) for row in fields):
            raise ToolExecutionError("takeover_unknown", "Write field identities are unavailable.")
        if any((row["object"], row["field"]) in identities for row in fields):
            blocked = [{"object": row["object"], "field": row["field"]} for row in fields
                       if (row["object"], row["field"]) in identities]
            raise ToolExecutionError("field_taken_over", "A field is controlled by the user; the entire transaction was rejected.",
                                     details={"fields": blocked})
        if any((row["object"], row["field"]) in uncertain for row in fields):
            raise ToolExecutionError("takeover_unknown", "The current state of a ledger field is unknown.")

    async def _fields(self, operation, arguments):
        if self.fields_provider is None:
            raise ToolExecutionError("takeover_unknown", "Native field identities are unavailable.")
        bound = self.identity()
        result = await self.fields_provider(operation, arguments)
        raw = result.to_dict()
        response = raw.get("response", {})
        payload = response.get("payload", {})
        data = payload.get("data", {})
        valid = not (not isinstance(data, dict) or not bound or self.identity() != bound or
                (response.get("connection_epoch"), response.get("colony_id"), response.get("load_epoch")) != tuple(bound)
                or payload.get("status") != "ok" or payload.get("issues") or data.get("truncated")
                or data.get("complete") is not True or not isinstance(data.get("fields"), list)
                or type(data.get("field_count")) is not int or data["field_count"] != len(data["fields"]))
        fields = data["fields"] if valid else []
        keys = [(row.get("object"), row.get("field")) for row in fields if isinstance(row, dict)]
        valid = (valid and len(keys) == len(fields)
                 and not any(not isinstance(x, str) or not x for key in keys for x in key)
                 and len(set(keys)) == len(keys)
                 and (operation != "write_undo_fields" or data.get("status") == "available"))
        if operation == "write_field_identities" and valid:
            valid = (all(type(row.get("op")) is int for row in fields)
                     and {row["op"] for row in fields} == set(range(len(arguments["changeset"]["ops"]))))
        if bound:
            tick = response.get("game_tick")
            await self.service.call("record", bound[1], bound[2], tick if type(tick) is int and tick >= 0 else 0,
                                    "plugin", "takeover.identities",
                                    {"valid": valid, "operation": operation, "arguments": arguments,
                                     "observation": raw, "tick_unknown": type(tick) is not int or tick < 0})
        if not valid:
            raise ToolExecutionError("takeover_unknown", "Native field identities are incomplete, invalid or duplicated.")
        return fields

    async def guard_changeset(self, changeset):
        bound = self.identity()
        fields = await self._fields("write_field_identities", {"changeset": changeset})
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during field resolution.")
        await self.guard(fields)
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during takeover assessment.")

    async def guard_undo(self, token):
        bound = self.identity()
        fields = await self._fields("write_undo_fields", {"token": token})
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during field resolution.")
        await self.guard(fields)
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during takeover assessment.")
