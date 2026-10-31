import copy
from types import SimpleNamespace

import pytest

from ..workspace.planner_completion import validate_planner_completion
from ..workspace.runtime import FrontdeskService
from .test_write_service import IDENTITY, catalog, observation, op


async def wire_commit(journal, directory, initial_observation, action, readback):
    from ..agent.write.service import WriteService
    from ..game.protocol import SessionDescriptor
    from ..game.write_port import BridgeGameWritePort

    connection, colony, epoch = IDENTITY

    class Bridge:
        capabilities = ("colony_write.transactions.v1",)

        def commit(self, change, *, expected_connection, expected_session, cancel_event, audit_dispatch):
            assert expected_connection == connection and expected_session.load_epoch == epoch
            audit_dispatch("wire-request", 5, cancel_event)
            data = {
                "status": "committed",
                "changeset_id": change["changeset_id"],
                "load_epoch": epoch,
                "readback": readback,
            }
            envelope = {
                "type": "write_result",
                "request_id": "wire-request",
                "connection_epoch": connection,
                "colony_id": colony,
                "load_epoch": epoch,
                "game_tick": 10,
                "payload": {"status": "ok", "data": data, "issues": []},
            }
            return SimpleNamespace(
                request_id="wire-request", payload=SimpleNamespace(status="ok", data=data), to_dict=lambda: envelope
            )

    async def record(event):
        await journal.call("record", colony, epoch, 10, "planner", "planner.progress", {"write": event})

    port = BridgeGameWritePort(Bridge(), connection, SessionDescriptor(colony, epoch, 10, 1))
    service = WriteService(directory, [initial_observation], port, lambda: IDENTITY, record=record)
    await service.commit({"entry_id": "task", "ops": [action]})
    events = await journal.call("events", colony)
    return next(e["payload"]["write"] for e in events if e["payload"].get("write", {}).get("stage") == "result")


@pytest.mark.asyncio
async def test_completion_uses_real_journal_and_scoped_evidence(tmp_path):
    connection, colony, epoch = IDENTITY
    game = SimpleNamespace(colony_id=colony, load_epoch=epoch, game_tick=10)
    journal = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: game)
    setup = SimpleNamespace(write_identity=lambda: IDENTITY, write_catalog=catalog())
    entry = {"entry_id": "task", "kind": "task", "status": "committed", "changesets": ["a" * 32], "evidence": []}
    try:
        with pytest.raises(ValueError):
            await validate_planner_completion(journal, entry, colony, epoch, setup, [])
        write = await wire_commit(
            journal, catalog(), observation(), op(), [{"object": "Pawn1", "field": "care", "value": "new"}]
        )
        change = write["changeset"]
        entry["changesets"] = [change["changeset_id"]]
        await validate_planner_completion(journal, entry, colony, epoch, setup, [])
        await journal.call("record", colony, epoch, 10, "planner", "planner.progress", {"write": write})
        await validate_planner_completion(journal, entry, colony, epoch, setup, [])
        for bad in ("invalid", "b" * 32):
            with pytest.raises(ValueError):
                await validate_planner_completion(journal, {**entry, "changesets": [bad]}, colony, epoch, setup, [])
        with pytest.raises(ValueError):
            await validate_planner_completion(journal, {**entry, "entry_id": "other"}, colony, epoch, setup, [])
        for identity in [(connection, colony, "old"), ("old", colony, epoch)]:
            with pytest.raises(ValueError):
                await validate_planner_completion(
                    journal,
                    entry,
                    colony,
                    epoch,
                    SimpleNamespace(write_identity=lambda: identity, write_catalog=catalog()),
                    [],
                )
        fields = [{"object": "Pawn1", "field": "care", "op": 0}]
        identity_obs = {
            "response": {
                "connection_epoch": connection,
                "colony_id": colony,
                "load_epoch": epoch,
                "payload": {"status": "ok", "data": {"complete": True, "field_count": 1, "fields": fields}},
            }
        }
        await journal.call(
            "record",
            colony,
            epoch,
            10,
            "plugin",
            "takeover.identities",
            {
                "valid": True,
                "operation": "write_field_identities",
                "arguments": {"changeset": change},
                "observation": identity_obs,
            },
        )
        obs = observation()
        obs["request"]["arguments"] = {}
        obs["response"].update(type="query_result", request_id="read", game_tick=10)
        obs["response"]["payload"]["data"]["objects"][0]["facets"]["work"]["medical_care"] = "new"
        verified = {**entry, "status": "verified", "evidence": ["original"]}
        with pytest.raises(ValueError):
            await validate_planner_completion(journal, verified, colony, epoch, setup, [obs])
        obs["response"]["game_tick"] = 11
        await journal.call("record", colony, epoch, 11, "planner", "planner.progress", {"observation": obs})
        await validate_planner_completion(journal, verified, colony, epoch, setup, [])
        for mutate in ("missing", "epoch", "partial"):
            bad = copy.deepcopy(obs)
            if mutate == "missing":
                bad["observation_id"] = "other"
            if mutate == "epoch":
                bad["response"]["load_epoch"] = "old"
            if mutate == "partial":
                bad["response"]["payload"]["data"]["objects"][0]["facets"]["work"]["status"] = "partial"
            bad["observation_id"] = "bad"
            with pytest.raises(ValueError):
                await validate_planner_completion(
                    journal,
                    {**verified, "evidence": ["bad"]},
                    colony,
                    epoch,
                    setup,
                    [] if mutate == "missing" else [bad],
                )
    finally:
        await journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set_bill", "add_production_bill"])
async def test_structural_and_multifield_verification_with_journal(tmp_path, operation):
    import json
    from pathlib import Path

    from ..agent.write.service import _substitute

    directory = json.loads((Path(__file__).parent / "fixtures/colony-write-v1/catalog.json").read_text(encoding="utf8"))
    desc = next(item for item in directory["operations"] if item["op"] == operation)
    types = {"string": "x", "integer": 1, "boolean": True, "object": {}}
    action = {
        name: prop.get("const", types.get(prop.get("type"), "x"))
        for name, prop in desc["schema"]["properties"].items()
        if name in desc["schema"]["required"]
    }
    action["value"] = {"suspended": True, "repeat_count": 1} if operation == "set_bill" else {"recipe": "Meal"}
    spec = desc["observation"]["extract"]
    def resolve(val):
        return _substitute(val, action)

    def put(node, path, value):
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    row = {}
    if spec["identity_path"]:
        put(row, spec["identity_path"], resolve(spec["object_id"]))
    if spec["status_path"]:
        put(row, spec["status_path"], "observed")
    for rule in spec.get("match", []):
        put(row, rule["path"], resolve(rule["value"]))
    if operation == "set_bill":
        value = [{spec["key_field"]: resolve(spec["key"]), **spec.get("row_require", {}), **action["value"]}]
    else:
        value = [{spec["id_field"]: "Bill_new", "bill_kind": "production", "recipe": "Meal"}]
        put(row, spec["complete_path"], True)
        for rule in spec["require"]:
            put(row, rule["path"], resolve(rule["value"]))
        av = spec["available"]
        put(row, av["path"], [{av["key_field"]: "Meal", av["value_field"]: True}])
    put(row, spec["path"], value)
    target = resolve(spec["object_id"])
    fields = (
        [{"object": target, "field": "bill:" + key, "op": 0} for key in action["value"]]
        if operation == "set_bill"
        else [{"object": target, "field": "production_bills", "op": 0}]
    )
    readback = (
        [{"object": f["object"], "field": f["field"], "value": action["value"][f["field"][5:]]} for f in fields]
        if operation == "set_bill"
        else [
            {
                "object": target,
                "field": "production_bills",
                "value": {"bill_ids": ["Bill_new"], "created": {"bill_id": "Bill_new", "recipe": "Meal"}},
            }
        ]
    )
    connection, colony, epoch = IDENTITY
    observation = {
        "observation_id": "post",
        "request": {"operation": desc["observation"]["read"]["native_operation"], "arguments": {}},
        "response": {
            "type": "query_result",
            "request_id": "read",
            "connection_epoch": connection,
            "colony_id": colony,
            "load_epoch": epoch,
            "game_tick": 11,
            "payload": {"status": "ok", "issues": [], "data": {"objects": [row]} if spec["objects_path"] else row},
        },
    }
    identity = {
        "response": {
            "connection_epoch": connection,
            "colony_id": colony,
            "load_epoch": epoch,
            "payload": {"status": "ok", "data": {"complete": True, "field_count": len(fields), "fields": fields}},
        }
    }
    journal = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: None)
    try:
        write = await wire_commit(journal, directory, observation, action, readback)
        change = write["changeset"]
        await journal.call(
            "record",
            colony,
            epoch,
            10,
            "plugin",
            "takeover.identities",
            {
                "valid": True,
                "operation": "write_field_identities",
                "arguments": {"changeset": change},
                "observation": identity,
            },
        )
        await journal.call("record", colony, epoch, 11, "planner", "planner.progress", {"observation": observation})
        entry = {
            "entry_id": "task",
            "kind": "task",
            "status": "verified",
            "changesets": [change["changeset_id"]],
            "evidence": ["post"],
        }
        await validate_planner_completion(
            journal, entry, colony, epoch, SimpleNamespace(write_identity=lambda: IDENTITY, write_catalog=directory), []
        )
    finally:
        await journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["missing", "strategy", "intent", "closed", "other_colony", "task", "prior_epoch_task"]
)
async def test_runtime_rejects_invalid_task_before_native_query_or_send(tmp_path, kind):
    from dataclasses import replace

    from ..agent.runtime.audit import AgentAuditLedger, AgentAuditRegistry
    from ..agent.runtime.loop import AgentRuntime
    from ..agent.runtime.session import AgentRunSession
    from ..game.observation import Observation
    from ..game.query_tools import make_game_tools
    from .test_agent import FakeLogger, settings
    from .test_capability_composition import Script, call
    from .test_semantic_tools import public_source

    connection, colony, epoch = IDENTITY
    current = SimpleNamespace(colony_id=colony, load_epoch=epoch, game_tick=1)
    journal = FrontdeskService(tmp_path / "events.sqlite", None, game_session=lambda: current)
    base = {
        "expected_revision": 0,
        "priority": 1,
        "parent": None,
        "source": {},
        "evidence": [],
        "review_at_tick": None,
        "changesets": [],
        "body": "",
    }
    try:
        await journal.call(
            "submit", colony, epoch, 1, "planner", {**base, "entry_id": "s", "kind": "strategy", "status": "open"}
        )
        if kind == "strategy":
            task_id = "s"
        elif kind == "intent":
            seq = await journal.call("record_utterance", colony, epoch, 1, "help")
            await journal.call(
                "submit",
                colony,
                epoch,
                1,
                "frontdesk",
                {**base, "entry_id": "i", "kind": "intent", "status": "open", "source": {"event_seq": seq}},
            )
            task_id = "i"
        elif kind in ("closed", "task", "other_colony", "prior_epoch_task"):
            target_colony = "other" if kind == "other_colony" else colony
            current.colony_id = target_colony
            current.load_epoch = "previous" if kind == "prior_epoch_task" else epoch
            if target_colony == "other":
                await journal.call(
                    "submit",
                    target_colony,
                    epoch,
                    1,
                    "planner",
                    {**base, "entry_id": "other-s", "kind": "strategy", "status": "open"},
                )
            await journal.call(
                "submit",
                target_colony,
                "previous" if kind == "prior_epoch_task" else epoch,
                1,
                "planner",
                {
                    **base,
                    "entry_id": "t",
                    "kind": "task",
                    "status": "closed" if kind == "closed" else "open",
                    "parent": "other-s" if target_colony == "other" else "s",
                    "source": {"entry_id": "other-s" if target_colony == "other" else "s"},
                },
            )
            current.colony_id, current.load_epoch = colony, epoch
            task_id = "t"
        else:
            task_id = "missing"
        calls = []

        async def query(request):
            calls.append("query")
            raise AssertionError("unexpected read")

        async def fields(operation, arguments):
            calls.append("fields")
            return Observation(
                {
                    "observation_id": "fields",
                    "request": {},
                    "response": {
                        "connection_epoch": connection,
                        "colony_id": colony,
                        "load_epoch": epoch,
                        "game_tick": 1,
                        "payload": {
                            "status": "ok",
                            "data": {
                                "complete": True,
                                "field_count": 1,
                                "fields": [{"object": "Pawn1", "field": "care", "op": 0}],
                            },
                        },
                    },
                }
            )

        async def ledger():
            calls.append("ledger")
            return Observation(
                {
                    "observation_id": "ledger",
                    "request": {},
                    "response": {
                        "connection_epoch": connection,
                        "colony_id": colony,
                        "load_epoch": epoch,
                        "game_tick": 1,
                        "payload": {
                            "status": "ok",
                            "data": {"ledger_status": "valid", "complete": True, "entry_count": 0, "entries": []},
                        },
                    },
                }
            )

        class Port:
            capabilities = []

            async def commit(self, change):
                calls.append("send")
                return {"status": "committed"}

        setup = replace(
            make_game_tools(public_source(), query),
            write_catalog=catalog(),
            write_port=Port(),
            write_identity=lambda: IDENTITY,
            ledger_query=ledger,
            write_fields_query=fields,
        )

        async def provide():
            return setup

        model = Script(
            [call("commit", {"entry_id": task_id, "ops": [op()]}), call("submit_response", {"answer": "done"})]
        )
        session = AgentRunSession.create(
            ledger=AgentAuditLedger("plan"), question="plan", colony_id=colony, role="planner"
        )
        session.takeover_service = journal
        session.observations.append(observation())
        runtime = AgentRuntime(
            tools_provider=provide,
            audit_registry=AgentAuditRegistry(),
            logger=FakeLogger(),
            model_factory=lambda _: model,
        )
        await runtime.execute(session, settings())
        if kind in ("task", "prior_epoch_task"):
            assert calls == ["fields", "ledger", "send"]
        else:
            assert calls == []
            assert "task_required" in str(model.requests[1]["messages"])
    finally:
        await journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "receipt",
        "envelope_data",
        "envelope_type",
        "request",
        "connection",
        "epoch",
        "status",
        "error",
        "source",
        "mode",
        "catalog_id",
        "catalog_contents",
    ],
)
async def test_real_wire_receipt_rejects_tampering_and_catalog_changes(tmp_path, mutation):
    from ..game.write_catalog import catalog_digest

    connection, colony, epoch = IDENTITY
    source = FrontdeskService(tmp_path / "source.sqlite", None, game_session=lambda: None)
    target = FrontdeskService(tmp_path / "target.sqlite", None, game_session=lambda: None)
    try:
        result = await wire_commit(
            source, catalog(), observation(), op(), [{"object": "Pawn1", "field": "care", "value": "new"}]
        )
        events = await source.call("events", colony)
        entry = {
            "entry_id": "task",
            "kind": "task",
            "status": "committed",
            "changesets": [result["changeset"]["changeset_id"]],
        }
        altered_catalog = catalog()
        for event in events:
            payload = copy.deepcopy(event["payload"])
            write = payload.get("write", {})
            if write.get("stage") == "result":
                if mutation == "receipt":
                    write["receipt"]["readback"][0]["value"] = "forged"
                if mutation == "envelope_data":
                    write["envelope"]["payload"]["data"]["readback"][0]["value"] = "forged"
                if mutation == "envelope_type":
                    write["envelope"]["type"] = "query_result"
                if mutation == "request":
                    write["envelope"]["request_id"] = "other"
                if mutation == "connection":
                    write["envelope"]["connection_epoch"] = "old"
                if mutation == "epoch":
                    write["envelope"]["load_epoch"] = "old"
                if mutation == "status":
                    write["envelope"]["payload"]["status"] = "error"
                if mutation == "error":
                    write["envelope"]["payload"]["error_code"] = "failed"
                if mutation == "mode":
                    write["mode"] = "dry_run"
                if mutation == "catalog_id":
                    write["catalog_id"] = "wrong"
                if mutation == "catalog_contents":
                    altered_catalog["modes"] = ["commit"]
                    altered_catalog["catalog_id"] = catalog_digest(altered_catalog)
            actor = "frontdesk" if mutation == "source" and write.get("stage") == "result" else event["source"]
            await target.call("record", colony, epoch, event["game_tick"], actor, event["kind"], payload)
        with pytest.raises(ValueError):
            await validate_planner_completion(
                target,
                entry,
                colony,
                epoch,
                SimpleNamespace(write_identity=lambda: IDENTITY, write_catalog=altered_catalog),
                [],
            )
        invalid = copy.deepcopy(catalog())
        invalid["modes"] = ["commit"]
        with pytest.raises(ValueError, match="valid current write catalog"):
            await validate_planner_completion(
                source,
                entry,
                colony,
                epoch,
                SimpleNamespace(write_identity=lambda: IDENTITY, write_catalog=invalid),
                [],
            )
    finally:
        await source.close()
        await target.close()
