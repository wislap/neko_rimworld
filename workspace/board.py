"""Fixed board skeleton and author rules; pure checks with no storage access."""

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
    if not _text(submission["entry_id"]) or not _text(submission["kind"], AUTHOR_OF):
        raise BoardError("An entry names its id and a known kind")
    if not _count(submission["expected_revision"]) or not _text(submission["status"], STATUSES):
        raise BoardError("A revision names the revision it read and a known status")
    if not is_integer(submission["priority"]) or not isinstance(submission["body"], str):
        raise BoardError("Priority is an integer and body is text")
    if submission["review_at_tick"] is not None and not _count(submission["review_at_tick"]):
        raise BoardError("A review tick is a non-negative integer or null")
    if submission["parent"] is not None and not _text(submission["parent"]):
        raise BoardError("A parent names an entry")
    if not isinstance(submission["source"], dict):
        raise BoardError("A source is an object")
    for name in ("evidence", "changesets"):
        if not isinstance(submission[name], list) or not all(_text(item) for item in submission[name]):
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
        raise BoardError("A strategy has no parent or source")
    if kind == "intent" and (parent is not None or set(source) != {"event_seq"} or not _count(source["event_seq"])):
        raise BoardError("An intent cites one user utterance by event sequence")
    if kind == "task" and (parent is None or set(source) != {"entry_id"} or not _text(source["entry_id"])):
        raise BoardError("A task names its parent and the entry it implements")


def _text(value, allowed=None):
    return isinstance(value, str) and bool(value.strip()) and (allowed is None or value in allowed)


def _count(value):
    return is_integer(value) and value >= 0
