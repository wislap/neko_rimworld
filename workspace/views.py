"""Read-only, paginated projections of the append-only colony event log."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from ..serialization import canonical
from .ledger import RUN_EVENT, UTTERANCE_EVENT, EventLedger


class WorkspaceViews:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def database(self):
        if not self.path.is_file():
            yield None
            return
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.create_function("contains_folded", 2, lambda text, query: query in (text or "").casefold())
        try:
            db.execute("BEGIN")
            EventLedger._validate_schema(db)
            yield db
        finally:
            db.close()

    def read(self, action, *, workspace_id="", turn_id="", observation_id="", offset=0, limit=20,
             current_colony_id=None, search="", colony_only=False):
        if (action not in {"list", "work", "turn", "observation", "audit"}
                or not isinstance(search, str) or len(search) > 256 or type(colony_only) is not bool):
            raise ValueError("Invalid history request")
        if (any(not isinstance(value, str) or len(value) > 128 for value in (workspace_id, turn_id, observation_id))
                or (current_colony_id is not None and (not isinstance(current_colony_id, str)
                                                       or len(current_colony_id) > 128))
                or type(offset) is not int or not 0 <= offset < 2**63
                or type(limit) is not int or not 1 <= limit <= 50):
            raise ValueError("Invalid history reference or page")
        query = search.strip().casefold()
        with self.database() as db:
            if db is None:
                if action == "list":
                    return {"workspaces": [], "total": 0, "offset": offset, "next_offset": None}
                raise ValueError("No saved work is available")
            if action == "list":
                return self._list(db, current_colony_id, colony_only, query, offset, limit)
            if not db.execute("SELECT 1 FROM events WHERE colony_id=? AND kind=? LIMIT 1",
                              (workspace_id, UTTERANCE_EVENT)).fetchone():
                raise ValueError("This work is not retained")
            if action == "work":
                return self._work(db, workspace_id, query, offset, limit)
            if not turn_id.isascii() or not turn_id.isdecimal() or str(int(turn_id)) != turn_id or len(turn_id) > 19:
                raise ValueError("This exchange is not retained")
            seq = int(turn_id)
            if seq >= 2**63:
                raise ValueError("This exchange is not retained")
            utterance = db.execute("SELECT payload, wall_time, game_tick, load_epoch FROM events "
                                   "WHERE seq=? AND colony_id=? AND kind=?", (seq, workspace_id, UTTERANCE_EVENT)).fetchone()
            if utterance is None:
                raise ValueError("This exchange is not retained")
            run = db.execute("SELECT payload FROM events WHERE colony_id=? AND kind=? "
                             "AND json_extract(payload,'$.utterance_seq')=? ORDER BY seq DESC LIMIT 1",
                             (workspace_id, RUN_EVENT, seq)).fetchone()
            payload = json.loads(run["payload"]) if run else {}
            result, audit = payload.get("result"), payload.get("audit")
            observations = (result or {}).get("observations") or []
            if action == "observation":
                match = next((obs for obs in observations if isinstance(obs, dict)
                              and obs.get("observation_id") == observation_id), None)
                if match is None:
                    raise ValueError("Original evidence is unavailable")
                return {"workspace_id": workspace_id, "turn_id": turn_id, "observation_id": observation_id,
                        "historical": True, "current_observation": False, "sha256": _hash(match), "observation": match}
            if action == "audit":
                if not isinstance(audit, dict):
                    raise ValueError("No saved audit is available")
                metadata = dict(audit)
                events = metadata.pop("events", [])
                return {**metadata, "workspace_id": workspace_id, "turn_id": turn_id,
                        "events": events[offset:offset + limit], "events_page": {"offset": offset, "limit": limit,
                        "total": len(events), "has_more": offset + limit < len(events)}}
            delivery = db.execute("SELECT payload FROM events WHERE colony_id=? AND kind='agent.delivery' "
                                  "AND json_extract(payload,'$.utterance_seq')=? ORDER BY seq DESC LIMIT 1",
                                  (workspace_id, seq)).fetchone()
            evidence = []
            for obs in observations:
                if not isinstance(obs, dict):
                    continue
                response = obs.get("response") or {}
                response_payload = response.get("payload") or {} if isinstance(response, dict) else {}
                evidence.append({"observation_id": obs.get("observation_id"), "sha256": _hash(obs),
                                 "request": obs.get("request"), "received_at": obs.get("received_at"),
                                 "status": response_payload.get("status"),
                                 "error_code": response_payload.get("error_code")})
            return {"workspace": _workspace(workspace_id), "workspace_id": workspace_id, "turn_id": turn_id,
                    "question": json.loads(utterance["payload"]).get("text"),
                    "status": (result or {}).get("status", "running"), "result": result,
                    "handoff": json.loads(delivery["payload"]) if delivery else None,
                    "audit_available": audit is not None, "evidence": evidence, "history_saved": True,
                    "wall_time": utterance["wall_time"], "game_tick": utterance["game_tick"],
                    "load_epoch": utterance["load_epoch"], "historical": True, "current_observation": False}

    def _list(self, db, current, colony_only, query, offset, limit):
        # The grouping scans only utterance text; model messages and audit bodies stay in SQLite.
        where = "kind=? AND (?=0 OR colony_id=?) AND (?='' OR ? OR contains_folded(json_extract(payload,'$.text'), ?))"
        params = (UTTERANCE_EVENT, colony_only, current, query, query in "frontdesk", query)
        groups = ("SELECT colony_id, MAX(seq) AS last_sequence FROM events WHERE " + where + " GROUP BY colony_id")
        total = db.execute("SELECT count(*) FROM (" + groups + ")", params).fetchone()[0]
        rows = db.execute("SELECT g.colony_id, g.last_sequence, u.payload, "
                          "(SELECT count(*) FROM events c WHERE c.colony_id=g.colony_id AND c.kind=?) AS turn_count "
                          "FROM (" + groups + ") g JOIN events u ON u.seq=(SELECT max(seq) FROM events "
                          "WHERE colony_id=g.colony_id AND kind=?) "
                          "ORDER BY (g.colony_id=?) DESC, u.seq DESC LIMIT ? OFFSET ?",
                          (UTTERANCE_EVENT, *params, UTTERANCE_EVENT, current, limit, offset)).fetchall()
        items = []
        for row in rows:
            actual_seq = db.execute("SELECT max(seq) FROM events WHERE colony_id=? AND kind=?",
                                    (row["colony_id"], UTTERANCE_EVENT)).fetchone()[0]
            status = _run_fields(db, row["colony_id"], actual_seq)
            items.append({**_workspace(row["colony_id"]), "last_question": json.loads(row["payload"]).get("text"),
                          "last_status": (status["status"] or "running") if status else "running", "last_sequence": actual_seq,
                          "turn_count": row["turn_count"]})
        return {"workspaces": items, **_page(total, offset, limit)}

    def _work(self, db, colony, query, offset, limit):
        where = "colony_id=? AND kind=? AND (?='' OR ? OR contains_folded(json_extract(payload,'$.text'), ?))"
        params = (colony, UTTERANCE_EVENT, query, query in "frontdesk", query)
        total = db.execute("SELECT count(*) FROM events WHERE " + where, params).fetchone()[0]
        rows = db.execute("SELECT seq, payload, wall_time, game_tick, load_epoch FROM events WHERE " + where
                          + " ORDER BY seq DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        items = []
        for row in rows:
            run = _run_fields(db, colony, row["seq"])
            items.append({"turn_id": str(row["seq"]), "sequence": row["seq"],
                          "question": json.loads(row["payload"]).get("text"),
                          "status": (run["status"] or "running") if run else "running", "preview": run["preview"] if run else None,
                          "report_id": run["report_id"] if run else None,
                          "created_at": datetime.fromtimestamp(row["wall_time"], timezone.utc).isoformat(),
                          "wall_time": row["wall_time"], "game_tick": row["game_tick"], "load_epoch": row["load_epoch"]})
        return {"workspace": _workspace(colony), "workspace_id": colony, "turns": items, **_page(total, offset, limit)}


def _run_fields(db, colony, seq):
    return db.execute("SELECT json_extract(payload,'$.result.status') AS status, "
                      "json_extract(payload,'$.result.agent_text') AS preview, "
                      "json_extract(payload,'$.result.report.report_id') AS report_id FROM events "
                      "WHERE colony_id=? AND kind=? AND json_extract(payload,'$.utterance_seq')=? "
                      "ORDER BY seq DESC LIMIT 1", (colony, RUN_EVENT, seq)).fetchone()


def _workspace(colony):
    return {"workspace_id": colony, "colony_id": colony, "owner": "frontdesk", "focus": "", "work_state": "waiting"}


def _page(total, offset, limit):
    return {"total": total, "offset": offset, "next_offset": offset + limit if offset + limit < total else None}


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()
