import pytest

from plugin.plugins.neko_rimworld.tool_contracts import ToolExecutionError
from plugin.plugins.neko_rimworld.workspace.takeovers import TakeoverControl, return_capability


class Observation:
    def __init__(self, data, epoch="e"):
        self.data = dict(data)
        self.data["complete"] = True
        if "entries" in self.data:
            self.data["entry_count"] = len(self.data["entries"])
        if "fields" in self.data:
            self.data["field_count"] = len(self.data["fields"])
        self.epoch = epoch

    def to_dict(self):
        return {"response": {"connection_epoch": "c", "colony_id": "x", "load_epoch": self.epoch, "game_tick": 10,
                "payload": {"status": "ok", "data": self.data}}}


class Journal:
    def __init__(self):
        self.events = []

    async def call(self, name, *args, **kwargs):
        if name == "events_by_kind":
            return [e for e in self.events if e["kind"] in args[1]]
        if name == "takeovers":
            returned = {e["payload"]["takeover_seq"] for e in self.events if e["kind"] == "user.return"}
            return [e for e in self.events if e["kind"] == "user.takeover" and e["seq"] not in returned]
        if name == "event":
            return {"kind": "user.utterance"}
        if name == "record":
            kind, payload = args[4:6]
        elif name == "record_takeover":
            kind, payload = "user.takeover", args[3]
        elif name == "record_return":
            kind, payload = "user.return", kwargs
        else:
            raise AssertionError(name)
        self.events.append({"kind": kind, "payload": payload, "seq": len(self.events) + 1, "load_epoch": args[1]})
        return len(self.events)


@pytest.mark.asyncio
async def test_takeover_return_and_new_write():
    journal = Journal()
    row = {"object": "p", "field": "work", "value": 1, "current_value": 2,
           "current_status": "observed", "may_change_by_game": False, "changeset_id": "one", "tick": 1}
    async def provider():
        return Observation({"ledger_status": "valid", "entries": [row.copy()]})
    async def fields(operation, arguments):
        return Observation({"fields": [{"object": "p", "field": "work", "op": 0}]})
    def identity():
        return ("c", "x", "e")
    control = TakeoverControl(journal, provider, identity, fields)
    with pytest.raises(ToolExecutionError, match="controlled"):
        await control.guard_changeset({"ops": [{}]})
    takeover = (await journal.call("takeovers", "x"))[0]["seq"]
    class Setup:
        ledger_query = staticmethod(provider)
        write_identity = staticmethod(identity)
        write_fields_query = staticmethod(fields)
    class Game:
        colony_id, load_epoch, game_tick = "x", "e", 10
    async def setup():
        return Setup()
    tool = return_capability(journal, Game(), {42}, setup)
    await tool.handler({"takeover_seq": takeover, "utterance_seq": 42})
    await control.guard_changeset({"ops": [{}]})
    row.update(value=3, current_value=3, changeset_id="two", tick=11)
    await control.guard_changeset({"ops": [{}]})
    row["current_value"] = 4
    with pytest.raises(ToolExecutionError, match="controlled"):
        await control.guard_changeset({"ops": [{}]})


@pytest.mark.asyncio
async def test_unknown_and_game_changes_do_not_create_takeovers():
    journal = Journal()
    row = {"object": "p", "field": "work", "value": 1, "current_value": 2,
           "current_status": "unresolved", "may_change_by_game": None, "changeset_id": "one", "tick": 1}
    async def provider():
        return Observation({"ledger_status": "valid", "entries": [row.copy()]})
    def identity():
        return ("c", "x", "e")
    control = TakeoverControl(journal, provider, identity, None)
    with pytest.raises(ToolExecutionError, match="unknown"):
        await control.guard([{"object": "p", "field": "work"}])
    assert not await journal.call("takeovers", "x")
    row.update(current_status="observed", may_change_by_game=True)
    await control.guard([{"object": "p", "field": "work"}])
    assert not await journal.call("takeovers", "x")


@pytest.mark.asyncio
async def test_batch_and_undo_block_open_takeover():
    journal = Journal()
    row = {"object": "p", "field": "work", "value": 1, "current_value": 2,
           "current_status": "observed", "may_change_by_game": False, "changeset_id": "one", "tick": 1}
    async def provider():
        return Observation({"ledger_status": "valid", "entries": [row]})
    async def fields(operation, arguments):
        data = {"fields": [{"object": "p", "field": "work", "op": 0},
                           {"object": "q", "field": "other", "op": 1}]}
        if operation == "write_undo_fields":
            data["status"] = "available"
        return Observation(data)
    def identity():
        return ("c", "x", "e")
    control = TakeoverControl(journal, provider, identity, fields)
    with pytest.raises(ToolExecutionError, match="entire transaction"):
        await control.guard_changeset({"ops": [{}, {}]})
    with pytest.raises(ToolExecutionError, match="entire transaction"):
        await control.guard_undo("token")


@pytest.mark.asyncio
async def test_return_rebases_after_load_without_claiming_user_change():
    journal = Journal()
    row = {"object": "p", "field": "work", "value": 1, "current_value": 2,
           "current_status": "observed", "may_change_by_game": False, "changeset_id": "one", "tick": 1}
    epoch = ["e"]
    async def provider():
        return Observation({"ledger_status": "valid", "entries": [row.copy()]}, epoch[0])
    def identity():
        return ("c", "x", epoch[0])
    control = TakeoverControl(journal, provider, identity, None)
    with pytest.raises(ToolExecutionError):
        await control.guard([{"object": "p", "field": "work"}])
    takeover = (await journal.call("takeovers", "x"))[0]["seq"]
    await journal.call("record_return", "x", "e", 10, takeover_seq=takeover, utterance_seq=7,
                       accepted_baseline={"field": row.copy()})
    # A later AI write establishes version B before the save is reloaded.
    row.update(value=4, current_value=4, changeset_id="two", tick=2)
    await control.guard([{"object": "p", "field": "work"}])
    epoch[0] = "new"
    # The loaded save has an older ledger version and a differing actual value.
    row.update(value=0, current_value=1, changeset_id="older", tick=0)
    await control.guard([{"object": "p", "field": "work"}])
    assert any(e["kind"] == "handover.rebased" for e in journal.events)
    assert not await journal.call("takeovers", "x")
    row["current_value"] = 3
    with pytest.raises(ToolExecutionError, match="controlled"):
        await control.guard([{"object": "p", "field": "work"}])


@pytest.mark.asyncio
async def test_rejects_duplicate_and_incomplete_identity_metadata():
    journal = Journal()
    async def provider():
        return Observation({"ledger_status": "valid", "entries": []})
    async def fields(operation, arguments):
        return Observation({"fields": [{"object": "p", "field": "f", "op": 0},
                                       {"object": "p", "field": "f", "op": 0}]})
    def identity():
        return ("c", "x", "e")
    control = TakeoverControl(journal, provider, identity, fields)
    with pytest.raises(ToolExecutionError, match="duplicated"):
        await control.guard_changeset({"ops": [{}]})
    identity_event = journal.events[-1]
    assert identity_event["kind"] == "takeover.identities"
    assert identity_event["payload"]["valid"] is False
    assert identity_event["payload"]["arguments"] == {"changeset": {"ops": [{}]}}
    assert identity_event["payload"]["observation"]["response"]["payload"]["data"]["field_count"] == 2
    async def incomplete():
        result = Observation({"ledger_status": "valid", "entries": []})
        result.data.pop("complete")
        return result
    control.provider = incomplete
    with pytest.raises(ToolExecutionError, match="incomplete"):
        await control.assess()


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing_complete", "wrong_identity", "bad_tick"])
async def test_invalid_snapshot_retains_original(problem):
    journal = Journal()
    async def provider():
        result = Observation({"ledger_status": "valid", "entries": []},
                             "wrong" if problem == "wrong_identity" else "e")
        if problem == "missing_complete":
            result.data.pop("complete")
        raw = result.to_dict()
        if problem == "bad_tick":
            raw["response"]["game_tick"] = -1
        class Raw:
            def to_dict(self):
                return raw
        return Raw()
    def identity():
        return ("c", "x", "e")
    control = TakeoverControl(journal, provider, identity, None)
    with pytest.raises(ToolExecutionError):
        await control.assess()
    snapshots = [e for e in journal.events if e["kind"] == "takeover.snapshot"]
    assert len(snapshots) == 1
    payload = snapshots[0]["payload"]
    assert payload["valid"] is False
    assert payload["tick_unknown"] is (problem == "bad_tick")
    assert payload["observation"] == (await provider()).to_dict()
