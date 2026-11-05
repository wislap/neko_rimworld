"""Validate planner completion claims against durable native evidence."""


async def validate_planner_completion(service, entry, colony_id, load_epoch, setup, observations):
    import uuid

    from ..agent.write.verification import verify_op
    from ..game.write_catalog import validate_write_catalog
    from ..serialization import canonical

    if entry.get("kind") != "task":
        raise ValueError("Only a task with its own commit receipts can be committed or verified")
    ids = entry.get("changesets")
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError("Cite the actual changeset IDs returned by successful commits")
    bound = tuple(setup.write_identity()) if setup and setup.write_identity and setup.write_identity() else ()
    if len(bound) != 3 or bound[1:] != (colony_id, load_epoch):
        raise ValueError("A current write connection is required to validate receipts")
    directory = (
        setup.write_catalog.get("colony_write", setup.write_catalog) if isinstance(setup.write_catalog, dict) else None
    )
    try:
        validate_write_catalog(directory)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("A valid current write catalog is required") from exc
    events = await service.call("planner_completion_events", colony_id)
    receipts = []
    for identifier in ids:
        try:
            if uuid.UUID(identifier).hex != identifier:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Changesets must be actual UUIDs returned by commit")
        matches = []
        for event in events:
            write = event.get("payload", {}).get("write", {})
            change, receipt, envelope = write.get("changeset", {}), write.get("receipt", {}), write.get("envelope", {})
            payload = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
            if (
                event.get("kind") == "planner.progress"
                and event.get("source") == "planner"
                and event.get("load_epoch") == load_epoch
                and write.get("stage") == "result"
                and write.get("mode") == "commit"
                and write.get("origin") == "native"
                and write.get("outcome") == "known"
                and write.get("transport_state") == "confirmed_receipt"
                and not write.get("error")
                and write.get("catalog_id") == directory["catalog_id"]
                and (write.get("connection_epoch"), write.get("colony_id"), write.get("load_epoch")) == bound
                and (envelope.get("connection_epoch"), envelope.get("colony_id"), envelope.get("load_epoch")) == bound
                and envelope.get("type") == "write_result"
                and envelope.get("request_id") == write.get("request_id")
                and bool(write.get("request_id"))
                and change.get("author") == "planner"
                and change.get("load_epoch") == load_epoch
                and isinstance(change.get("ops"), list)
                and bool(change["ops"])
                and change.get("changeset_id") == identifier
                and change.get("entry_id") == entry.get("entry_id")
                and receipt.get("status") == "committed"
                and receipt.get("changeset_id") == identifier
                and receipt.get("load_epoch") == load_epoch
                and payload.get("status") == "ok"
                and not payload.get("error_code")
                and not payload.get("issues")
                and isinstance(payload.get("data"), dict)
                and canonical(payload["data"]) == canonical(receipt)
                and type(envelope.get("game_tick")) is int
            ):
                queued = [
                    prior
                    for prior in events
                    if prior.get("seq", 0) < event.get("seq", 0)
                    and prior.get("kind") == "planner.progress"
                    and prior.get("source") == "planner"
                    and prior.get("load_epoch") == load_epoch
                    and all(
                        prior.get("payload", {}).get("write", {}).get(k) == write.get(k)
                        for k in (
                            "mode",
                            "origin",
                            "connection_epoch",
                            "colony_id",
                            "load_epoch",
                            "request_id",
                            "changeset_id",
                            "idempotency_key",
                            "attempt",
                        )
                    )
                    and prior.get("payload", {}).get("write", {}).get("stage") == "queued"
                ]
                if (
                    write.get("changeset_id") == identifier
                    and write.get("idempotency_key") == change.get("idempotency_key")
                    and write.get("idempotency_key")
                    and type(write.get("attempt")) is int
                    and queued
                ):
                    matches.append(write)
        if not matches:
            raise ValueError("No successful current-connection commit receipt for this task and changeset")
        receipts.append(matches[-1])
    if entry["status"] != "verified":
        return
    work = await service.call("colony_work", colony_id, load_epoch, bound[0])
    plans = [plan for plan in work["construction"]["plans"] if plan["task_id"] == entry.get("entry_id")]
    if plans and not all(plan["construction_complete"] for plan in plans):
        raise ValueError("Construction verification requires current native completion for every effective element of the adopted plan revision")
    cited = entry.get("evidence")
    if not isinstance(cited, list) or not cited:
        raise ValueError("Cite later original observations for every committed operation")
    observations = list(observations)
    observations += [e.get("payload", {}).get("observation") for e in events if e.get("kind") == "planner.progress"]
    observations = [o for o in observations if isinstance(o, dict) and o.get("observation_id") in cited]
    if any(not isinstance(identifier, str) or not identifier for identifier in cited) or set(cited) != {
        o.get("observation_id") for o in observations
    }:
        raise ValueError("Evidence IDs must name original observations available in the journal")
    if any(
        (
            o.get("response", {}).get("connection_epoch"),
            o.get("response", {}).get("colony_id"),
            o.get("response", {}).get("load_epoch"),
        )
        != bound
        for o in observations
    ):
        raise ValueError("Evidence must belong to the current connection, colony and epoch")
    for write in receipts:
        tick = write["envelope"]["game_tick"]
        change = write["changeset"]
        identity_events = [
            e
            for e in events
            if e.get("kind") == "takeover.identities"
            and e.get("load_epoch") == load_epoch
            and e.get("payload", {}).get("valid") is True
            and e["payload"].get("operation") == "write_field_identities"
            and e["payload"].get("arguments", {}).get("changeset") == change
            and e["payload"].get("observation", {}).get("response", {}).get("connection_epoch") == bound[0]
            and e["payload"].get("observation", {}).get("response", {}).get("colony_id") == colony_id
            and e["payload"].get("observation", {}).get("response", {}).get("load_epoch") == load_epoch
        ]
        if not identity_events:
            raise ValueError("Original field identity evidence is unavailable for this changeset")
        data = identity_events[-1]["payload"]["observation"]["response"]["payload"].get("data", {})
        fields = data.get("fields", [])
        if not isinstance(fields, list) or data.get("complete") is not True or data.get("field_count") != len(fields):
            raise ValueError("Original field identity evidence is incomplete")
        for index, op in enumerate(change["ops"]):
            mapped = [field for field in fields if isinstance(field, dict) and field.get("op") == index]
            if not mapped or not all(
                isinstance(f.get("object"), str) and isinstance(f.get("field"), str) for f in mapped
            ):
                raise ValueError("Original field identities are missing for a committed operation")
            if not any(
                verify_op(directory, op, observation, *bound, tick, write["receipt"], mapped)
                for observation in observations
            ):
                raise ValueError(
                    "Read each changed field after the commit tick and cite the matching original observation"
                )
    if tuple(setup.write_identity()) != bound:
        raise ValueError("Connection or loaded world changed during verification")
