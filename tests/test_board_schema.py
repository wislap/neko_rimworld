"""Model-visible board contract agrees with pure submission checks."""
import copy

import jsonschema
import pytest

from plugin.plugins.neko_rimworld.workspace.board import BoardError, check_revision, check_shape, entry_schema


def entry(kind):
    parent, source = {"strategy": (None, {}), "task": ("strategy-1", {"entry_id": "strategy-1"}), "intent": (None, {"event_seq": 1})}[kind]
    return {"entry_id": kind + "-1", "expected_revision": 0, "kind": kind, "status": "open", "priority": 1, "parent": parent, "source": source, "evidence": [], "review_at_tick": None, "changesets": [], "body": "Example"}


@pytest.mark.parametrize("actor,kind", [("planner", "strategy"), ("planner", "task"), ("frontdesk", "intent")])
def test_valid_entries(actor, kind):
    value = entry(kind)
    jsonschema.validate(value, entry_schema(actor))
    check_shape(value)
    check_revision(None, actor, value)


@pytest.mark.parametrize("kind,field,value", [("strategy", "source", None), ("strategy", "source", {"entry_id": "x"}), ("strategy", "parent", "x"), ("task", "source", {}), ("task", "parent", None), ("task", "source", {"entry_id": " "}), ("intent", "source", {}), ("intent", "parent", "x"), ("intent", "source", {"event_seq": -1})])
def test_invalid_links(kind, field, value):
    actor = "frontdesk" if kind == "intent" else "planner"
    submission = entry(kind)
    submission[field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(submission, entry_schema(actor))
    with pytest.raises(BoardError):
        check_shape(submission)


@pytest.mark.parametrize("status,field", [("committed", "changesets"), ("verified", "evidence")])
def test_status_requires_references(status, field):
    submission = entry("task")
    submission["status"] = status
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(submission, entry_schema("planner"))
    with pytest.raises(BoardError):
        check_revision(None, "planner", submission)
    submission[field] = ["reference-1"]
    jsonschema.validate(submission, entry_schema("planner"))
    check_revision(None, "planner", submission)


def test_actor_schema_and_fresh_results():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(entry("intent"), entry_schema("planner"))
    schema = entry_schema("planner")
    saved = copy.deepcopy(schema)
    schema["properties"]["kind"]["enum"].clear()
    assert entry_schema("planner") == saved
