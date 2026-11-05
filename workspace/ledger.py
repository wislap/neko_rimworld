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
OBSERVED_EVENT = "colony.observed"
TAKEOVER_EVENT = "user.takeover"
RETURN_EVENT = "user.return"
RUN_EVENT = "agent.run"
RESERVED_EVENTS = frozenset({REVISION_EVENT, UTTERANCE_EVENT, LOAD_EVENT, OBSERVED_EVENT, TAKEOVER_EVENT, RETURN_EVENT, RUN_EVENT, "frontdesk.context_policy", "scheduler.policy", "scheduler.default_review", "wake.registered", "wake.cancelled", "wake.evaluated", "autoplay.activation", "user.policy_protection", "user.policy_protection_released", "construction.restarted", "area.manual", "area.manual_snapshot", "construction.purpose_assessed"})

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
            CREATE INDEX events_evidence_reference ON events(colony_id, kind, json_extract(payload, '$.observation.observation_id'), seq);
            CREATE INDEX events_kind_colony_tick ON events(kind, colony_id, game_tick, seq);
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
        "events_evidence_reference": ("CREATE INDEX events_evidence_reference ON events"
                                      "(colony_id, kind, json_extract(payload, '$.observation.observation_id'), seq)"),
        "events_kind_colony_tick": "CREATE INDEX events_kind_colony_tick ON events(kind, colony_id, game_tick, seq)",
    }
    TRIGGERS = frozenset(
        {"sealed_event_update", "sealed_event_delete", "sealed_revision_update", "sealed_revision_delete"}
    )

    def __init__(self, path: Path):
        if path.exists():
            # Only the exact D2 schema may receive the additive derived index.
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as existing:
                existing.row_factory = sqlite3.Row
                upgrade = self._validate_schema(existing, allow_missing_derived_indexes=True)
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            try:
                if upgrade:
                    with self.transaction():
                        missing = self._validate_schema(self.db, allow_missing_derived_indexes=True)
                        for name in sorted(missing):
                            self.db.execute(self.REQUIRED_INDEX_SQL[name])
            except Exception:
                self.db.close()
                raise
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, isolation_level=None)
            self.db.executescript(self.SCHEMA)
        self.db.row_factory = sqlite3.Row
        try:
            self._validate_schema(self.db)
            self.db.execute("PRAGMA foreign_keys=ON")
            self._narrative_cache = {}
        except Exception:
            self.db.close()
            raise

    @classmethod
    def _validate_schema(cls, db, *, allow_missing_derived_indexes=False):
        """Require the two tables with their columns and sealing triggers; never migrate or repair."""
        try:
            for table, columns in cls.COLUMNS.items():
                present = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if present != columns:
                    raise ValueError(f"Unsupported event ledger schema: {table} requires current columns")
            indexes = {row["name"]: row["sql"] for row in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            )}
            additive = {"events_evidence_reference", "events_kind_colony_tick"}
            missing = additive - indexes.keys()
            if any(indexes.get(name) != sql for name, sql in cls.REQUIRED_INDEX_SQL.items()
                   if not (name in missing and allow_missing_derived_indexes)):
                raise ValueError("Unsupported event ledger schema: required event indexes are missing or changed")
            triggers = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            if not cls.TRIGGERS <= triggers:
                raise ValueError("Unsupported event ledger schema: append-only triggers are required")
            # Compare actual definitions, not just names: tampered table constraints,
            # triggers, extra objects and changed non-required indexes must not be upgraded.
            with closing(sqlite3.connect(":memory:")) as expected:
                expected.executescript(cls.SCHEMA)
                query = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                wanted = {(row[0], row[1]): row[2] for row in expected.execute(query)}
                actual = {(row[0], row[1]): row[2] for row in db.execute(query)}
                if allow_missing_derived_indexes:
                    for name in missing:
                        wanted.pop(("index", name))
                if actual != wanted:
                    raise ValueError("Unsupported event ledger schema: definitions differ from the known schema")
            return missing
        except sqlite3.DatabaseError as error:
            raise ValueError("Unsupported event ledger database: current SQLite schema is required") from error

    def close(self):
        self._narrative_cache.clear()
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

    def cadence_events(self, colony_id):
        """Read only cadence inputs, without decoding model messages or audits."""
        from .cadence_events import INPUT_KINDS, event_view
        kinds = INPUT_KINDS
        marks = ",".join("?" for _ in kinds)
        rows = self.db.execute(
            "SELECT seq,colony_id,load_epoch,game_tick,wall_time,source,kind, "
            "CASE WHEN kind='planner.finished' THEN json_object('start_seq', json_extract(payload,'$.start_seq'), 'result', "
            "json_object('terminal',json_object('kind',json_extract(payload,'$.result.terminal.kind')))) "
            "WHEN kind='planner.started' THEN json_object("
            "'wake_seqs',json(COALESCE(json_extract(payload,'$.wake_seqs'),'[]')), "
            "'work_ids',json(COALESCE(json_extract(payload,'$.work_ids'),'[]')), "
            "'signal_seqs',json(COALESCE(json_extract(payload,'$.signal_seqs'),'[]'))) "
            "ELSE payload END AS payload FROM events WHERE colony_id=? AND kind IN (" + marks + ") ORDER BY seq",
            (colony_id, *kinds),
        )
        return event_view(_event(row) for row in rows)

    def latest_observed_tick(self, colony_id, load_epoch):
        row = self.db.execute("SELECT MAX(game_tick) FROM events WHERE colony_id=? AND load_epoch=?",
                              (colony_id, load_epoch)).fetchone()
        return row[0]

    def recording_tick(self, colony_id, load_epoch, context_tick):
        """Choose a business append tick while its caller owns the transaction."""
        if not is_integer(context_tick):
            raise ValueError("A game tick is a 64-bit integer")
        latest = self.latest_observed_tick(colony_id, load_epoch)
        return context_tick if latest is None else max(context_tick, latest)

    def game_event_cursor(self, colony_id, load_epoch, connection):
        row = self.db.execute(
            "SELECT json_extract(payload,'$.event.cursor') FROM events "
            "WHERE colony_id=? AND load_epoch=? AND kind='game.event' "
            "AND json_extract(payload,'$.connection')=? ORDER BY seq DESC LIMIT 1", (colony_id, load_epoch, connection),
        ).fetchone()
        return row[0] if row else 0

    def game_event_at_cursor(self, colony_id, load_epoch, connection, cursor):
        row = self.db.execute(
            "SELECT * FROM events WHERE colony_id=? AND load_epoch=? AND kind='game.event' "
            "AND json_extract(payload,'$.connection')=? AND json_extract(payload,'$.event.cursor')=?",
            (colony_id, load_epoch, connection, cursor),
        ).fetchone()
        return _event(row) if row else None

    def current_epoch(self, colony_id):
        """Use confirmed load/activation/native facts, never role progress or outcomes."""
        row = self.db.execute(
            "SELECT load_epoch FROM events WHERE colony_id=? "
            "AND kind IN ('colony.observed','colony.load','autoplay.activation','game.event') ORDER BY seq DESC LIMIT 1",
            (colony_id,),
        ).fetchone()
        return row[0] if row else None

    def retained_response(self, colony_id, load_epoch, question, request_id):
        # A run may start from a game/agent trigger and receive the host request
        # later through the inbox. Resolve that wake to the original run anchor.
        row = self.db.execute(
            """SELECT r.*, (SELECT d.payload FROM events d
                 WHERE d.colony_id=r.colony_id AND d.load_epoch=r.load_epoch AND d.kind='agent.delivery'
                   AND json_extract(d.payload,'$.utterance_seq')=coalesce(json_extract(r.payload,'$.utterance_seq'),json_extract(r.payload,'$.trigger_seq'))
                 ORDER BY d.seq DESC LIMIT 1) AS delivery_payload
               FROM events r JOIN events u ON u.colony_id=r.colony_id AND u.load_epoch=r.load_epoch
               WHERE r.colony_id=? AND r.load_epoch=? AND r.kind=? AND u.kind=?
                 AND json_extract(u.payload,'$.text')=? AND json_extract(u.payload,'$.request_id')=?
                 AND json_extract(r.payload,'$.result.status') IN ('ok','degraded')
                 AND (json_extract(r.payload,'$.utterance_seq')=u.seq OR EXISTS (
                   SELECT 1 FROM events w JOIN events i ON i.colony_id=w.colony_id AND i.load_epoch=w.load_epoch
                     AND i.kind='frontdesk.inbox' AND json_extract(i.payload,'$.wake_seq')=w.seq
                   WHERE w.colony_id=r.colony_id AND w.load_epoch=r.load_epoch AND w.kind='frontdesk.wake'
                     AND json_extract(w.payload,'$.utterance_seq')=u.seq
                     AND coalesce(json_extract(i.payload,'$.utterance_seq'),json_extract(i.payload,'$.trigger_seq'))=
                         coalesce(json_extract(r.payload,'$.utterance_seq'),json_extract(r.payload,'$.trigger_seq'))
                     AND i.seq<r.seq))
               ORDER BY r.seq DESC LIMIT 1""",
            (colony_id, load_epoch, RUN_EVENT, UTTERANCE_EVENT, question, request_id),
        ).fetchone()
        return _event(row) if row else None

    def progress(self, colony_id, utterance_seq):
        rows = self.db.execute("SELECT * FROM events WHERE colony_id=? AND kind='agent.progress' AND coalesce(json_extract(payload,'$.trigger_seq'),json_extract(payload,'$.utterance_seq'))=? ORDER BY seq", (colony_id, utterance_seq))
        return [_event(row) for row in rows]

    def progress_metadata(self, colony_id, utterance_seq):
        rows = self.db.execute(
            "SELECT json_extract(payload,'$.tool_call_id') AS call_id, "
            "json_extract(payload,'$.observation.observation_id') AS evidence_id, "
            "json_extract(payload,'$.observation.request') AS request, "
            "json_extract(payload,'$.observation.response.game_tick') AS game_tick "
            "FROM events WHERE colony_id=? AND kind='agent.progress' "
            "AND coalesce(json_extract(payload,'$.trigger_seq'),json_extract(payload,'$.utterance_seq'))=? AND json_type(payload,'$.observation')='object' ORDER BY seq",
            (colony_id, utterance_seq),
        )
        return [{"payload": {"tool_call_id": row["call_id"], "observation": {
            "observation_id": row["evidence_id"], "request": json.loads(row["request"]) if row["request"] else {},
            "response": {"game_tick": row["game_tick"]},
        }}} for row in rows]

    def evidence(self, colony_id, observation_id):
        row = self.db.execute("SELECT * FROM events WHERE colony_id=? AND kind='agent.progress' "
                              "AND json_extract(payload,'$.observation.observation_id')=? ORDER BY seq DESC LIMIT 1",
                              (colony_id, observation_id)).fetchone()
        return _event(row) if row else None

    def native_work_evidence(self, colony_id, observation_ids):
        rows = self.db.execute(
            "SELECT * FROM events WHERE colony_id=? AND kind IN ('agent.progress','planner.progress') "
            "AND json_extract(payload,'$.observation_source')='native' "
            "AND json_extract(payload,'$.observation.observation_id') IN (SELECT value FROM json_each(?)) ORDER BY seq",
            (colony_id, encode(list(observation_ids))))
        return [_event(row) for row in rows]

    def interrupted_events(self, colony_id):
        # GROUP BY materializes completed anchors once, without re-parsing each
        # large run payload for every candidate progress event.
        rows = self.db.execute(
            "WITH completed AS (SELECT coalesce(json_extract(payload,'$.trigger_seq'),"
            "json_extract(payload,'$.utterance_seq')) AS anchor FROM events "
            "WHERE colony_id=? AND kind=? GROUP BY anchor) "
            "SELECT e.* FROM events e LEFT JOIN completed c ON c.anchor="
            "CASE WHEN e.kind IN (?, 'frontdesk.trigger') THEN e.seq ELSE "
            "coalesce(json_extract(e.payload,'$.trigger_seq'),json_extract(e.payload,'$.utterance_seq')) END "
            "WHERE e.colony_id=? AND e.kind IN (?, 'frontdesk.trigger', 'agent.progress') "
            "AND c.anchor IS NULL ORDER BY e.seq",
            (colony_id, RUN_EVENT, UTTERANCE_EVENT, colony_id, UTTERANCE_EVENT),
        )
        return [_event(row) for row in rows]

    def context_events(self, colony_id, cutoff, pinned, after_run_seq=0):
        # Cached narration is a disposable projection; new log rows alone are scanned.
        state = self._narrative_cache.get(colony_id)
        if state is None or after_run_seq < state["covered"]:
            state = {"covered": after_run_seq, "last_scanned": after_run_seq, "runs": {}}
            self._narrative_cache[colony_id] = state
        state["covered"] = after_run_seq
        state["runs"] = {seq: event for seq, event in state["runs"].items() if seq > after_run_seq}
        highwater = self.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE colony_id=?", (colony_id,)).fetchone()[0]
        cursor = max(state["last_scanned"], after_run_seq)
        while True:
            rows = self.db.execute(
                "SELECT seq,colony_id,load_epoch,game_tick,wall_time,source,kind,"
                "json_object('trigger_seq',coalesce(json_extract(payload,'$.trigger_seq'),json_extract(payload,'$.utterance_seq'))) AS payload "
                "FROM events WHERE kind=? AND colony_id=? AND seq>? AND seq<=? AND "
                "EXISTS (SELECT 1 FROM json_each(events.payload,'$.messages') m WHERE json_extract(m.value,'$.role')='assistant' AND "
                "((json_type(m.value,'$.content')='text' AND length(json_extract(m.value,'$.content'))>0) OR "
                "EXISTS (SELECT 1 FROM json_each(m.value,'$.tool_calls') c WHERE json_extract(c.value,'$.function.name')='submit_response' AND "
                "CASE WHEN json_valid(json_extract(c.value,'$.function.arguments')) THEN "
                "((json_type(json_extract(c.value,'$.function.arguments'),'$.answer')='text' AND length(json_extract(json_extract(c.value,'$.function.arguments'),'$.answer'))>0) OR "
                "(json_type(json_extract(c.value,'$.function.arguments'),'$.detail')='text' AND length(json_extract(json_extract(c.value,'$.function.arguments'),'$.detail'))>0)) ELSE 0 END))) ORDER BY seq LIMIT 256",
                (RUN_EVENT, colony_id, cursor, highwater),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                event = _event(row)
                event["payload"]["messages"] = self._context_messages(event["seq"], True)
                state["runs"][event["seq"]] = event
            cursor = rows[-1]["seq"]
        state["last_scanned"] = max(highwater, cursor)
        selected = dict(state["runs"])
        for row in self.db.execute(
            "SELECT * FROM events WHERE kind IN (?, 'frontdesk.trigger') AND colony_id=? AND game_tick>=?", (UTTERANCE_EVENT, colony_id, cutoff)
        ):
            selected[row["seq"]] = _event(row)
        for row in self.db.execute(
            "SELECT * FROM events WHERE kind=? AND colony_id=? AND seq IN (SELECT value FROM json_each(?))",
            (UTTERANCE_EVENT, colony_id, encode(list(pinned))),
        ):
            selected[row["seq"]] = _event(row)
        for row in self.db.execute(
            "SELECT seq,colony_id,load_epoch,game_tick,wall_time,source,kind,"
            "json_object('trigger_seq',coalesce(json_extract(payload,'$.trigger_seq'),json_extract(payload,'$.utterance_seq'))) AS payload "
            "FROM events WHERE kind=? AND colony_id=? AND game_tick>=?", (RUN_EVENT, colony_id, cutoff)
        ):
            if row["seq"] not in selected:
                event = _event(row)
                event["payload"]["messages"] = self._context_messages(event["seq"], event["seq"] > after_run_seq)
                selected[event["seq"]] = event
        return [selected[seq] for seq in sorted(selected)]

    def _context_messages(self, seq, include_narrative):
        rows = self.db.execute(
            "SELECT m.key AS idx, json_extract(m.value,'$.role') AS role, "
            "CASE WHEN ? AND json_extract(m.value,'$.role')='assistant' THEN json_extract(m.value,'$.content') END AS content, "
            "json_extract(m.value,'$.tool_call_id') AS tool_call_id "
            "FROM events e, json_each(e.payload,'$.messages') m WHERE e.seq=? ORDER BY m.key", (include_narrative, seq),
        ).fetchall()
        messages = []
        by_index = {}
        for row in rows:
            message = {"role": row["role"]}
            if row["role"] == "assistant":
                message["content"] = row["content"]
            else:
                message.update(tool_call_id=row["tool_call_id"], content="")
            messages.append(message)
            by_index[row["idx"]] = message
        calls = self.db.execute(
            "SELECT m.key AS idx, json_extract(c.value,'$.id') AS id, json_extract(c.value,'$.type') AS type, "
            "json_extract(c.value,'$.function.name') AS name, CASE WHEN ? AND json_extract(c.value,'$.function.name')='submit_response' "
            "THEN json_extract(c.value,'$.function.arguments') END AS arguments "
            "FROM events e, json_each(e.payload,'$.messages') m, json_each(m.value,'$.tool_calls') c WHERE e.seq=? ORDER BY m.key,c.key",
            (include_narrative, seq),
        )
        for row in calls:
            by_index[row["idx"]].setdefault("tool_calls", []).append({"id": row["id"], "type": row["type"] or "function", "function": {"name": row["name"], "arguments": row["arguments"] or "{}"}})
        return messages

    def events_by_kind(self, colony_id, kinds, *, after_seq=0, load_epoch=None, anchor=None):
        """Read selected full events, ordered by sequence, with optional scope filters."""
        sql = ("SELECT * FROM events INDEXED BY events_kind_colony_sequence "
               "WHERE colony_id=? AND kind IN (SELECT value FROM json_each(?)) "
               "AND seq>?")
        params = [colony_id, encode(list(kinds)), after_seq]
        if load_epoch is not None:
            sql += " AND load_epoch=?"
            params.append(load_epoch)
        if anchor is not None:
            sql += " AND CASE WHEN json_type(payload,'$.trigger_seq') IS NOT NULL THEN json_extract(payload,'$.trigger_seq') ELSE json_extract(payload,'$.utterance_seq') END=?"
            params.append(anchor)
        rows = self.db.execute(sql + " ORDER BY seq", params)
        return [_event(row) for row in rows]

    def planner_completion_events(self, colony_id):
        """Original write, observation and identity records in one ordered snapshot."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE colony_id=? AND "
            "((kind='planner.progress' AND (json_type(payload,'$.write') IS NOT NULL "
            "OR json_type(payload,'$.observation') IS NOT NULL)) OR kind='takeover.identities' "
            # The previous full scan examined every top-level `write` before
            # checking its kind. Preserve failure on non-object values even
            # when they occur in another kind of event.
            "OR (kind NOT IN ('planner.progress','takeover.identities') "
            "AND json_type(payload,'$.write') IS NOT NULL AND json_type(payload,'$.write')!='object')) ORDER BY seq",
            (colony_id,))
        return [_event(row) for row in rows]

    def action_events(self, colony_id):
        """Original records that can contribute to the dashboard action view."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE colony_id=? AND kind IN ('planner.progress','autoplay.undo') "
            "AND json_type(payload,'$.write')='object' AND json_extract(payload,'$.write.stage')='result' "
            "AND json_extract(payload,'$.write.mode') IN ('commit','undo') ORDER BY seq", (colony_id,))
        return [_event(row) for row in rows]

    def event_highwater(self, colony_id):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE colony_id=?", (colony_id,)).fetchone()[0]

    def event_ticks(self, colony_id, seqs):
        rows = self.db.execute(
            "SELECT seq,game_tick FROM events WHERE colony_id=? AND seq IN (SELECT value FROM json_each(?))",
            (colony_id, encode(list(seqs))))
        return {row["seq"]: row["game_tick"] for row in rows}

    def planner_finished_metadata(self, colony_id, *, through_seq=None):
        """Only fields needed to reconstruct unfinished planner ranges; never load audits."""
        rows = self.db.execute(
            "SELECT seq, json_extract(payload,'$.start_seq') AS start_seq, "
            "json_extract(payload,'$.result.terminal.kind') AS terminal_kind "
            "FROM events WHERE colony_id=? AND kind='planner.finished' AND seq<=? ORDER BY seq",
            (colony_id, INTEGER_LIMIT - 1 if through_seq is None else through_seq))
        return [{"seq": row["seq"], "start_seq": row["start_seq"], "terminal_kind": row["terminal_kind"]}
                for row in rows]

    def successful_run(self, colony_id, anchor, *, through_seq=None):
        """Latest successful frontdesk run for an anchor, with its complete result only."""
        row = self.db.execute(
            "SELECT seq,colony_id,load_epoch,game_tick,wall_time,source,kind, "
            "json_extract(payload,'$.result') AS result FROM events "
            "WHERE colony_id=? AND kind=? AND "
            "CASE WHEN json_type(payload,'$.trigger_seq') IS NOT NULL THEN json_extract(payload,'$.trigger_seq') ELSE json_extract(payload,'$.utterance_seq') END=? "
            "AND json_extract(payload,'$.result.status')='ok' AND (? IS NULL OR seq<=?) ORDER BY seq DESC LIMIT 1",
            (colony_id, RUN_EVENT, anchor, through_seq, through_seq)).fetchone()
        if row is None:
            return None
        return {**{key: row[key] for key in row.keys() if key != "result"},
                "payload": {"result": json.loads(row["result"])}}

    def latest_event(self, colony_id, kind):
        row = self.db.execute("SELECT * FROM events WHERE kind=? AND colony_id=? ORDER BY seq DESC LIMIT 1", (kind, colony_id)).fetchone()
        return _event(row) if row else None

    def event(self, colony_id, seq):
        row = self.db.execute("SELECT * FROM events WHERE colony_id=? AND seq=?", (colony_id, seq)).fetchone()
        return _event(row) if row is not None else None

    def revision(self, entry_id):
        """The latest revision of one entry in any colony; entry ids are unique across the file."""
        row = self.db.execute(
            "SELECT * FROM board_revisions WHERE entry_id=? ORDER BY revision DESC LIMIT 1", (entry_id,)
        ).fetchone()
        return _revision(row) if row is not None else None

    def revisions(self, colony_id, *, kind=None):
        sql = "SELECT * FROM board_revisions WHERE colony_id=?"
        params = [colony_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        rows = self.db.execute(sql + " ORDER BY entry_id, revision", params)
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


def consumed_planner_wakes(receipts, completions, load_epoch, through_seq):
    """Explicitly delivered wake IDs of successful runs within one log prefix."""
    completed = {event["start_seq"] for event in completions
                 if event["seq"] <= through_seq and event["terminal_kind"] == "yield"}
    consumed = set()
    for event in receipts:
        if event["load_epoch"] != load_epoch or event["seq"] > through_seq:
            continue
        start = event["seq"] if event["kind"] == "planner.started" else event["payload"].get("start_seq")
        if event["kind"] not in {"planner.started", "planner.received"} or start not in completed:
            continue
        wakes = event["payload"].get("wake_seqs", [])
        if not isinstance(wakes, list) or any(type(seq) is not int for seq in wakes):
            raise RuntimeError("Invalid planner wake identities")
        consumed.update(wakes)
    return consumed


def _event(row):
    return {**dict(row), "payload": json.loads(row["payload"])}


def _revision(row):
    return {
        **dict(row),
        "source": json.loads(row["source"]),
        "evidence": json.loads(row["evidence"]),
        "changesets": json.loads(row["changesets"]),
    }
