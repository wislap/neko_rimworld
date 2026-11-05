"""Fixed board skeleton and author rules; pure checks with no storage access."""

from ..values import is_nonblank_text
from .ledger import FRONTDESK, PLANNER, is_integer

FIELDS = frozenset(
    {
        "entry_id",
        "expected_revision",
        "kind",
        "status",
        "priority",
        "parent",
        "source",
        "evidence",
        "review_at_tick",
        "changesets",
        "body",
    }
)
AUTHOR_OF = {"strategy": PLANNER, "intent": FRONTDESK, "task": PLANNER}
STATUSES = frozenset({"open", "drafting", "decided", "committed", "verified", "blocked", "closed"})
STATUSES_BY_AUTHOR = {FRONTDESK: frozenset({"open", "blocked", "closed"}), PLANNER: STATUSES}
# A task hangs under a strategy or an intent and implements one of them (DESIGN 4.12.2).
TASK_TARGET_KINDS = frozenset({"strategy", "intent"})


def entry_schema(actor):
    """JSON Schema for a complete board submission by an author."""
    if actor not in STATUSES_BY_AUTHOR:
        raise ValueError(f"Unknown board author: {actor}")
    text = {"type": "string", "pattern": r"\S"}
    kinds = sorted(kind for kind, author in AUTHOR_OF.items() if author == actor)
    links = {
        "strategy": {"parent": {"type": "null"}, "source": {
            "type": "object", "properties": {}, "additionalProperties": False}},
        "task": {"parent": text, "source": {"type": "object", "properties": {"entry_id": text},
            "required": ["entry_id"], "additionalProperties": False}},
        "intent": {"parent": {"type": "null"}, "source": {"type": "object", "properties": {
            "event_seq": {"type": "integer", "minimum": 0}}, "required": ["event_seq"],
            "additionalProperties": False}},
    }
    return {
        "type": "object",
        "description": ("Submit every field. New entries use expected_revision 0; revisions use the last read revision. "
                        "Strategy example: parent=null, source={}. Task example: parent=\"strategy-id\", "
                        "source={\"entry_id\":\"strategy-id\"}; both references must name an existing strategy or intent. "
                        "Intent example: parent=null, source={\"event_seq\":1}, citing original user words. "
                        "Committed requires changesets; verified requires evidence."),
        "properties": {
            "entry_id": text, "expected_revision": {"type": "integer", "minimum": 0},
            "kind": {"type": "string", "enum": kinds},
            "status": {"type": "string", "enum": sorted(STATUSES_BY_AUTHOR[actor])},
            "priority": {"type": "integer"}, "parent": {"type": ["string", "null"]},
            "source": {"type": "object"},
            "evidence": {"type": "array", "items": text},
            "review_at_tick": {"type": ["integer", "null"], "minimum": 0},
            "changesets": {"type": "array", "items": text}, "body": {"type": "string"},
        },
        "required": sorted(FIELDS), "additionalProperties": False,
        "anyOf": [
            {"properties": {"kind": {"type": "string", "enum": [kind]}, **links[kind]},
             "anyOf": [
                 {"properties": {"status": {"type": "string", "enum": sorted(STATUSES_BY_AUTHOR[actor] - {"committed", "verified"})}}},
                 *({"properties": {"status": {"type": "string", "enum": [status]},
                                  field: {"type": "array", "minItems": 1}}}
                   for status, field in (("committed", "changesets"), ("verified", "evidence"))
                   if status in STATUSES_BY_AUTHOR[actor]),
             ]}
            for kind in kinds
        ],
    }


class BoardError(ValueError):
    pass


class RevisionConflict(BoardError):
    pass


class AuthorViolation(BoardError):
    pass


def check_shape(submission):
    """Reject anything outside the fixed skeleton before any storage lookup."""
    if not isinstance(submission, dict) or set(submission) != FIELDS:
        raise BoardError("Board entries use the fixed skeleton fields")
    if not is_nonblank_text(submission["entry_id"]) or not _choice(submission["kind"], AUTHOR_OF):
        raise BoardError("An entry names its id and a known kind")
    if not _count(submission["expected_revision"]) or not _choice(submission["status"], STATUSES):
        raise BoardError("A revision names the revision it read and a known status")
    if not is_integer(submission["priority"]) or not isinstance(submission["body"], str):
        raise BoardError("Priority is an integer and body is text")
    if submission["review_at_tick"] is not None and not _count(submission["review_at_tick"]):
        raise BoardError("A review tick is a non-negative integer or null")
    if submission["parent"] is not None and not is_nonblank_text(submission["parent"]):
        raise BoardError("parent must be null for strategy or intent; task requires a non-empty strategy or intent entry id")
    if not isinstance(submission["source"], dict):
        raise BoardError("source must be an object: strategy uses {}, task uses {\"entry_id\":\"...\"}, intent uses {\"event_seq\":...}")
    for name in ("evidence", "changesets"):
        if not isinstance(submission[name], list) or not all(is_nonblank_text(item) for item in submission[name]):
            raise BoardError(f"{name} is a list of identifiers")
    _check_link_shape(submission)


def check_revision(current, actor, submission):
    """Apply author, revision and status rules; return the row to append.

    `current` is the entry's latest stored revision, or None for a new entry. The submitter
    names the revision it read (`expected_revision`, 0 for a new entry); anything else conflicts.
    """
    kind = submission["kind"]
    if current is not None:
        if actor != current["author"]:
            raise AuthorViolation("Only the entry's author may write a new revision")
        if kind != current["kind"]:
            raise BoardError("An entry's kind is fixed")
    if actor != AUTHOR_OF[kind]:
        raise AuthorViolation(f"{kind} entries are written by {AUTHOR_OF[kind]}")
    seen = 0 if current is None else current["revision"]
    if submission["expected_revision"] != seen:
        raise RevisionConflict(f"Entry {submission['entry_id']} is at revision {seen}")
    status = submission["status"]
    if status not in STATUSES_BY_AUTHOR[actor]:
        raise AuthorViolation(f"{actor} may not set status {status}")
    if status == "committed" and not submission["changesets"]:
        raise BoardError("A committed entry names its changesets")
    if status == "verified" and not submission["evidence"]:
        raise BoardError("A verified entry cites its verification evidence")
    row = {key: value for key, value in submission.items() if key != "expected_revision"}
    return {**row, "revision": seen + 1, "author": actor}


def _check_link_shape(submission):
    kind, source, parent = submission["kind"], submission["source"], submission["parent"]
    if kind == "strategy" and (parent is not None or source != {}):
        raise BoardError("A strategy requires parent=null and source={}")
    if kind == "intent" and (parent is not None or set(source) != {"event_seq"} or not _count(source["event_seq"])):
        raise BoardError("An intent requires parent=null and source={\"event_seq\": <non-negative integer>} citing user words")
    if kind == "task" and (parent is None or set(source) != {"entry_id"} or not is_nonblank_text(source["entry_id"])):
        raise BoardError("A task requires a non-empty parent and source={\"entry_id\": <non-empty entry id>}; both must reference a strategy or intent")


def _choice(value, allowed):
    return is_nonblank_text(value) and value in allowed


def _count(value):
    return is_integer(value) and value >= 0
