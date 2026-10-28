"""Append-only event log and the board revisions that sit beside it in one SQLite file."""

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from ..serialization import canonical as encode

USER = "user"
FRONTDESK = "frontdesk"
PLANNER = "planner"
PLUGIN = "plugin"
SOURCES = frozenset({USER, FRONTDESK, PLANNER, "investigator", PLUGIN, "mod"})

REVISION_EVENT = "board.revision"
UTTERANCE_EVENT = "user.utterance"
LOAD_EVENT = "colony.load"
TAKEOVER_EVENT = "user.takeover"
RETURN_EVENT = "user.return"
RUN_EVENT = "agent.run"
RESERVED_EVENTS = frozenset({REVISION_EVENT, UTTERANCE_EVENT, LOAD_EVENT, TAKEOVER_EVENT, RETURN_EVENT, RUN_EVENT})

# SQLite INTEGER is a signed 64-bit value; larger Python integers cannot be stored.
INTEGER_LIMIT = 2**63


def is_integer(value):
    return type(value) is int and -INTEGER_LIMIT <= value < INTEGER_LIMIT


class EventLedger:
    """Owns one SQLite file for every colony. Rows are never updated or deleted.

    Callers serialize access on one thread.
    """

    SCHEMA = """
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                colony_id TEXT NOT NULL,
                load_epoch TEXT NOT NULL,
                game_tick INTEGER NOT NULL,
                wall_time REAL NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX events_colony_sequence ON events(colony_id, seq);
            CREATE INDEX events_kind_colony_sequence ON events(kind, colony_id, seq);
            CREATE INDEX events_utterance_reference ON events(colony_id, kind, json_extract(payload, '$.utterance_seq'), seq);
            CREATE TABLE board_revisions (
                entry_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                colony_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL REFERENCES events(seq),
                kind TEXT NOT NULL,
                author TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                parent TEXT,
                source TEXT NOT NULL,
                evidence TEXT NOT NULL,
                review_at_tick INTEGER,
                changesets TEXT NOT NULL,
                body TEXT NOT NULL,
                PRIMARY KEY (entry_id, revision)
            );
            CREATE INDEX revisions_colony ON board_revisions(colony_id, entry_id, revision);
            CREATE TRIGGER sealed_event_update
                BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT,'Event log is append-only'); END;
            CREATE TRIGGER sealed_event_delete
                BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT,'Event log is append-only'); END;
            CREATE TRIGGER sealed_revision_update
                BEFORE UPDATE ON board_revisions
                BEGIN SELECT RAISE(ABORT,'Board revisions are append-only'); END;
            CREATE TRIGGER sealed_revision_delete
                BEFORE DELETE ON board_revisions
                BEGIN SELECT RAISE(ABORT,'Board revisions are append-only'); END;
        """
    COLUMNS = {
        "events": {"seq", "colony_id", "load_epoch", "game_tick", "wall_time", "source", "kind", "payload"},
        "board_revisions": {
            "entry_id", "revision", "colony_id", "event_seq", "kind", "author", "status", "priority",
            "parent", "source", "evidence", "review_at_tick", "changesets", "body",
        },
    }
    REQUIRED_INDEX_SQL = {
        "events_kind_colony_sequence": "CREATE INDEX events_kind_colony_sequence ON events(kind, colony_id, seq)",
        "events_utterance_reference": ("CREATE INDEX events_utterance_reference ON events"
                                       "(colony_id, kind, json_extract(payload, '$.utterance_seq'), seq)"),
    }
    TRIGGERS = frozenset(
        {"sealed_event_update", "sealed_event_delete", "sealed_revision_update", "sealed_revision_delete"}
    )

    def __init__(self, path: Path):
        if path.exists():
            # Admission cannot write, repair or migrate an unsupported database.
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as existing:
                existing.row_factory = sqlite3.Row
                self._validate_schema(existing)
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, isolation_level=None)
            self.db.executescript(self.SCHEMA)
        self.db.row_factory = sqlite3.Row
        try:
            self._validate_schema(self.db)
            self.db.execute("PRAGMA foreign_keys=ON")
        except Exception:
            self.db.close()
            raise

    @classmethod
    def _validate_schema(cls, db):
        """Require the two tables with their columns and sealing triggers; never migrate or repair."""
        try:
            for table, columns in cls.COLUMNS.items():
                present = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if present != columns:
                    raise ValueError(f"Unsupported event ledger schema: {table} requires current columns")
            indexes = {row["name"]: row["sql"] for row in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            )}
            if any(indexes.get(name) != sql for name, sql in cls.REQUIRED_INDEX_SQL.items()):
                raise ValueError("Unsupported event ledger schema: required event indexes are missing or changed")
            triggers = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            if not cls.TRIGGERS <= triggers:
                raise ValueError("Unsupported event ledger schema: append-only triggers are required")
        except sqlite3.DatabaseError as error:
            raise ValueError("Unsupported event ledger database: current SQLite schema is required") from error

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def append(self, colony_id, load_epoch, game_tick, source, kind, payload):
        if not isinstance(colony_id, str) or not colony_id:
            raise ValueError("An event requires a colony identity")
        if not isinstance(load_epoch, str) or not load_epoch:
            raise ValueError("An event requires the game's load epoch")
        if not is_integer(game_tick):
            raise ValueError("A game tick is a 64-bit integer")
        if source not in SOURCES:
            raise ValueError(f"Unknown event source: {source}")
        if not isinstance(kind, str) or not kind:
            raise ValueError("An event requires a kind")
        if not isinstance(payload, dict):
            raise ValueError("An event payload is an object")
        cursor = self.db.execute(
            "INSERT INTO events(colony_id,load_epoch,game_tick,wall_time,source,kind,payload) VALUES (?,?,?,?,?,?,?)",
            (colony_id, load_epoch, game_tick, time.time(), source, kind, encode(payload)),
        )
        return cursor.lastrowid

    def has_event(self, colony_id, kind, seq):
        if not is_integer(seq):
            return False
        row = self.db.execute("SELECT 1 FROM events WHERE seq=? AND colony_id=? AND kind=?", (seq, colony_id, kind))
        return row.fetchone() is not None

    def has_return(self, colony_id, takeover_seq):
        row = self.db.execute(
            "SELECT 1 FROM events WHERE colony_id=? AND kind=? AND json_extract(payload,'$.takeover_seq')=?",
            (colony_id, RETURN_EVENT, takeover_seq),
        )
        return row.fetchone() is not None

    def colonies(self):
        return [row[0] for row in self.db.execute("SELECT DISTINCT colony_id FROM events")]

    def events(self, colony_id):
        rows = self.db.execute("SELECT * FROM events WHERE colony_id=? ORDER BY seq", (colony_id,))
        return [_event(row) for row in rows]

    def retained_response(self, colony_id, load_epoch, question, request_id):
        row = self.db.execute(
            "SELECT r.*, (SELECT d.payload FROM events d WHERE d.colony_id=r.colony_id AND d.kind='agent.delivery' AND json_extract(d.payload,'$.utterance_seq')=u.seq ORDER BY d.seq DESC LIMIT 1) AS delivery_payload FROM events r JOIN events u ON u.seq=json_extract(r.payload,'$.utterance_seq') "
            "WHERE r.colony_id=? AND r.load_epoch=? AND u.load_epoch=? AND r.kind=? AND u.kind=? AND json_extract(u.payload,'$.text')=? "
            "AND json_extract(u.payload,'$.request_id')=? AND json_extract(r.payload,'$.result.status') IN ('ok','degraded') "
            "ORDER BY r.seq DESC LIMIT 1",
            (colony_id, load_epoch, load_epoch, RUN_EVENT, UTTERANCE_EVENT, question, request_id),
        ).fetchone()
        return _event(row) if row else None

    def progress(self, colony_id, utterance_seq):
        rows = self.db.execute("SELECT * FROM events WHERE colony_id=? AND kind='agent.progress' AND json_extract(payload,'$.utterance_seq')=? ORDER BY seq", (colony_id, utterance_seq))
        return [_event(row) for row in rows]

    def interrupted_events(self, colony_id):
        rows = self.db.execute(
            "SELECT e.* FROM events e WHERE e.colony_id=? AND (e.kind=? OR e.kind='agent.progress') AND NOT EXISTS "
            "(SELECT 1 FROM events r WHERE r.colony_id=e.colony_id AND r.kind=? AND json_extract(r.payload,'$.utterance_seq')=CASE WHEN e.kind=? THEN e.seq ELSE json_extract(e.payload,'$.utterance_seq') END) ORDER BY e.seq",
            (colony_id, UTTERANCE_EVENT, RUN_EVENT, UTTERANCE_EVENT),
        )
        return [_event(row) for row in rows]

    def context_events(self, colony_id, cutoff, pinned):
        # Select only the window transcript fields; never decode result/audit payloads.
        rows = self.db.execute(
            "SELECT seq,colony_id,load_epoch,game_tick,wall_time,source,kind,"
            "CASE WHEN kind=? THEN json_object('utterance_seq',json_extract(payload,'$.utterance_seq'),'messages',json_extract(payload,'$.messages')) ELSE payload END AS payload "
            "FROM events WHERE colony_id=? AND ((kind IN (?,?) AND game_tick>=?) OR (kind=? AND seq IN (SELECT value FROM json_each(?)))) ORDER BY seq",
            (RUN_EVENT, colony_id, UTTERANCE_EVENT, RUN_EVENT, cutoff, UTTERANCE_EVENT, encode(list(pinned))),
        )
        return [_event(row) for row in rows]

    def events_by_kind(self, colony_id, kinds):
        rows = self.db.execute("SELECT * FROM events WHERE colony_id=? AND kind IN (SELECT value FROM json_each(?)) ORDER BY seq", (colony_id, encode(list(kinds))))
        return [_event(row) for row in rows]

    def event(self, colony_id, seq):
        row = self.db.execute("SELECT * FROM events WHERE colony_id=? AND seq=?", (colony_id, seq)).fetchone()
        return _event(row) if row is not None else None

    def revision(self, entry_id):
        """The latest revision of one entry in any colony; entry ids are unique across the file."""
        row = self.db.execute(
            "SELECT * FROM board_revisions WHERE entry_id=? ORDER BY revision DESC LIMIT 1", (entry_id,)
        ).fetchone()
        return _revision(row) if row is not None else None

    def revisions(self, colony_id):
        rows = self.db.execute(
            "SELECT * FROM board_revisions WHERE colony_id=? ORDER BY entry_id, revision", (colony_id,)
        )
        return [_revision(row) for row in rows]

    def add_revision(self, colony_id, event_seq, row):
        self.db.execute(
            "INSERT INTO board_revisions(entry_id,revision,colony_id,event_seq,kind,author,status,priority,parent,"
            "source,evidence,review_at_tick,changesets,body) "
            "VALUES (:entry_id,:revision,:colony_id,:event_seq,:kind,:author,:status,:priority,:parent,:source,"
            ":evidence,:review_at_tick,:changesets,:body)",
            {
                **row,
                "colony_id": colony_id,
                "event_seq": event_seq,
                "source": encode(row["source"]),
                "evidence": encode(row["evidence"]),
                "changesets": encode(row["changesets"]),
            },
        )


def _event(row):
    return {**dict(row), "payload": json.loads(row["payload"])}


def _revision(row):
    return {
        **dict(row),
        "source": json.loads(row["source"]),
        "evidence": json.loads(row["evidence"]),
        "changesets": json.loads(row["changesets"]),
    }
