"""Frontdesk controls for releasing journal-derived field takeovers."""
from ..tool_contracts import ToolExecutionError, ToolSpec


def policy_protection_capability(service, game, received_utterances, *, binding_check=None):
    """Let the frontdesk preserve or release an explicit user policy constraint."""
    async def protect(args):
        try:
            if (not isinstance(args, dict) or not {"utterance_seq", "protection"} <= args.keys()
                    or args.keys() - {"utterance_seq", "protection", "release_seq"}
                    or type(args["utterance_seq"]) is not int or args["utterance_seq"] not in received_utterances):
                raise ValueError("Cite user words delivered to this run")
            original = await service.call("event", game.colony_id, args["utterance_seq"])
            if original is None or original.get("kind") != "user.utterance":
                raise ValueError("A policy protection cites original user words")
            seq = await service.call("record_policy_protection", game.colony_id, game.load_epoch, game.game_tick,
                                     utterance_seq=args["utterance_seq"], protection=args["protection"],
                                     release_seq=args.get("release_seq"), command_binding_check=binding_check)
            return {"event_seq": seq, "protection": args["protection"],
                    "released": args.get("release_seq") is not None}
        except (ValueError, KeyError, TypeError) as error:
            raise ToolExecutionError("policy_protection_invalid", str(error)) from error

    return ToolSpec("policy_protection", "Record a user-requested policy protection, citing original user words received in this run. "
                    "reference protects a person's assigned policy; policy_content protects a policy's content; effective_rules protects "
                    "the rules a person actually uses, including changes through shared policy content. "
                    "Release requires release_seq naming the original protection, the same protection object and new user words. "
                    "A planner scope assessment never grants or releases this protection.",
                    {"type": "object", "additionalProperties": False, "required": ["utterance_seq", "protection"],
                     "properties": {"utterance_seq": {"type": "integer", "minimum": 1},
                                    "release_seq": {"type": "integer", "minimum": 1},
                                    "protection": {"type": "object", "additionalProperties": False,
                                        "required": ["kind"], "properties": {
                                            "kind": {"enum": ["reference", "policy_content", "effective_rules"]},
                                            "pawn_id": {"type": "string", "minLength": 1},
                                            "policy_id": {"type": "string", "minLength": 1}}}}},
                    protect, exclusive=True)


def return_capability(service, game, received_utterances, setup_provider=None, *, binding_check=None):
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
            control = TakeoverControl(service, setup.ledger_query, setup.write_identity, setup.write_fields_query,
                                      scoped_provider=getattr(setup, "scoped_ledger_query", None))
            opened = await service.call("takeovers", game.colony_id)
            selected = next((item for item in opened if item["seq"] == takeover_seq), None)
            if selected is None:
                raise ValueError("The takeover is not open")
            selected_scope = {key: selected["payload"][key] for key in ("object", "field")}
            raw, data = await control.snapshot(fields=[selected_scope])
            if (raw["response"]["colony_id"], raw["response"]["load_epoch"]) != (game.colony_id, game.load_epoch):
                raise ValueError("The game changed before return")
            opened = await service.call("takeovers", game.colony_id)
            takeover = next((item for item in opened if item["seq"] == takeover_seq), None)
            if takeover is None:
                raise ValueError("The takeover is not open")
            key = (takeover["payload"]["object"], takeover["payload"]["field"])
            rows = [row for row in data["entries"] if (row.get("object"), row.get("field")) == key]
            manual_baseline = None
            if not rows:
                manual = [source for source in data.get("manual_sources", [])
                          if (source.get("object"), source.get("field")) == key and source.get("ownership") is None]
                if len(manual) != 1:
                    raise ValueError("The unowned manual area field cannot be verified")
                source = manual[0]
                current = source.get("current_value")
                if (source.get("current_status") != "observed" or not isinstance(current, dict)
                        or type(current.get("enabled")) is not bool
                        or current.get("native_instance") != source["new_value"].get("native_instance")
                        or current.get("manual_id") != source.get("manual_id")):
                    raise ValueError("The unowned manual area field has no current generation-bound value")
                manual_baseline = {"object": key[0], "field": key[1], "manual_id": source["manual_id"], "current_value": current}
                accepted_baseline = {"manual_area": manual_baseline, "observation": raw}
            else:
                if (len(rows) != 1 or rows[0].get("current_status") != "observed"
                        or "current_value" not in rows[0] or "value" not in rows[0]
                        or type(rows[0].get("may_change_by_game")) is not bool
                        or not isinstance(rows[0].get("changeset_id"), str)
                        or type(rows[0].get("tick")) is not int):
                    raise ValueError("The taken-over field cannot be verified")
                accepted_baseline = {"field": rows[0], "observation": raw}
            if setup.write_identity() != (raw["response"]["connection_epoch"], game.colony_id, game.load_epoch):
                raise ValueError("The game changed before return")
            seq = await service.call("record_return", game.colony_id, game.load_epoch,
                                     raw["response"]["game_tick"], takeover_seq=takeover_seq,
                                     utterance_seq=utterance_seq,
                                     accepted_baseline=accepted_baseline,
                                     command_binding_check=binding_check or (lambda: setup.write_identity() == (
                                         raw["response"]["connection_epoch"], game.colony_id, game.load_epoch)))
            return {"event_seq": seq, "takeover_seq": takeover_seq, "utterance_seq": utterance_seq,
                    "object": key[0], "field": key[1],
                    **({"manual_id": manual_baseline["manual_id"], "prior_ai_ownership": False} if manual_baseline else {})}
        except (ValueError, KeyError) as error:
            raise ToolExecutionError("return_invalid", str(error)) from error

    return ToolSpec("return_takeover", "Return an open taken-over field only when the user requests it in words received in this run. "
                    "Only a successful result confirms a recorded return; cite its event_seq, object and field. "
                    "A user request, intent or lack of an open takeover does not itself confirm a return.",
                    {"type": "object", "properties": {"takeover_seq": {"type": "integer"},
                     "utterance_seq": {"type": "integer"}}, "required": ["takeover_seq", "utterance_seq"],
                     "additionalProperties": False}, release, exclusive=True)


class TakeoverControl:
    """Use the journal as the sole durable source of open takeovers."""

    def __init__(self, service, provider, identity, fields_provider, *, scoped_provider=None):
        self.service, self.provider, self.identity = service, provider, identity
        self.fields_provider = fields_provider
        self._manual_area_complete = None
        self.scoped_provider = scoped_provider

    async def guard_task(self, entry_id):
        """Tasks persist across loads; the subsequent write evidence must use the current binding."""
        bound = self.identity()
        if not bound or len(bound) != 3:
            raise ToolExecutionError("world_changed", "A current colony connection is required.")
        entries = await self.service.call("board", bound[1])
        entry = next((row for row in entries if row.get("entry_id") == entry_id), None)
        if (entry is None or entry.get("author") != "planner" or entry.get("kind") != "task"
                or entry.get("status") == "closed"):
            raise ToolExecutionError("task_required", "Create or select an existing open planner task on this colony board before submitting a changeset.")
        if self.identity() != bound:
            raise ToolExecutionError("world_changed", "Connection or loaded world changed.")

    async def snapshot(self, *, fields=None):
        bound = self.identity()
        if not bound or (self.provider is None and self.scoped_provider is None):
            raise ToolExecutionError("takeover_unknown", "Current ledger observation is unavailable.")
        selected = None
        if fields is not None and self.scoped_provider is not None:
            selected = sorted(({"object": row["object"], "field": row["field"]} for row in fields),
                              key=lambda row: (row["object"], row["field"]))
            if not 1 <= len(selected) <= 64 or len({(row["object"], row["field"]) for row in selected}) != len(selected):
                raise ToolExecutionError("takeover_unknown", "Select 1..64 unique native ledger fields.")
            observation = await self.scoped_provider(selected)
        else:
            if self.provider is None:
                raise ToolExecutionError("takeover_unknown", "Whole-ledger observation is unavailable.")
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
        if selected is not None:
            scope = data.get("scope", {})
            requested = {(row["object"], row["field"]) for row in selected}
            if (scope.get("kind") != "fields" or scope.get("fields") != selected
                    or not set(keys) <= requested
                    or any((source.get("object"), source.get("field")) not in requested for source in data.get("manual_sources", []))):
                await invalid("Native ledger scope does not match the actual requested fields.")
        if tick_unknown:
            await invalid("Ledger snapshot has no valid game tick.")
        if "manual_sources_complete" in data and type(data["manual_sources_complete"]) is not bool:
            await invalid("Native manual area coverage status is invalid.")
        self._manual_area_complete = data.get("manual_sources_complete")
        if data.get("manual_sources") or any(row.get("manual_source") is not None for row in data["entries"]):
            try:
                await self.service.call("ingest_area_manual_snapshot", response["colony_id"], response["load_epoch"], tick,
                                        response["connection_epoch"], raw,
                                        command_binding_check=lambda: self.identity() == bound)
            except ValueError as error:
                raise ToolExecutionError("takeover_unknown", "Native manual area provenance is invalid: " + str(error)) from error
        return raw, data

    async def assess(self, *, fields=None):
        import copy
        import json

        raw, data = await self.snapshot(fields=fields)
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
            user_cancelled = (complete and row["field"].startswith("construction:")
                              and isinstance(row.get("current_value"), dict)
                              and row["current_value"].get("stage") == "user_cancelled")
            decision = "takeover" if different and (row["may_change_by_game"] is False or user_cancelled) else ("unknown" if not complete else "no_takeover")
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
                     "ai_value": row["value"], "changeset_id": row["changeset_id"]},
                    command_binding_check=lambda: self.identity() == (
                        raw["response"]["connection_epoch"], colony, epoch))
                identities.add(key)
        return identities, uncertain

    async def guard(self, fields):
        if not isinstance(fields, list) or not fields or any(
                not isinstance(row, dict) or not isinstance(row.get("object"), str)
                or not isinstance(row.get("field"), str) for row in fields):
            raise ToolExecutionError("takeover_unknown", "Write field identities are unavailable.")
        identities, uncertain = await self.assess(fields=fields)
        if any((row["object"], row["field"]) in identities for row in fields):
            blocked = [{"object": row["object"], "field": row["field"]} for row in fields
                       if (row["object"], row["field"]) in identities]
            raise ToolExecutionError("field_taken_over", "A field is controlled by the user; the entire transaction was rejected.",
                                     details={"fields": blocked})
        if any((row["object"], row["field"]) in uncertain for row in fields):
            raise ToolExecutionError("takeover_unknown", "The current state of a ledger field is unknown.")
        if self._manual_area_complete is False and any(row["field"].startswith("area_flag:") for row in fields):
            raise ToolExecutionError("takeover_unknown", "Native manual area provenance coverage is unavailable for these cells.")

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
        await self._guard_construction(changeset)
        await self._guard_policy_effects(changeset.get("ops", ()))
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during takeover assessment.")

    async def _guard_construction(self, changeset):
        from .construction_evidence import native_element

        placements = [op for op in changeset.get("ops", ()) if op.get("op") in {
            "place_blueprint", "cancel_blueprint", "restart_blueprint"}]
        if not placements:
            return
        bound = self.identity()
        if not bound:
            raise ToolExecutionError("world_changed", "A current construction world is required")
        state = await self.service.call("colony_work", bound[1], bound[2], bound[0])
        for op in placements:
            cancellation = op["op"] == "cancel_blueprint"
            value = op if cancellation else op.get("value", {})
            plan = next((row for row in state["construction"]["plans"] if row["plan_id"] == value.get("plan_id")), None)
            if plan is None or plan["task_id"] != changeset.get("entry_id"):
                raise ToolExecutionError("construction_not_adopted", "Adopt this construction plan for the current task before placing orders")
            if not cancellation:
                try:
                    await self.service.call("construction_dispatch_authority", bound[1], bound[2], plan["plan_id"], [value.get("element_id")])
                except ValueError as error:
                    raise ToolExecutionError("construction_intent_changed", str(error)) from error
            definition = next((row for row in plan["plan"]["elements"] if row["id"] == value.get("element_id")), None)
            status = next((row for row in plan["elements"] if row["id"] == value.get("element_id")), None)
            if definition is None or status is None or op.get("map") != plan["plan"]["map_id"]:
                raise ToolExecutionError("construction_intent_changed", "Choose an element of this adopted plan and map")
            if cancellation:
                if (op.get("value") is not False or status["intent"] != "cancelled" or status["stage"] != "blueprint"
                        or (status.get("stop") or {}).get("source") != "planner"):
                    raise ToolExecutionError("construction_intent_changed", "Record the planner's cancellation intent before cancelling its original blueprint")
                continue
            required_stages = {"unobserved"} if op["op"] == "place_blueprint" else {"model_cancelled", "destroyed", "user_cancelled"}
            if (status["intent"] != "continue" or status["stage"] not in required_stages
                    or value != native_element(plan["plan_id"], plan["plan"], definition)):
                raise ToolExecutionError("construction_intent_changed", "Construction must match the current adopted revision and its continuing, unobserved element")
        if self.identity() != bound:
            raise ToolExecutionError("world_changed", "The construction world changed during intent validation")

    async def guard_undo(self, token):
        bound = self.identity()
        fields = await self._fields("write_undo_fields", {"token": token})
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during field resolution.")
        await self.guard(fields)
        await self._guard_policy_effects((), undo_fields=fields)
        if self.identity() != bound:
            raise ToolExecutionError("takeover_unknown", "The world changed during takeover assessment.")

    async def _policy_protections(self, colony):
        """Only frontdesk records anchored to actual user words carry authority."""
        events = await self.service.call("events_by_kind", colony,
                                         ("user.policy_protection", "user.policy_protection_released"))
        active = {}
        for event in events:
            if event.get("source") != "frontdesk":
                continue
            payload = event.get("payload", {})
            original = await self.service.call("event", colony, payload.get("utterance_seq"))
            if original is None or original.get("kind") != "user.utterance":
                continue
            if event["kind"] == "user.policy_protection":
                active[event["seq"]] = payload["protection"]
            elif payload.get("protection_seq") in active:
                if active[payload["protection_seq"]] == payload.get("protection"):
                    active.pop(payload["protection_seq"])
        return list(active.values())

    async def _policy_snapshot(self, identifiers):
        bound = self.identity()
        if not bound or self.fields_provider is None:
            raise ToolExecutionError("policy_scope_unknown", "Native policy membership is unavailable.")
        observation = await self.fields_provider("inspect_food_policies", {"ids": sorted(identifiers)})
        raw = observation.to_dict()
        response = raw.get("response", {})
        payload = response.get("payload", {})
        data = payload.get("data", {})
        rows = data.get("objects", []) if isinstance(data, dict) else []
        valid = (self.identity() == bound
                 and tuple(response.get(key) for key in ("connection_epoch", "colony_id", "load_epoch")) == tuple(bound)
                 and payload.get("status") == "ok" and not payload.get("issues")
                 and isinstance(data, dict) and data.get("complete") is True and data.get("users_complete") is True
                 and isinstance(rows, list) and len(rows) == len(identifiers)
                 and all(isinstance(row, dict) and row.get("policy_id") in identifiers
                         and row.get("users_complete") is True and isinstance(row.get("users"), list)
                         and all(isinstance(pawn, str) and pawn for pawn in row["users"])
                         and len(set(row["users"])) == len(row["users"]) for row in rows)
                 and len({row["policy_id"] for row in rows}) == len(identifiers))
        tick = response.get("game_tick")
        await self.service.call("record", bound[1], bound[2], tick if type(tick) is int and tick >= 0 else 0,
                                "plugin", "takeover.policy_snapshot", {"valid": valid, "observation": raw})
        if not valid:
            raise ToolExecutionError("policy_scope_unknown", "Native policy membership is incomplete or belongs to another world.")
        return {row["policy_id"]: row for row in rows}

    async def _guard_policy_effects(self, ops, *, undo_fields=()):
        from .policy_scope import check_scope

        bound = self.identity()
        relevant = [op for op in ops if op.get("op") in {"set_food_policy_content", "delete_food_policy", "set_food_policy"}]
        content_ids = {op["policy"] for op in relevant if op["op"] != "set_food_policy"}
        content_ids.update(row["object"] for row in undo_fields if row["field"] == "food_policy_content")
        assignment_ids = {row["object"] for row in undo_fields if row["field"] == "food_policy"}
        if not relevant and not content_ids and not assignment_ids:
            return
        if not bound or len(bound) != 3:
            raise ToolExecutionError("world_changed", "The loaded world changed before policy protection assessment.")
        protections = await self._policy_protections(bound[1])
        snapshots = await self._policy_snapshot(content_ids) if content_ids else {}
        for op in relevant:
            if op["op"] == "set_food_policy":
                affected = op["pawn"]
                old_policy = op.get("expect", {}).get("policy_id")
                new_policy = op.get("value", {}).get("policy_id")
                blocked = [item for item in protections if item["kind"] in {"reference", "effective_rules"}
                           and item.get("pawn_id") == affected
                           and (item["kind"] == "reference" or item.get("policy_id") in {None, old_policy, new_policy})]
                if blocked:
                    raise ToolExecutionError("policy_effect_protected", "The person's policy or effective rules are protected by the user.",
                                             details={"protections": blocked})
                continue
            current = snapshots[op["policy"]]
            impact = op.get("value", {}).get("impact", {})
            if impact.get("mode") == "fixed_users":
                members = impact.get("users")
                if (not isinstance(members, list) or any(not isinstance(member, str) or not member for member in members)
                        or len(set(members)) != len(members) or set(members) != set(current["users"])):
                    raise ToolExecutionError("policy_scope_conflict", "The policy's current users differ from the fixed effect scope.")
                mode = "members"
            elif impact.get("mode") == "dynamic_shared" and "users" not in impact:
                members, mode = [], "shared"
            else:
                raise ToolExecutionError("policy_scope_invalid", "Choose an explicit fixed-user or dynamic shared effect scope.")
            identifier = current["policy_id"]
            assessment = check_scope({"policy_id": identifier, "scope_id": identifier, "mode": mode, "members": members},
                                     {"policy_id": identifier, "scope_id": identifier, "members": current["users"], "complete": True},
                                     protections=protections)
            if not assessment["allowed"]:
                raise ToolExecutionError("policy_effect_protected", "The shared edit affects user-protected rules.", details=assessment)
        for identifier in content_ids:
            # Undo is subject to the same current dependents; its native CAS
            # also refuses a changed membership before applying old content.
            if any(row["object"] == identifier and row["field"] == "food_policy_content" for row in undo_fields):
                current = snapshots[identifier]
                assessment = check_scope({"policy_id": identifier, "scope_id": identifier, "mode": "shared"},
                                         {"policy_id": identifier, "scope_id": identifier, "members": current["users"], "complete": True},
                                         protections=protections)
                if not assessment["allowed"]:
                    raise ToolExecutionError("policy_effect_protected", "Undo would alter user-protected shared rules.", details=assessment)
        if any(item.get("pawn_id") in assignment_ids and item["kind"] in {"reference", "effective_rules"} for item in protections):
            raise ToolExecutionError("policy_effect_protected", "Undo would alter a user-protected policy assignment.")
