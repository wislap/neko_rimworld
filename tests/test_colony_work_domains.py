"""Business contracts replay through the actual append-only EventLedger."""

import pytest

from plugin.plugins.neko_rimworld.workspace import construction, observations, policy_scope, resource_allocation
from plugin.plugins.neko_rimworld.workspace.ledger import EventLedger

EPOCH = "0" * 32
BINDING = {"colony_id": "colony", "load_epoch": EPOCH, "connection_epoch": "connection"}


@pytest.fixture
def ledger(tmp_path):
    value = EventLedger(tmp_path / "work.sqlite")
    yield value
    value.close()


def append(ledger, spec, *, epoch=EPOCH, tick=100):
    with ledger.transaction():
        return ledger.append("colony", epoch, tick, spec["source"], spec["kind"], spec["payload"])


def events(ledger):
    return ledger.events("colony")


def plan(*identities):
    return {"format": "construction_plan.v1", "compiled": True, "map_id": "Map_1", "origin": {"x": 10, "z": 10},
            "elements": [{"id": identity, "definition": "Wall", "position": {"x": index, "z": 0}, "rotation": 0}
                         for index, identity in enumerate(identities or ("wall",))]}


def adopt(ledger, drawing=None):
    append(ledger, construction.prepare_adoption(events(ledger), plan_id="house", task_id="task", plan=drawing or plan(),
                                                 source={"report_id": "suggestion"}, load_epoch=EPOCH))


def fact(ledger, stage, identity, **extra):
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="wall", stage=stage,
                                             object_id=identity, observation_id=f"read-{stage}", load_epoch=EPOCH, **extra))


def current_plan(ledger, epoch=EPOCH):
    return construction.project(events(ledger), load_epoch=epoch)["plans"][0]


def purpose(ledger, **extra):
    args = dict(plan_id="house", task_id="task", expected_revision=1, load_epoch=EPOCH,
                goal="Two colonists can use this room for sleeping", judgment="supported",
                findings=["Current native room and bed observations support the stated sleeping purpose."],
                evidence_ids=["native-room-read", "native-bed-read"])
    append(ledger, construction.prepare_purpose_assessment(events(ledger), **{**args, **extra}))


def stock(wood=80, **extra):
    return {"map_id": "Map_1", "observation_id": "stock-1", "load_epoch": EPOCH, "game_tick": 100,
            "quantities": {"WoodLog": wood}, "complete": True, "scope": {"selected_sources": ["map_stock"]}, **extra}


def demands():
    return [{"id": "house", "revision": 1, "window": "now", "resources": {"WoodLog": 60}},
            {"id": "kitchen", "revision": 1, "window": "now", "resources": {"WoodLog": 50}}]


def condition(identity="stage", **extra):
    return {"id": identity, "kind": "construction", "target_ids": ["Blueprint_1"], "max_samples": 100,
            "expires_at_tick": 1000, "plan_id": "house", **extra}


def request(ledger, *, expected=0, conditions=None, binding=BINDING):
    append(ledger, observations.prepare_request(events(ledger), observation_id="watch", task_id="task", expected_revision=expected,
                                                conditions=conditions or [condition()], supported_kinds={"construction"}, binding=binding, now_tick=100))


def acknowledge(ledger, *, covered=("stage",), binding=BINDING, revision=1, status="registered", receipt="receipt"):
    append(ledger, observations.prepare_coverage(events(ledger), observation_id="watch", revision=revision, binding=binding,
                                                 status=status, covered_condition_ids=covered, receipt_id=receipt))


def test_suggestion_is_not_an_adopted_task_and_adoption_is_unique(ledger):
    append(ledger, {"kind": "agent.report", "source": "planner", "payload": {"plan": plan()}})
    assert construction.project(events(ledger), load_epoch=EPOCH) == {"plans": []}
    adopt(ledger)
    row = current_plan(ledger)
    assert row["source"] == {"report_id": "suggestion"}
    assert row["phase"] == "reconcile"
    assert row["elements"][0]["continuation"] == "reconcile"
    with pytest.raises(ValueError, match="already adopted"):
        adopt(ledger)


def test_evidenced_planner_purpose_judgment_is_separate_from_construction_fulfillment(ledger):
    adopt(ledger)
    assert current_plan(ledger)["purpose_status"] == "not_evaluated"
    assert current_plan(ledger)["purpose_assessment"] is None
    purpose(ledger)
    row = current_plan(ledger)
    assert row["purpose_status"] == "supported"
    assert not row["construction_complete"]
    assessment = row["purpose_assessment"]
    assert assessment["source"] == "planner_assessment"
    assert assessment["evidence_ids"] == ["native-room-read", "native-bed-read"]
    assert assessment["freshness"] == "current"
    assert assessment["revision"] == 1 and assessment["load_epoch"] == EPOCH
    fact(ledger, "built", "Wall_1")
    purpose(ledger, judgment="unmet", findings=["A completed wall does not supply the required two beds."])
    assert current_plan(ledger)["construction_complete"]
    assert current_plan(ledger)["purpose_status"] == "unmet"


def test_purpose_judgment_stales_after_real_fact_change_and_does_not_revive_when_state_returns(ledger):
    adopt(ledger)
    fact(ledger, "built", "Wall_1")
    purpose(ledger)
    assessed_version = current_plan(ledger)["state_version"]
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="wall", stage="built",
                                             object_id="Wall_1", observation_id="new-identical-read", load_epoch=EPOCH))
    assert current_plan(ledger)["purpose_status"] == "supported"
    assert current_plan(ledger)["state_version"] == assessed_version
    fact(ledger, "missing", None)
    assert current_plan(ledger)["purpose_status"] == "stale"
    assert "related_facts_changed" in current_plan(ledger)["purpose_assessment"]["stale_reasons"]
    fact(ledger, "built", "Wall_1")
    assert current_plan(ledger)["construction_complete"]
    assert current_plan(ledger)["purpose_status"] == "stale"
    purpose(ledger, judgment="unknown", findings=["Current construction is present, but the room purpose needs another observation."])
    assert current_plan(ledger)["purpose_status"] == "unknown"


def test_purpose_assessment_keeps_old_revision_and_epoch_visible_as_stale(ledger):
    adopt(ledger)
    fact(ledger, "built", "Wall_1")
    purpose(ledger)
    restored = current_plan(ledger, "1" * 32)
    assert restored["purpose_status"] == "stale"
    assert restored["purpose_assessment"]["stale_reasons"] == ["load_epoch_changed"]
    assert restored["purpose_assessment"]["judgment"] == "supported"
    append(ledger, construction.prepare_revision(events(ledger), plan_id="house", expected_revision=1, plan=plan(), load_epoch=EPOCH))
    assert current_plan(ledger)["purpose_status"] == "stale"
    assert "revision_changed" in current_plan(ledger)["purpose_assessment"]["stale_reasons"]
    with pytest.raises(ValueError, match="current adopted task and plan revision"):
        purpose(ledger)
    purpose(ledger, expected_revision=2)
    assert current_plan(ledger)["purpose_status"] == "supported"


def test_purpose_constructor_rejects_older_epoch_and_wrong_adopted_task(ledger):
    adopt(ledger)
    with pytest.raises(ValueError, match="current adopted task"):
        purpose(ledger, task_id="other-task")
    append(ledger, {"kind": "colony.load", "source": "mod", "payload": {}}, epoch="1" * 32)
    with pytest.raises(ValueError, match="older world epoch"):
        purpose(ledger)


@pytest.mark.parametrize("changes", [{"goal": ""}, {"judgment": "certified"}, {"findings": []}, {"findings": [""]}, {"evidence_ids": []}, {"evidence_ids": ["same", "same"]}])
def test_purpose_assessment_requires_explicit_judgment_findings_and_native_references(ledger, changes):
    adopt(ledger)
    with pytest.raises(ValueError):
        purpose(ledger, **changes)


def test_native_lineage_and_current_epoch_fulfillment(ledger):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    assert current_plan(ledger)["placement_complete"]
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "frame", "Frame_2")
    fact(ledger, "frame", "Frame_2", previous_object_id="Blueprint_1")
    fact(ledger, "built", "Wall_3", previous_object_id="Frame_2")
    assert current_plan(ledger)["construction_complete"]
    restored = current_plan(ledger, "1" * 32)
    assert not restored["construction_complete"]
    assert restored["elements"][0]["stage"] == "built"
    assert restored["elements"][0]["continuation"] == "reconcile"
    assert restored["purpose_status"] == "not_evaluated"


def test_failed_frame_hands_the_element_to_its_native_replacement_blueprint(ledger):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    fact(ledger, "frame", "Frame_2", previous_object_id="Blueprint_1")
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "blueprint", "Blueprint_9")
    fact(ledger, "blueprint", "Blueprint_3", previous_object_id="Frame_2")
    element = current_plan(ledger)["elements"][0]
    assert element["stage"] == "blueprint" and element["binding"]["object_id"] == "Blueprint_3"
    assert not current_plan(ledger)["construction_complete"]
    fact(ledger, "frame", "Frame_4", previous_object_id="Blueprint_3")
    fact(ledger, "built", "Wall_5", previous_object_id="Frame_4")
    assert current_plan(ledger)["construction_complete"]
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "blueprint", "Blueprint_6", previous_object_id="Wall_5")


def test_unknown_absence_does_not_erase_lifecycle_binding_or_authorize_replacement(ledger):
    adopt(ledger)
    fact(ledger, "built", "Wall_1")
    fact(ledger, "missing", None)
    assert current_plan(ledger)["elements"][0]["continuation"] == "reconcile"
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "blueprint", "Blueprint_2")
    fact(ledger, "built", "Wall_1")
    assert current_plan(ledger)["construction_complete"]


def test_explicit_player_cancel_survives_revision_and_requires_real_user_return(ledger):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    fact(ledger, "user_cancelled", "Blueprint_1")
    revised = plan()
    revised["elements"][0]["position"]["x"] = 5
    append(ledger, construction.prepare_revision(events(ledger), plan_id="house", expected_revision=1, plan=revised, load_epoch=EPOCH))
    assert current_plan(ledger)["elements"][0]["intent"] == "cancelled"
    assert current_plan(ledger)["elements"][0]["stop"]["object_id"] == "Blueprint_1"
    assert current_plan(ledger)["elements"][0].get("binding") is None
    with pytest.raises(ValueError, match="user return"):
        construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="continue")
    seq = append(ledger, {"kind": "user.return", "source": "user", "payload": {"takeover_seq": 1}})
    with pytest.raises(ValueError, match="user return"):
        construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="continue", source_seq=seq)
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="user returned it", source_seq=seq, validated_release=True))
    assert current_plan(ledger)["elements"][0]["intent"] == "continue"


def test_model_cancellation_can_be_explicitly_reconsidered_but_cannot_erase_user_stop(ledger):
    adopt(ledger)
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="cancelled", reason="choose another layout"))
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="explicit revised decision"))
    assert current_plan(ledger)["elements"][0]["intent"] == "continue"
    fact(ledger, "blueprint", "Blueprint_1")
    fact(ledger, "user_cancelled", "Blueprint_1")
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="cancelled", reason="acknowledge"))
    with pytest.raises(ValueError, match="user return"):
        construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="generic continue")


def test_revision_retirement_and_changed_geometry_invalidate_evidence(ledger):
    adopt(ledger, plan("wall", "retired"))
    fact(ledger, "built", "Wall_1")
    append(ledger, construction.prepare_revision(events(ledger), plan_id="house", expected_revision=1, plan=plan(), load_epoch=EPOCH))
    assert current_plan(ledger)["counts"]["required"] == 1
    assert current_plan(ledger)["construction_complete"]
    changed = plan()
    changed["elements"][0]["definition"] = "Door"
    append(ledger, construction.prepare_revision(events(ledger), plan_id="house", expected_revision=2, plan=changed, load_epoch=EPOCH))
    assert not current_plan(ledger)["construction_complete"]
    assert current_plan(ledger)["elements"][0]["fact"] is None
    with pytest.raises(ValueError, match="revision conflict"):
        construction.prepare_revision(events(ledger), plan_id="house", expected_revision=2, plan=changed, load_epoch=EPOCH)


def test_plan_origin_move_invalidates_world_evidence_and_rename_cannot_erase_stop(ledger):
    adopt(ledger)
    fact(ledger, "built", "Wall_1")
    moved = plan()
    moved["origin"]["x"] += 1
    append(ledger, construction.prepare_revision(events(ledger), plan_id="house", expected_revision=1, plan=moved, load_epoch=EPOCH))
    assert current_plan(ledger)["elements"][0]["fact"] is None
    assert not current_plan(ledger)["construction_complete"]
    fact(ledger, "blueprint", "Blueprint_2")
    fact(ledger, "user_cancelled", "Blueprint_2")
    renamed = {**moved, "elements": [{**moved["elements"][0], "id": "new-wall"}]}
    with pytest.raises(ValueError, match="stable element id"):
        construction.prepare_revision(events(ledger), plan_id="house", expected_revision=2, plan=renamed, load_epoch=EPOCH)


def test_cancelled_explicit_target_cannot_be_reissued_via_another_plan_or_retirement(ledger):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    native_field = {"object": "Map_1", "field": "construction:opaque-native-hash"}
    fact(ledger, "user_cancelled", "Blueprint_1", field_identity=native_field)
    append(ledger, construction.prepare_adoption(events(ledger), plan_id="second", task_id="task", plan=plan("new-wall"), source={}, load_epoch=EPOCH))
    dispatch = dict(plan_id="second", element_ids=["new-wall"], load_epoch=EPOCH)
    with pytest.raises(ValueError, match="native user action"):
        construction.assert_dispatch_authority(events(ledger), **dispatch)
    replacement = plan("other")
    replacement["origin"]["x"] += 5
    append(ledger, construction.prepare_revision(events(ledger), plan_id="house", expected_revision=1, plan=replacement, load_epoch=EPOCH))
    protected = current_plan(ledger)["protected_targets"][0]
    assert protected["element_id"] == "wall"
    assert protected["target"]["position"]["x"] == 10
    assert protected["scope"] == native_field
    with pytest.raises(ValueError, match="native user action"):
        construction.assert_dispatch_authority(events(ledger), **dispatch)
    # A user's explicit cancellation of this wall is not a universal empty-cell ban.
    different = plan("door")
    different["elements"][0]["definition"] = "Door"
    append(ledger, construction.prepare_adoption(events(ledger), plan_id="different", task_id="task", plan=different, source={}, load_epoch=EPOCH))
    construction.assert_dispatch_authority(events(ledger), plan_id="different", element_ids=["door"], load_epoch=EPOCH)
    takeover = append(ledger, {"kind": "user.takeover", "source": "frontdesk", "payload": {**native_field, "value_read": None}})
    append(ledger, {"kind": "user.return", "source": "frontdesk", "payload": {"takeover_seq": takeover}})
    result = construction.assert_dispatch_authority(events(ledger), **dispatch)
    assert result["checked_protections"] == 0
    assert current_plan(ledger)["protected_targets"] == []


def test_compiled_plan_hash_is_order_independent_and_detached(ledger):
    drawing = plan()
    first = construction.prepare_adoption([], plan_id="a", task_id="task", plan=drawing, source={}, load_epoch=EPOCH)
    second = construction.prepare_adoption([], plan_id="b", task_id="task", plan=dict(reversed(list(drawing.items()))), source={}, load_epoch=EPOCH)
    assert first["payload"]["plan_hash"] == second["payload"]["plan_hash"]
    drawing["elements"][0]["definition"] = "Door"
    assert first["payload"]["plan"]["elements"][0]["definition"] == "Wall"
    drawing["compiled"] = False
    with pytest.raises(ValueError, match="compiled"):
        adopt(ledger, drawing)


def test_area_designations_and_completed_terrain_use_cells_without_fake_thing_ids(ledger):
    drawing = plan()
    drawing["elements"][0].update(definition="WoodPlankFloor", layer="floor")
    drawing["areas"] = [{"id": "roof", "kind": "roof", "cells": [{"x": 0, "z": 0}]}]
    adopt(ledger, drawing)
    fact(ledger, "blueprint", "Blueprint_1")
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "built", None)
    fact(ledger, "built", None, previous_object_id="Blueprint_1")
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="roof", stage="designated",
                                             object_id=None, observation_id="roof-designations", load_epoch=EPOCH))
    assert current_plan(ledger)["placement_complete"]
    assert not current_plan(ledger)["construction_complete"]
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="roof", stage="built",
                                             object_id=None, observation_id="roof-complete-cells", load_epoch=EPOCH))
    assert current_plan(ledger)["construction_complete"]
    assert all(row["fact"]["evidence_kind"] == "cells" and row["object_id"] is None for row in current_plan(ledger)["elements"])


def test_area_field_identity_uses_actual_native_area_object_not_the_map(ledger):
    drawing = plan()
    drawing["areas"] = [{"id": "stockpile", "kind": "stockpile", "cells": [{"x": 0, "z": 0}]}]
    adopt(ledger, drawing)
    identity = {"object": "Zone_Stockpile_7", "field": "zone_cells"}
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="stockpile", stage="built",
                                             object_id=None, observation_id="actual-zone-cells", load_epoch=EPOCH, field_identity=identity))
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "stockpile")
    assert row["fulfilled"]
    assert row["fact"]["field_identity"] == identity
    with pytest.raises(ValueError, match="plan map"):
        construction.prepare_fact(events(ledger), plan_id="house", element_id="wall", stage="built", object_id="Wall_1",
                                  observation_id="wrong-native-object", load_epoch=EPOCH, field_identity=identity)


def test_area_cancel_requires_all_real_native_field_scopes_to_be_returned(ledger):
    drawing = plan()
    drawing["areas"] = [{"id": "roof", "kind": "roof", "cells": [{"x": 0, "z": 0}, {"x": 1, "z": 0}]}]
    adopt(ledger, drawing)
    identities = [{"object": "Area_BuildRoof_7", "field": "area_flag:10:10"},
                  {"object": "Area_BuildRoof_7", "field": "area_flag:11:10"}]
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="roof", stage="user_cancelled",
                                             object_id=None, observation_id="native-cell-cancellation", load_epoch=EPOCH, field_identities=identities))
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "roof")
    assert row["stop"]["scopes"] == identities
    assert row["stop"]["scope"] is None
    first_takeover = append(ledger, {"kind": "user.takeover", "source": "frontdesk", "payload": identities[0]})
    first_return = append(ledger, {"kind": "user.return", "source": "frontdesk", "payload": {"takeover_seq": first_takeover}})
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "roof")
    assert len(row["stop"]["scope_returns"]) == 1
    assert row["stop"].get("released_by") is None
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="roof", stage="user_cancelled",
                                             object_id=None, observation_id="repeat-native-cancellation", load_epoch=EPOCH, field_identities=list(reversed(identities))))
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "roof")
    assert len(row["stop"]["scope_returns"]) == 1
    with pytest.raises(ValueError, match="Every native area scope"):
        construction.prepare_intent(events(ledger), plan_id="house", element_ids=["roof"], intent="continue", reason="one cell returned",
                                    source_seq=first_return, validated_release=True)
    second_takeover = append(ledger, {"kind": "user.takeover", "source": "frontdesk", "payload": identities[1]})
    second_return = append(ledger, {"kind": "user.return", "source": "frontdesk", "payload": {"takeover_seq": second_takeover}})
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "roof")
    assert row["stop"]["released_by"] == second_return
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["roof"], intent="continue", reason="all native scopes returned"))
    append(ledger, construction.prepare_fact(events(ledger), plan_id="house", element_id="roof", stage="user_cancelled",
                                             object_id=None, observation_id="same-cancel-after-return", load_epoch=EPOCH, field_identities=identities))
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "roof")
    assert row["intent"] == "continue"
    assert row["stop"]["released_by"] == second_return
    assert current_plan(ledger)["protected_targets"] == []
    construction.assert_dispatch_authority(events(ledger), plan_id="house", element_ids=["roof"], load_epoch=EPOCH)


def test_native_area_identity_list_is_exact_unique_and_area_only(ledger):
    drawing = plan()
    drawing["areas"] = [{"id": "home", "kind": "home", "cells": [{"x": 0, "z": 0}]}]
    adopt(ledger, drawing)
    identity = {"object": "Area_Home_7", "field": "area_flag:10:10"}
    base = dict(plan_id="house", element_id="home", stage="built", object_id=None, observation_id="actual-cells", load_epoch=EPOCH)
    for extra in ({"field_identities": []}, {"field_identities": [identity, identity]},
                  {"field_identities": [identity], "field_identity": identity}, {"field_identities": [{"object": "Area_Home_7"}]}):
        with pytest.raises(ValueError):
            construction.prepare_fact(events(ledger), **base, **extra)
    with pytest.raises(ValueError, match="area-only"):
        construction.prepare_fact(events(ledger), **{**base, "element_id": "wall", "object_id": "Wall_1"}, field_identities=[identity])


def test_new_real_manual_area_action_does_not_inherit_previous_return(ledger):
    drawing = plan()
    drawing["areas"] = [{"id": "home", "kind": "home", "cells": [{"x": 0, "z": 0}]}]
    adopt(ledger, drawing)
    identity = {"object": "Area_Home_7", "field": "area_flag:10:10"}
    args = dict(plan_id="house", element_id="home", stage="user_cancelled", object_id=None,
                observation_id="native-manual-cells", load_epoch=EPOCH, field_identities=[identity])
    append(ledger, construction.prepare_fact(events(ledger), **args, native_action_id="manual-1"))
    takeover = append(ledger, {"kind": "user.takeover", "source": "frontdesk", "payload": identity})
    returned = append(ledger, {"kind": "user.return", "source": "frontdesk", "payload": {"takeover_seq": takeover}})
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["home"], intent="continue", reason="user returned exact cell"))
    append(ledger, construction.prepare_fact(events(ledger), **args, native_action_id="manual-1"))
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "home")
    assert row["intent"] == "continue"
    assert row["stop"]["released_by"] == returned
    append(ledger, construction.prepare_fact(events(ledger), **args, native_action_id="manual-2"))
    row = next(row for row in current_plan(ledger)["elements"] if row["id"] == "home")
    assert row["intent"] == "cancelled"
    assert row["stop"]["native_action_id"] == "manual-2"
    assert not row["stop"].get("released_by")
    assert row["stop"]["scope_returns"] == []


@pytest.mark.parametrize("element_id,stage,object_id", [("wall", "user_cancelled", None), ("home", "built", None)])
def test_manual_action_identity_cannot_be_attached_to_unrelated_fact_types(ledger, element_id, stage, object_id):
    drawing = plan()
    drawing["areas"] = [{"id": "home", "kind": "home", "cells": [{"x": 0, "z": 0}]}]
    adopt(ledger, drawing)
    with pytest.raises(ValueError, match="manual action ids"):
        construction.prepare_fact(events(ledger), plan_id="house", element_id=element_id, stage=stage, object_id=object_id,
                                  observation_id="actual-cells", load_epoch=EPOCH, native_action_id="manual-1")


def test_construction_native_progress_cannot_regress_even_with_same_object_id(ledger):
    adopt(ledger)
    fact(ledger, "built", "Wall_1")
    with pytest.raises(ValueError, match="backwards"):
        fact(ledger, "frame", "Wall_1")


def test_confirmed_model_cancellation_and_explicit_restart_start_a_new_generation(ledger):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    fact(ledger, "model_cancelled", None)
    row = current_plan(ledger)["elements"][0]
    assert row["intent"] == "cancelled"
    assert row["stop"]["source"] == "planner"
    assert row["binding"]["object_id"] == "Blueprint_1"
    with pytest.raises(ValueError, match="explicit continued intent"):
        construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                     previous_object_id="Blueprint_1", object_id="Blueprint_2", observation_id="native-restart-receipt")
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="restart explicitly"))
    fact(ledger, "model_cancelled", None)
    assert current_plan(ledger)["elements"][0]["intent"] == "continue"
    assert current_plan(ledger)["elements"][0]["continuation"] == "reconcile"
    # A read of a replacement blueprint cannot substitute for a restart receipt.
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "blueprint", "Blueprint_2")
    append(ledger, construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                                 previous_object_id="Blueprint_1", object_id="Blueprint_2", observation_id="native-restart-receipt"))
    row = current_plan(ledger)["elements"][0]
    assert row["generation"] == 2
    assert row["binding"] == {"object_id": "Blueprint_2", "stage": "blueprint"}
    assert row["fact"]["origin"] == "native_restart_receipt"
    with pytest.raises(ValueError, match="lineage"):
        fact(ledger, "frame", "Frame_3", previous_object_id="Blueprint_1")
    fact(ledger, "frame", "Frame_3", previous_object_id="Blueprint_2")
    fact(ledger, "built", "Wall_4", previous_object_id="Frame_3")
    assert current_plan(ledger)["construction_complete"]


def test_player_cancelled_generation_requires_actual_scope_return_before_restart(ledger):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    native_field = {"object": "Map_1", "field": "construction:opaque-native-hash"}
    fact(ledger, "user_cancelled", None, field_identity=native_field)
    with pytest.raises(ValueError):
        construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                     previous_object_id="Blueprint_1", object_id="Blueprint_2", observation_id="native-restart-receipt")
    takeover = append(ledger, {"kind": "user.takeover", "source": "frontdesk", "payload": native_field})
    append(ledger, {"kind": "user.return", "source": "frontdesk", "payload": {"takeover_seq": takeover}})
    append(ledger, construction.prepare_intent(events(ledger), plan_id="house", element_ids=["wall"], intent="continue", reason="user returned this target"))
    assert current_plan(ledger)["elements"][0]["stop"]["released_by"]
    assert current_plan(ledger)["elements"][0]["continuation"] == "reconcile"
    append(ledger, construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                                 previous_object_id="Blueprint_1", object_id="Blueprint_2", observation_id="native-restart-receipt", field_identity=native_field))
    assert current_plan(ledger)["elements"][0]["generation"] == 2


@pytest.mark.parametrize("stage", ["missing", "built"])
def test_restart_does_not_convert_unknown_or_existing_objects_into_new_generations(ledger, stage):
    adopt(ledger)
    fact(ledger, "blueprint", "Blueprint_1")
    if stage == "missing":
        fact(ledger, stage, None)
    else:
        fact(ledger, stage, "Wall_1", previous_object_id="Blueprint_1")
    with pytest.raises(ValueError, match="known cancelled/destroyed"):
        construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                     previous_object_id="Blueprint_1", object_id="Blueprint_2", observation_id="not-a-native-restart")


def test_destroyed_generation_restart_requires_exact_previous_identity(ledger):
    adopt(ledger)
    fact(ledger, "built", "Wall_1")
    fact(ledger, "destroyed", None)
    for previous, replacement in (("wrong", "Blueprint_2"), ("Wall_1", "Wall_1")):
        with pytest.raises(ValueError, match="exact known previous object"):
            construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                         previous_object_id=previous, object_id=replacement, observation_id="native-restart-receipt")
    append(ledger, construction.prepare_restart(events(ledger), plan_id="house", element_id="wall", load_epoch=EPOCH,
                                                 previous_object_id="Wall_1", object_id="Blueprint_2", observation_id="native-restart-receipt"))
    assert current_plan(ledger)["elements"][0]["generation"] == 2


def test_resource_readings_expose_independent_success_and_joint_shortfall():
    report = resource_allocation.assess(stock(), demands())
    assert all(row["satisfied"] for row in report["independent"])
    assert report["joint"][0]["shortfalls"] == {"WoodLog": 30}
    assert report["selected"] is None
    assert report["competition"] == [{"window": "now", "resource": "WoodLog", "demand_ids": ["house", "kitchen"], "shortfall": 30, "status": "unallocated"}]


def test_selected_allocation_remains_visible_when_stock_and_demand_change(ledger):
    append(ledger, resource_allocation.prepare_selection(events(ledger), allocation_id="wood", expected_revision=0, snapshot=stock(),
                                                         demands=demands(), shares={"house": {"WoodLog": 60}, "kitchen": {"WoodLog": 20}}, window="now", reason="house first", task_id="task"))
    choice = resource_allocation.project(events(ledger))["decisions"][0]
    assert choice["assessment"]["selected"]["demands"][1]["remaining"] == {"WoodLog": 30}
    changed = demands()
    changed[0]["revision"] = 2
    changed[0]["resources"]["WoodLog"] = 70
    report = resource_allocation.assess(stock(50, observation_id="stock-2"), changed, selection=choice)
    assert report["selected"]["shares"]["house"] == {"WoodLog": 60}
    assert report["selected"]["current_stock"]["shortfalls"] == {"WoodLog": 30}
    assert report["selected"]["demands"][0]["allocation_covers_demand"] is False
    assert report["selected"]["demands"][0]["satisfied"] is None
    assert report["selected"]["stale"]
    assert {row["kind"] for row in report["discrepancies"]} == {"demand_changed", "stock_changed"}


def test_resource_scope_changes_invalidate_choice_without_rewriting_it(ledger):
    chosen = resource_allocation.prepare_selection([], allocation_id="wood", expected_revision=0, snapshot=stock(), demands=demands(),
                                                   shares={"house": {"WoodLog": 60}}, window="now", reason="choose")
    report = resource_allocation.assess(stock(scope={"selected_sources": ["carried"]}), demands(), selection=chosen["payload"])
    assert report["discrepancies"] == [{"kind": "resource_scope_changed", "selected": {"selected_sources": ["map_stock"]}, "current": {"selected_sources": ["carried"]}}]
    assert report["selected"]["demands"][0]["satisfied"] is None
    assert report["selected"]["shares"] == {"house": {"WoodLog": 60}}
    with pytest.raises(ValueError, match="explicit resource scope"):
        resource_allocation.assess(stock(scope={}), demands())


def test_resource_projection_requires_current_epoch_evidence_and_retains_original_choice(ledger):
    append(ledger, resource_allocation.prepare_selection([], allocation_id="wood", expected_revision=0, snapshot=stock(), demands=demands(),
                                                         shares={"house": {"WoodLog": 60}}, window="now", reason="choose"))
    new_epoch = "1" * 32
    historical = resource_allocation.project(events(ledger), load_epoch=new_epoch)["decisions"][0]
    assert historical["evidence_status"] == "historical"
    assert historical["assessment"]["independent"][0]["historical_satisfied"]
    assert historical["assessment"]["independent"][0]["satisfied"] is None
    assert historical["assessment"]["selected"]["stale"]
    assert historical["snapshot"]["quantities"]["WoodLog"] == 80
    append(ledger, resource_allocation.prepare_observation(events(ledger), allocation_id="wood", expected_revision=1,
                                                           snapshot=stock(50, observation_id="new-stock", load_epoch=new_epoch)), epoch=new_epoch)
    current = resource_allocation.project(events(ledger), load_epoch=new_epoch)["decisions"][0]
    assert current["evidence_status"] == "current_epoch"
    assert current["snapshot"]["quantities"]["WoodLog"] == 80
    assert current["current_snapshot"]["quantities"]["WoodLog"] == 50
    assert current["shares"] == {"house": {"WoodLog": 60}}
    assert current["assessment"]["selected"]["current_stock"]["shortfalls"] == {"WoodLog": 10}
    assert current["assessment"]["selected"]["stale"]
    assert len(current["history"]) == 2


def test_resource_observation_is_bound_to_allocation_revision_and_explicit_needs(ledger):
    args = dict(allocation_id="wood", snapshot=stock(), demands=demands(), shares={"house": {"WoodLog": 60}}, window="now", reason="choose")
    append(ledger, resource_allocation.prepare_selection([], expected_revision=0, **args))
    append(ledger, resource_allocation.prepare_selection(events(ledger), expected_revision=1, **args))
    with pytest.raises(ValueError, match="revision conflict"):
        resource_allocation.prepare_observation(events(ledger), allocation_id="wood", expected_revision=1, snapshot=stock(50))
    changed_demands = demands()
    changed_demands[0].update(revision=2, resources={"WoodLog": 70})
    append(ledger, resource_allocation.prepare_observation(events(ledger), allocation_id="wood", expected_revision=2,
                                                           snapshot=stock(), demands=changed_demands))
    row = resource_allocation.project(events(ledger))["decisions"][0]
    assert row["demands"][0]["revision"] == 1
    assert row["current_demands"][0]["revision"] == 2
    assert row["assessment"]["selected"]["stale"]


def test_allocations_cannot_spend_unknown_or_duplicate_stock(ledger):
    args = dict(allocation_id="wood", expected_revision=0, demands=demands(), window="now", reason="choose")
    with pytest.raises(ValueError, match="known available"):
        resource_allocation.prepare_selection([], snapshot=stock(), shares={"house": {"WoodLog": 60}, "kitchen": {"WoodLog": 50}}, **args)
    unknown = stock(quantities={}, complete=False)
    assert resource_allocation.assess(unknown, demands())["joint"][0]["satisfied"] is None
    with pytest.raises(ValueError, match="known available"):
        resource_allocation.prepare_selection([], snapshot=unknown, shares={"house": {"WoodLog": 1}}, **args)
    with pytest.raises(ValueError, match="exceeds"):
        resource_allocation.prepare_selection([], snapshot=stock(), shares={"house": {"WoodLog": 61}}, **args)


def test_alternatives_require_model_choice_and_sequential_windows_remain_distinct():
    alternatives = demands()
    for row in alternatives:
        row.update(relation="alternative", group_id="layout")
    report = resource_allocation.assess(stock(), alternatives)
    assert report["joint"][0]["status"] == "choice_required"
    assert report["joint"][0]["satisfied"] is None
    with pytest.raises(ValueError, match="one alternative"):
        resource_allocation.prepare_selection([], allocation_id="wood", expected_revision=0, snapshot=stock(), demands=alternatives,
                                              shares={"house": {"WoodLog": 20}, "kitchen": {"WoodLog": 20}}, window="now", reason="choose")
    rows = demands()
    for index, row in enumerate(rows):
        row.update(relation="sequential", group_id="stages", window=f"stage-{index}")
    report = resource_allocation.assess(stock(), rows)
    assert len(report["joint"]) == 2
    assert all(row["satisfied"] for row in report["joint"])


def permission(mode="members"):
    return {"policy_id": "FoodPolicy_1", "scope_id": "colony", "mode": mode, "members": ["Pawn_A"]}


def members(*ids, complete=True):
    return {"policy_id": "FoodPolicy_1", "scope_id": "colony", "members": list(ids or ("Pawn_A",)), "complete": complete}


def test_fixed_policy_scope_detects_new_member_even_without_content_change():
    result = policy_scope.check_scope(permission(), members("Pawn_A", "Pawn_B"))
    assert not result["allowed"]
    assert result["status"] == "scope_conflict"
    assert result["extra_members"] == ["Pawn_B"]
    assert policy_scope.check_scope(permission("shared"), members("Pawn_A", "Pawn_B"))["allowed"]
    assert not policy_scope.check_scope(permission("shared"), members(complete=False))["allowed"]


def test_policy_reference_content_and_effective_rule_takeovers_are_distinct():
    reference = {"kind": "reference", "pawn_id": "Pawn_A", "policy_id": "FoodPolicy_1"}
    effective = {"kind": "effective_rules", "pawn_id": "Pawn_A", "policy_id": "FoodPolicy_1"}
    content = {"kind": "policy_content", "policy_id": "FoodPolicy_1"}
    assert policy_scope.check_scope(permission(), members(), protections=[reference])["allowed"]
    assert not policy_scope.check_scope(permission(), members(), protections=[reference], action="assign", pawn_ids=["Pawn_A"])["allowed"]
    assert not policy_scope.check_scope(permission(), members(), protections=[effective])["allowed"]
    assert not policy_scope.check_scope(permission(), members(), protections=[content])["allowed"]
    assert policy_scope.check_scope(permission(), members(), protections=[content], action="assign", pawn_ids=["Pawn_A"])["allowed"]
    assert policy_scope.check_scope(permission("shared"), members("Pawn_B"), protections=[effective])["allowed"]
    protected_old_policy = {**effective, "policy_id": "FoodPolicy_OLD"}
    assert not policy_scope.check_scope(permission(), members(), protections=[protected_old_policy], action="assign", pawn_ids=["Pawn_A"])["allowed"]


def test_policy_impact_replay_preserves_conflicts_and_revision(ledger):
    append(ledger, policy_scope.prepare_impact(events(ledger), impact_id="food", expected_revision=0, permission=permission(), current=members("Pawn_A", "Pawn_B")))
    row = policy_scope.project(events(ledger))["impacts"][0]
    assert row["assessment"]["status"] == "scope_conflict"
    assert row["assessment"]["requires_apply_time_check"]
    with pytest.raises(ValueError, match="revision conflict"):
        policy_scope.prepare_impact(events(ledger), impact_id="food", expected_revision=0, permission=permission(), current=members())


def test_observation_request_acknowledgment_and_rebind_are_distinct(ledger):
    request(ledger)
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "pending"
    acknowledge(ledger)
    report = observations.project(events(ledger), binding=BINDING)
    assert report["coverage"][0]["status"] == "covered"
    assert report["intents"][0]["active"]
    rebind = {**BINDING, "load_epoch": "1" * 32, "connection_epoch": "new-connection"}
    report = observations.project(events(ledger), binding=rebind)
    assert report["coverage"][0]["status"] == "pending_rebind"
    assert report["coverage"][0]["covered_condition_ids"] == []
    assert report["intents"][0]["active"]
    acknowledge(ledger, binding=rebind, receipt="restored")
    assert observations.project(events(ledger), binding=rebind)["coverage"][0]["status"] == "covered"
    assert observations.project(events(ledger), binding=rebind, enabled=False)["coverage"][0]["status"] == "inactive"


def test_same_binding_reactivation_needs_fresh_ack_and_preserves_terminal_budget(ledger):
    append(ledger, {"kind": "autoplay.activation", "source": "plugin", "payload": {}})
    request(ledger)
    acknowledge(ledger)
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "covered"
    append(ledger, observations.prepare_fact(events(ledger), observation_id="watch", revision=1, condition_id="stage", receipt_id="receipt",
                                             status="exhausted", sample_tick=100, observation={}, binding=BINDING))
    append(ledger, {"kind": "autoplay.disabled", "source": "plugin", "payload": {}})
    assert observations.project(events(ledger), binding=BINDING, enabled=False)["coverage"][0]["status"] == "inactive"
    append(ledger, {"kind": "autoplay.activation", "source": "plugin", "payload": {}})
    pending = observations.project(events(ledger), binding=BINDING)["coverage"][0]
    assert pending["status"] == "pending"
    assert pending["receipt_id"] is None
    assert pending["ack"]["receipt_id"] == "receipt"
    assert pending["condition_states"] == {"stage": "exhausted"}
    acknowledge(ledger)
    current = observations.project(events(ledger), binding=BINDING)["coverage"][0]
    assert current["status"] == "uncovered"
    assert current["condition_states"] == {"stage": "exhausted"}


def test_observation_partial_rejected_expired_and_exhausted_coverage_remain_explicit(ledger):
    request(ledger, conditions=[condition(), condition("other", target_ids=["Blueprint_2"])])
    acknowledge(ledger, covered=["stage"])
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "partial"
    append(ledger, observations.prepare_fact(events(ledger), observation_id="watch", revision=1, condition_id="stage", receipt_id="receipt",
                                             status="exhausted", sample_tick=100, observation={}, binding=BINDING))
    row = observations.project(events(ledger), binding=BINDING)["coverage"][0]
    assert row["status"] == "uncovered"
    assert row["uncovered_condition_ids"] == ["other", "stage"]
    acknowledge(ledger, status="rejected", covered=[], receipt="rejected")
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "rejected"


def test_observation_updates_reject_stale_acks_and_revoke_survives_reconnect(ledger):
    request(ledger)
    acknowledge(ledger)
    request(ledger, expected=1, conditions=[condition("new")])
    with pytest.raises(ValueError, match="current active revision"):
        acknowledge(ledger)
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "pending"
    append(ledger, observations.prepare_revoke(events(ledger), observation_id="watch", expected_revision=2, reason="task ended"))
    changed = {**BINDING, "connection_epoch": "reconnect"}
    assert observations.project(events(ledger), binding=changed)["coverage"][0]["status"] == "inactive"
    assert not observations.project(events(ledger), binding=changed)["intents"][0]["executable"]
    with pytest.raises(ValueError, match="current active revision"):
        acknowledge(ledger, revision=2, covered=["new"])


@pytest.mark.parametrize("replacement", [{"kind": "arbitrary_dsl"}, {"max_samples": 0}, {"expires_at_tick": 100}, {"sample_interval_ticks": 0}, {"target_ids": []}, {"predicate": "ignore unbuildable"}])
def test_observation_registration_rejects_unsupported_or_unbounded_conditions(ledger, replacement):
    with pytest.raises(ValueError):
        request(ledger, conditions=[condition(**replacement)])


def test_observation_native_facts_need_matching_receipt_and_condition(ledger):
    request(ledger)
    acknowledge(ledger)
    args = dict(observation_id="watch", revision=1, condition_id="stage", receipt_id="receipt", status="changed", sample_tick=100, observation={}, binding=BINDING)
    for replacement in ({"receipt_id": "old"}, {"condition_id": "other"}, {"binding": {**BINDING, "connection_epoch": "old"}}):
        with pytest.raises(ValueError, match="current native receipt"):
            observations.prepare_fact(events(ledger), **{**args, **replacement})
    append(ledger, observations.prepare_fact(events(ledger), **args))
    assert observations.project(events(ledger), binding=BINDING)["intents"][0]["latest_facts"][0]["sample_tick"] == 100


@pytest.mark.parametrize("mode", ["rejected", "partial", "expired", "exhausted"])
def test_unregistered_or_terminal_observation_facts_cannot_claim_coverage(ledger, mode):
    request(ledger, conditions=[condition(), condition("other", target_ids=["Blueprint_2"])])
    acknowledge(ledger, status="rejected" if mode == "rejected" else "registered", covered=[] if mode == "rejected" else ["stage"])
    args = dict(observation_id="watch", revision=1, condition_id="other" if mode == "partial" else "stage", receipt_id="receipt",
                status="changed", sample_tick=100, observation={}, binding=BINDING)
    if mode in {"expired", "exhausted"}:
        append(ledger, observations.prepare_fact(events(ledger), **{**args, "status": mode}))
        acknowledge(ledger, covered=["stage"])
        assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["condition_states"]["stage"] == mode
    with pytest.raises(ValueError):
        observations.prepare_fact(events(ledger), **args)


def test_temporarily_unknown_observation_can_recover_under_same_native_receipt(ledger):
    request(ledger)
    acknowledge(ledger)
    args = dict(observation_id="watch", revision=1, condition_id="stage", receipt_id="receipt", sample_tick=100, observation={}, binding=BINDING)
    append(ledger, observations.prepare_fact(events(ledger), status="unknown", **args))
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "uncovered"
    append(ledger, observations.prepare_fact(events(ledger), status="changed", **args))
    assert observations.project(events(ledger), binding=BINDING)["coverage"][0]["status"] == "covered"


def test_observation_quota_counts_all_active_targets_but_not_revoked_intents(ledger):
    targets = [f"Blueprint_{index}" for index in range(64)]
    request(ledger, conditions=[condition(target_ids=targets)])
    with pytest.raises(ValueError, match="quota"):
        observations.prepare_request(events(ledger), observation_id="other", task_id="task", expected_revision=0,
                                     conditions=[condition()], supported_kinds={"construction"}, binding=BINDING)
    append(ledger, observations.prepare_revoke(events(ledger), observation_id="watch", expected_revision=1, reason="new scope"))
    spec = observations.prepare_request(events(ledger), observation_id="other", task_id="task", expected_revision=0,
                                        conditions=[condition()], supported_kinds={"construction"}, binding=BINDING)
    append(ledger, spec)
    assert observations.project(events(ledger), binding=BINDING)["intents"][1]["active"]


def test_observation_admission_rejects_expiry_beyond_native_tick_range(ledger):
    from plugin.plugins.neko_rimworld.game.observation_contract import MAX_NATIVE_TICK

    with pytest.raises(ValueError, match="positive native tick"):
        observations.prepare_request(events(ledger), observation_id="watch", task_id="task", expected_revision=0,
                                     conditions=[condition(expires_at_tick=MAX_NATIVE_TICK + 1)],
                                     supported_kinds={"construction"}, binding=BINDING)


@pytest.mark.parametrize("mode", ["time_expired", "exhausted"])
def test_naturally_finished_observations_release_concurrent_target_quota(ledger, mode):
    targets = [f"Blueprint_{index}" for index in range(64)]
    request(ledger, conditions=[condition(target_ids=targets)])
    now_tick = 1001 if mode == "time_expired" else 100
    if mode == "exhausted":
        acknowledge(ledger)
        append(ledger, observations.prepare_fact(events(ledger), observation_id="watch", revision=1, condition_id="stage",
                                                 receipt_id="receipt", status="exhausted", sample_tick=100, observation={}, binding=BINDING))
    spec = observations.prepare_request(events(ledger), observation_id="other", task_id="task", expected_revision=0,
                                        conditions=[condition(expires_at_tick=2000)], supported_kinds={"construction"}, binding=BINDING, now_tick=now_tick)
    append(ledger, spec)
    assert len(observations.project(events(ledger), binding=BINDING)["intents"]) == 2


def test_journal_transaction_can_atomically_reject_a_multidomain_change(ledger):
    adoption = construction.prepare_adoption([], plan_id="house", task_id="task", plan=plan(), source={}, load_epoch=EPOCH)
    with pytest.raises(ValueError, match="known available"):
        with ledger.transaction():
            ledger.append("colony", EPOCH, 100, adoption["source"], adoption["kind"], adoption["payload"])
            resource_allocation.prepare_selection(events(ledger), allocation_id="wood", expected_revision=0, snapshot=stock(),
                                                  demands=demands(), shares={"house": {"WoodLog": 60}, "kitchen": {"WoodLog": 50}}, window="now", reason="overdraw")
    assert events(ledger) == []


def test_event_payloads_replay_after_process_restart(tmp_path):
    path = tmp_path / "replay.sqlite"
    ledger = EventLedger(path)
    adopt(ledger)
    request(ledger)
    acknowledge(ledger)
    before = {"construction": construction.project(events(ledger), load_epoch=EPOCH),
              "observations": observations.project(events(ledger), binding=BINDING)}
    ledger.close()
    ledger = EventLedger(path)
    try:
        after = {"construction": construction.project(events(ledger), load_epoch=EPOCH),
                 "observations": observations.project(events(ledger), binding=BINDING)}
        assert after == before
        assert ledger.events("other-colony") == []
    finally:
        ledger.close()
