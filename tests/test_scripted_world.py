"""Write transactions on the scripted world: DESIGN 5.5 rules 1-6 and the three injected faults."""

import pytest
from plugin.plugins.neko_rimworld.tests.scripted_world import ScriptedGameWorld
from plugin.plugins.neko_rimworld.tool_contracts import GameWritePort, ReceiptLost, ToolExecutionError

EMPTY_POLICY = {"pause_on_letter_defs": [], "source": "frontdesk"}
PAWN = "Thing_Human123"
BILL = "Bill_88"


def priority(value, expect, *, key="cs-1", epoch=None, work="Doctor", pawn=PAWN):
    return {
        "changeset_id": key, "author": "planner", "entry_id": "t-12", "load_epoch": epoch or "epoch-0",
        "idempotency_key": key,
        "ops": [{"op": "set_work_priority", "pawn": pawn, "work": work, "expect": expect, "value": value,
                 "evidence": "e-1021"}],
    }


def bill(suspended, expect, *, key="cs-b", epoch=None):
    return {
        "changeset_id": key, "author": "planner", "entry_id": "t-12", "load_epoch": epoch or "epoch-0",
        "idempotency_key": key,
        "ops": [{"op": "set_bill", "bill": BILL, "expect": {"suspended": expect},
                 "value": {"suspended": suspended}, "evidence": "e-1030"}],
    }


def world_with(**values):
    world = ScriptedGameWorld()
    world.player_edit(PAWN, "set_work_priority/work=Doctor", values.get("priority", 3))
    world.player_edit(BILL, "set_bill/suspended", values.get("suspended", False))
    return world


@pytest.mark.asyncio
async def test_scripted_world_satisfies_the_write_port():
    assert isinstance(ScriptedGameWorld(), GameWritePort)


# 5.5 rule 1: all fields of a changeset take effect together, or none does.
@pytest.mark.asyncio
async def test_failed_apply_rolls_back_every_field_already_written():
    world = world_with()
    await world.push_policy(EMPTY_POLICY)
    world.fail_next_apply_after(1)
    changeset = priority(1, 3)
    changeset["ops"].append(
        {"op": "set_bill", "bill": BILL, "expect": {"suspended": False}, "value": {"suspended": True}, "evidence": "e"}
    )
    with pytest.raises(RuntimeError, match="Injected failure"):
        await world.commit(changeset)
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3
    assert world.value(BILL, "set_bill/suspended") is False
    assert world.ledger(PAWN, "set_work_priority/work=Doctor") is None
    assert not [e for e in await world.events() if e["kind"] == "write"]
    # Origin is restored too: the next conflict still names the seeding write, not the failed one.
    world.player_edit(BILL, "set_bill/suspended", False)
    receipt = await world.commit(priority(1, 2, key="cs-2"))
    assert receipt["last_writer"] == "user" and receipt["at_tick"] == 0


@pytest.mark.asyncio
async def test_one_conflicting_op_writes_nothing_in_the_changeset():
    world = world_with()
    changeset = priority(1, 3)
    changeset["ops"].append(
        {"op": "set_bill", "bill": BILL, "expect": {"suspended": True}, "value": {"suspended": True}, "evidence": "e"}
    )
    receipt = await world.commit(changeset)
    assert receipt["status"] == "conflict" and receipt["op"] == 1
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3


# 5.5 rule 2: compare the old value of each field the operation writes, not a global version.
@pytest.mark.asyncio
async def test_unrelated_world_change_does_not_conflict():
    world = world_with()
    world.advance(5000)
    world.player_edit(BILL, "set_bill/suspended", True)
    receipt = await world.commit(priority(1, 3))
    assert receipt["status"] == "committed"


@pytest.mark.asyncio
async def test_conflict_receipt_has_the_shape_from_design():
    world = world_with()
    world.advance(182511)
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 2)
    receipt = await world.commit(priority(1, 3))
    assert receipt == {
        "status": "conflict", "op": 0, "expected": 3, "observed": 2, "last_writer": "user", "at_tick": 182511,
    }


@pytest.mark.asyncio
async def test_value_changed_and_changed_back_passes_the_comparison():
    world = world_with()
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 1)
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 3)
    receipt = await world.commit(priority(1, 3))
    assert receipt["status"] == "committed"


@pytest.mark.asyncio
async def test_dry_run_reports_conflicts_without_changing_the_world():
    world = world_with()
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 2)
    verdict = await world.dry_run(priority(1, 3))
    assert verdict["status"] == "conflict" and verdict["observed"] == 2
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 2


@pytest.mark.asyncio
async def test_dict_op_conflict_names_only_the_mismatched_sub_fields():
    world = world_with()
    world.player_edit(BILL, "set_bill/count", 5)
    world.player_edit(BILL, "set_bill/suspended", True)
    changeset = bill(True, False)
    changeset["ops"][0]["expect"]["count"] = 5
    changeset["ops"][0]["value"]["count"] = 10
    receipt = await world.commit(changeset)
    assert receipt["op"] == 0 and receipt["expected"] == {"suspended": False}
    assert receipt["observed"] == {"suspended": True}


@pytest.mark.asyncio
async def test_dry_run_that_would_succeed_changes_nothing():
    world = world_with()
    await world.push_policy(EMPTY_POLICY)
    before = world.save()
    verdict = await world.dry_run(priority(1, 3))
    assert verdict["status"] == "ok" and verdict["preview"][0]["value"] == 1
    assert world.save() == before
    assert world.ledger(PAWN, "set_work_priority/work=Doctor") is None
    assert not [e for e in await world.events() if e["kind"] == "write"]
    assert (await world.commit(priority(1, 3)))["status"] == "committed"


@pytest.mark.asyncio
async def test_dry_run_from_an_old_epoch_is_stale():
    world = world_with()
    world.load(world.save())
    assert await world.dry_run(priority(1, 3)) == {"status": "stale_epoch", "load_epoch": world.load_epoch}


# 5.5 rule 3: the ledger keeps only the last AI write per (object, field).
@pytest.mark.asyncio
async def test_ledger_keeps_only_the_last_ai_write_per_field():
    world = world_with()
    await world.commit(priority(1, 3, key="cs-1"))
    await world.commit(priority(2, 1, key="cs-2"))
    entry = world.ledger(PAWN, "set_work_priority/work=Doctor")
    assert entry["value"] == 2 and entry["changeset_id"] == "cs-2" and entry["author"] == "planner"
    assert world.ledger(BILL, "set_bill/suspended") is None


@pytest.mark.asyncio
async def test_player_edits_never_enter_the_ledger():
    world = world_with()
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 4)
    assert world.ledger(PAWN, "set_work_priority/work=Doctor") is None


@pytest.mark.asyncio
async def test_ledger_survives_save_and_load_with_its_world():
    world = world_with()
    await world.commit(priority(1, 3))
    save = world.save()
    world.load(save)
    assert world.ledger(PAWN, "set_work_priority/work=Doctor")["value"] == 1


@pytest.mark.asyncio
async def test_loading_an_earlier_save_drops_ledger_entries_written_after_it():
    world = world_with()
    save = world.save()
    await world.commit(priority(1, 3))
    world.load(save)
    assert world.ledger(PAWN, "set_work_priority/work=Doctor") is None
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3


# 5.5 rule 4: undo compares too, and never overwrites someone else's change.
@pytest.mark.asyncio
async def test_undo_restores_the_previous_value():
    world = world_with()
    token = (await world.commit(priority(1, 3)))["undo_token"]
    result = await world.undo(token)
    assert result["status"] == "undone" and result["readback"][0]["value"] == 3
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3


@pytest.mark.asyncio
async def test_undo_puts_back_the_ledger_entry_the_commit_replaced():
    world = world_with()
    await world.commit(priority(1, 3, key="cs-1"))
    token = (await world.commit(priority(2, 1, key="cs-2")))["undo_token"]
    assert (await world.undo(token))["status"] == "undone"
    entry = world.ledger(PAWN, "set_work_priority/work=Doctor")
    assert entry["changeset_id"] == "cs-1" and entry["value"] == 1


@pytest.mark.asyncio
async def test_undo_refuses_when_the_field_changed_after_the_commit():
    world = world_with()
    token = (await world.commit(priority(1, 3)))["undo_token"]
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 4)
    result = await world.undo(token)
    assert result["status"] == "conflict" and result["observed"] == 4 and result["last_writer"] == "user"
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 4


@pytest.mark.asyncio
async def test_undo_token_is_consumed_once():
    world = world_with()
    token = (await world.commit(priority(1, 3)))["undo_token"]
    assert (await world.undo(token))["status"] == "undone"
    assert (await world.undo(token))["status"] == "already_undone"
    assert (await world.undo("undo-missing"))["status"] == "unknown_token"


# 5.5 rule 5: a retry with the same idempotency key returns the cached receipt; the cache is per epoch.
@pytest.mark.asyncio
async def test_same_key_retry_returns_the_cached_receipt_without_applying_again():
    world = world_with()
    first = await world.commit(priority(1, 3))
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 5)
    again = await world.commit(priority(1, 3))
    assert again == first
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 5


@pytest.mark.asyncio
async def test_same_key_with_a_different_changeset_is_rejected():
    world = world_with()
    await world.commit(priority(1, 3))
    with pytest.raises(ToolExecutionError) as caught:
        await world.commit(priority(2, 3))
    assert caught.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_receipt_cache_is_discarded_by_a_load():
    world = world_with()
    save = world.save()
    await world.commit(priority(1, 3))
    world.load(save)
    retry = await world.commit(priority(1, 3, epoch=world.load_epoch))
    assert retry["status"] == "committed" and retry["load_epoch"] == world.load_epoch
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 1


@pytest.mark.asyncio
async def test_conflict_receipt_is_cached_under_its_key():
    world = world_with()
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 2)
    first = await world.commit(priority(1, 3))
    world.player_edit(PAWN, "set_work_priority/work=Doctor", 3)
    assert await world.commit(priority(1, 3)) == first
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3


@pytest.mark.asyncio
async def test_lost_receipt_then_load_makes_the_same_key_retry_stale():
    world = world_with()
    save = world.save()
    world.lose_next_receipt()
    with pytest.raises(ReceiptLost):
        await world.commit(priority(1, 3))
    world.load(save)
    assert await world.commit(priority(1, 3)) == {"status": "stale_epoch", "load_epoch": world.load_epoch}
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3


# 5.5 rule 6: a load invalidates changesets, receipts and undo tokens from the old epoch.
@pytest.mark.asyncio
async def test_changeset_from_an_old_epoch_is_rejected_after_a_load():
    world = world_with()
    old_epoch = world.load_epoch
    save = world.save()
    world.load(save)
    receipt = await world.commit(priority(1, 3, epoch=old_epoch))
    assert receipt == {"status": "stale_epoch", "load_epoch": world.load_epoch}
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3


@pytest.mark.asyncio
async def test_undo_token_from_an_old_epoch_is_void_after_a_load():
    world = world_with()
    token = (await world.commit(priority(1, 3)))["undo_token"]
    world.load(world.save())
    assert await world.undo(token) == {"status": "stale_epoch", "load_epoch": world.load_epoch}


# Injected fault 1: the player changes the same field after the planner read it, one frame before the write.
@pytest.mark.asyncio
async def test_player_edit_before_apply_becomes_a_conflict_and_writes_nothing():
    world = world_with()
    world.schedule("before_apply", lambda w: w.player_edit(PAWN, "set_work_priority/work=Doctor", 2))
    receipt = await world.commit(priority(1, 3))
    assert receipt["status"] == "conflict" and receipt["observed"] == 2 and receipt["last_writer"] == "user"
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 2
    assert world.ledger(PAWN, "set_work_priority/work=Doctor") is None


# Injected fault 2: the write is applied but the reply is lost; the same key retry must not apply it twice.
@pytest.mark.asyncio
async def test_lost_receipt_retry_with_the_same_key_returns_the_original_receipt():
    world = world_with()
    await world.push_policy(EMPTY_POLICY)
    world.lose_next_receipt()
    changeset = bill(True, False, key="cs-lost")
    with pytest.raises(ReceiptLost) as lost:
        await world.commit(changeset)
    assert lost.value.idempotency_key == "cs-lost"
    assert world.value(BILL, "set_bill/suspended") is True
    retry = await world.commit(changeset)
    assert retry["status"] == "committed" and retry["undo_token"] == "undo-1"
    assert [e["data"]["receipt"]["changeset_id"] for e in await world.events() if e["kind"] == "write"] == ["cs-lost"]


# Injected fault 3: a load happens while the commit is in progress; the commit must not write into the new epoch.
@pytest.mark.asyncio
async def test_load_during_commit_rejects_the_changeset_and_restores_nothing_new():
    world = world_with()
    save = world.save()
    world.schedule("before_apply", lambda w: w.load(save))
    receipt = await world.commit(priority(1, 3))
    assert receipt == {"status": "stale_epoch", "load_epoch": world.load_epoch}
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3
    assert world.ledger(PAWN, "set_work_priority/work=Doctor") is None


@pytest.mark.asyncio
async def test_changeset_without_its_full_key_set_is_rejected():
    world = world_with()
    changeset = priority(1, 3)
    changeset.pop("author")
    with pytest.raises(ToolExecutionError) as caught:
        await world.commit(changeset)
    assert caught.value.code == "invalid_changeset"


@pytest.mark.asyncio
async def test_changeset_that_writes_one_field_twice_is_rejected():
    world = world_with()
    changeset = priority(1, 3)
    changeset["ops"].append(dict(changeset["ops"][0]))
    with pytest.raises(ToolExecutionError) as caught:
        await world.commit(changeset)
    assert caught.value.code == "invalid_changeset"


# set_paused: outside the changeset, the ledger and the conflict check.
@pytest.mark.asyncio
async def test_set_paused_is_not_a_changeset_and_leaves_no_ledger_entry():
    world = world_with()
    await world.push_policy(EMPTY_POLICY)
    before = world.save()
    result = await world.set_paused(True, role="planner")
    assert result == {"paused": True, "role": "planner"} and world.paused
    assert world.value(PAWN, "set_work_priority/work=Doctor") == 3
    assert world.save()["ledger"] == {} and set(world.save()["origin"]) == set(before["origin"])
    events = await world.events()
    assert events[-1]["kind"] == "pause_changed"
    assert events[-1]["data"] == {"cursor": 3, "kind": "pause_changed", "paused": True,
                                  "speed_paused": True, "force_paused": False,
                                  "windows_force_paused": False, "source": "planner", "pause_state": world.pause_state()}


@pytest.mark.asyncio
async def test_events_since_a_cursor_returns_only_newer_events():
    world = world_with()
    await world.push_policy(EMPTY_POLICY)
    events = await world.events()
    assert [e["cursor"] for e in events] == [1, 2]
    await world.set_paused(True, role="planner")
    newer = await world.events(since=events[-1]["cursor"])
    assert [e["cursor"] for e in newer] == [3] and newer[0]["kind"] == "pause_changed"
    assert len(await world.events()) == 3


@pytest.mark.asyncio
async def test_object_keys_are_configurable_for_other_operations():
    world = ScriptedGameWorld(object_keys=("thing",))
    world.player_edit("Thing_Steel1", "set_forbidden", False)
    changeset = priority(True, False)
    changeset["ops"] = [{"op": "set_forbidden", "thing": "Thing_Steel1", "expect": False, "value": True, "evidence": "e"}]
    assert (await world.commit(changeset))["status"] == "committed"
    with pytest.raises(ToolExecutionError) as caught:
        await world.commit(priority(1, 3, key="cs-2"))
    assert caught.value.code == "invalid_changeset"


@pytest.mark.asyncio
async def test_set_paused_rejects_non_boolean_values():
    with pytest.raises(ToolExecutionError) as caught:
        await ScriptedGameWorld().set_paused("yes", role="planner")
    assert caught.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_push_policy_is_accepted_as_given():
    policy = {"pause_on_letter_defs": ["ThreatBig"], "source": "frontdesk"}
    assert await ScriptedGameWorld().push_policy(policy) == policy


@pytest.mark.asyncio
async def test_event_subscription_scope_and_reload():
    world = world_with()
    world.advance(20)
    with pytest.raises(ToolExecutionError) as error:
        await world.events()
    assert error.value.code == "events_not_subscribed"
    await world.push_policy(EMPTY_POLICY)
    assert not world.paused
    first = await world.events()
    assert [event["kind"] for event in first] == ["session_snapshot", "policy_changed"]
    assert first[0]["data"] == {"cursor": 1, "kind": "session_snapshot", "origin": "new_game", "start_tick": 0,
                                 "pause_state": world.pause_state()}
    assert first[0]["game_tick"] == 20
    old_epoch = first[0]["load_epoch"]
    saved = world.save()
    world.load(saved)
    with pytest.raises(ToolExecutionError) as error:
        await world.events()
    assert error.value.code == "events_not_subscribed"
    world.advance(5)
    await world.push_policy(EMPTY_POLICY)
    second = await world.events()
    assert [event["cursor"] for event in second] == [1, 2]
    assert all(event["load_epoch"] == world.load_epoch != old_epoch for event in second)
    assert second[0]["data"] == {"cursor": 1, "kind": "session_snapshot", "origin": "loaded_game", "start_tick": 20,
                                  "pause_state": world.pause_state()}
    assert second[0]["game_tick"] == 25
