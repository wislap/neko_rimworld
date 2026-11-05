"""Test-only in-memory game port: field values, ledger, receipts and load epochs.

It models no game rules. It only knows which field holds which value, who wrote it last, and the
transaction semantics of DESIGN 5.5: compare-then-write, ledger, undo, idempotency and epoch checks.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..tool_contracts import ReceiptLost, ToolExecutionError

DEFAULT_OBJECT_KEYS = ("pawn", "bill")
CONTROL_KEYS = frozenset({"op", "expect", "value", "evidence"})
CHANGESET_KEYS = frozenset({"changeset_id", "author", "entry_id", "load_epoch", "idempotency_key", "ops"})
HOOK_POINTS = ("before_check", "before_apply")

Address = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _Write:
    op_index: int
    address: Address
    expect: Any
    value: Any
    #: Sub-field name when the op's expect and value are dicts; None for a scalar op.
    part: str | None = None


@dataclass(slots=True)
class _Undo:
    changeset_id: str
    author: str
    epoch: str
    #: Undo as a changeset: expect is what the commit wrote, value is what it replaced.
    writes: tuple[_Write, ...]
    #: Ledger entries before the commit; undo puts them back (None means there was none).
    ledger_before: dict[Address, dict[str, Any] | None]
    consumed: bool = False


class ScriptedGameWorld:
    """Implements GameWritePort on an in-memory world. Scripts can inject conflicts, lost replies and loads."""

    def __init__(self, object_keys: Sequence[str] = DEFAULT_OBJECT_KEYS) -> None:
        #: Op keys that name the target object; one of them must appear in every op.
        self._object_keys = tuple(object_keys)
        self._epoch = 0
        self._tick = 0
        self._serial = 0
        self._paused = False
        self._pause_owner = "unknown"
        self._pause_revision = 1
        self._pause_transition_revision = 0
        self._values: dict[Address, Any] = {}
        self._origin: dict[Address, tuple[str, int]] = {}
        self._ledger: dict[Address, dict[str, Any]] = {}
        self._receipts: dict[str, tuple[str, dict[str, Any]]] = {}
        self._undo: dict[str, _Undo] = {}
        self._events: list[dict[str, Any]] = []
        self._event_active = False
        self._session_origin = "new_game"
        self._session_start_tick = 0
        self._request_serial = 0
        self._hooks: dict[str, list[Callable[[ScriptedGameWorld], None]]] = {point: [] for point in HOOK_POINTS}
        self._lose_next_receipt = False
        self._fail_after: int | None = None

    capabilities = frozenset({"colony_write.transactions.v1", "colony_events.v1", "colony_pause.v1", "colony_pause.policy.v1", "colony_pause.resume_guard.v1"})

    @property
    def load_epoch(self) -> str:
        return f"epoch-{self._epoch}"

    @property
    def paused(self) -> bool:
        return self._paused

    def advance(self, ticks: int) -> None:
        self._tick += ticks

    def player_edit(self, object_id: str, field: str, value: Any) -> None:
        """A change made outside the AI (the player, or the game itself at seeding). Never enters the ledger."""
        self._set((object_id, field), value, "user")

    def value(self, object_id: str, field: str) -> Any:
        return self._values.get((object_id, field))

    def ledger(self, object_id: str, field: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._ledger.get((object_id, field)))

    def schedule(self, point: str, action: Callable[[ScriptedGameWorld], None]) -> None:
        """Run action once, inside the next commit that reaches point (before_check or before_apply)."""
        if point not in self._hooks:
            raise ValueError(f"Unknown hook point: {point}")
        self._hooks[point].append(action)

    def lose_next_receipt(self) -> None:
        """The next commit that applies writes raises ReceiptLost instead of returning its receipt."""
        self._lose_next_receipt = True

    def fail_next_apply_after(self, writes: int) -> None:
        """The next apply raises after `writes` fields were written; all of them must be rolled back."""
        self._fail_after = writes

    def save(self) -> dict[str, Any]:
        return copy.deepcopy({"tick": self._tick, "values": self._values, "origin": self._origin, "ledger": self._ledger})

    def load(self, save: Mapping[str, Any]) -> None:
        """Restore a save. Bumps the load epoch, which invalidates receipts, undo tokens and stale changesets."""
        state = copy.deepcopy(dict(save))
        self._tick = state["tick"]
        self._values = state["values"]
        self._origin = state["origin"]
        self._ledger = state["ledger"]
        self._epoch += 1
        self._pause_owner = "unknown"
        self._pause_revision += 1
        self._receipts.clear()
        self._event_active = False
        self._events.clear()
        self._session_origin = "loaded_game"
        self._session_start_tick = self._tick

    async def dry_run(self, changeset: Mapping[str, Any]) -> dict[str, Any]:
        body = _changeset(changeset)
        writes = _writes(body["ops"], self._object_keys)
        verdict = self._verdict(body["load_epoch"], writes)
        if verdict is not None:
            return verdict
        return {
            "status": "ok",
            "preview": [
                {"op": w.op_index, "object": w.address[0], "field": w.address[1], "expect": w.expect, "value": w.value}
                for w in writes
            ],
        }

    async def commit(self, changeset: Mapping[str, Any]) -> dict[str, Any]:
        body = _changeset(changeset)
        key, fingerprint = body["idempotency_key"], _fingerprint(body)
        cached = self._receipts.get(key)
        if cached is not None:
            if cached[0] != fingerprint:
                raise ToolExecutionError(
                    "idempotency_conflict", "Idempotency key was reused for a different changeset.", recoverable=False
                )
            return copy.deepcopy(cached[1])
        writes = _writes(body["ops"], self._object_keys)
        self._run_hooks("before_check")
        verdict = self._verdict(body["load_epoch"], writes)
        if verdict is None:
            self._run_hooks("before_apply")
            verdict = self._verdict(body["load_epoch"], writes)
        if verdict is not None:
            if verdict["status"] == "conflict":
                self._receipts[key] = (fingerprint, verdict)
            return copy.deepcopy(verdict)
        ledger_before = {w.address: copy.deepcopy(self._ledger.get(w.address)) for w in writes}
        applied = self._apply(writes, writer=body["author"], ledger=lambda w: self._ai_entry(body, w))
        self._serial += 1
        token = f"undo-{self._serial}"
        self._undo[token] = _Undo(
            changeset_id=body["changeset_id"],
            author=body["author"],
            epoch=self.load_epoch,
            writes=tuple(_Write(w.op_index, w.address, w.value, before, w.part) for w, before in applied),
            ledger_before=ledger_before,
        )
        receipt = {
            "status": "committed",
            "changeset_id": body["changeset_id"],
            "load_epoch": self.load_epoch,
            "readback": self._readback(writes),
            "undo_token": token,
        }
        self._receipts[key] = (fingerprint, receipt)
        self._emit_write("commit", receipt)
        if self._lose_next_receipt:
            self._lose_next_receipt = False
            raise ReceiptLost(key)
        return copy.deepcopy(receipt)

    async def undo(self, token: str) -> dict[str, Any]:
        record = self._undo.get(token)
        if record is None:
            return {"status": "unknown_token"}
        if record.epoch != self.load_epoch:
            return {"status": "stale_epoch", "load_epoch": self.load_epoch}
        if record.consumed:
            return {"status": "already_undone"}
        verdict = self._verdict(record.epoch, record.writes)
        if verdict is not None:
            return copy.deepcopy(verdict)
        self._apply(
            record.writes,
            writer=record.author,
            ledger=lambda w: copy.deepcopy(record.ledger_before[w.address]),
        )
        record.consumed = True
        receipt = {
            "status": "undone",
            "changeset_id": record.changeset_id,
            "load_epoch": self.load_epoch,
            "readback": self._readback(record.writes),
        }
        self._emit_write("undo", receipt)
        return receipt

    async def events(self, since: int = 0) -> list[dict[str, Any]]:
        if not self._event_active:
            raise ToolExecutionError("events_not_subscribed", "Enable event delivery with push_policy first.")
        if type(since) is not int or since < 0:
            raise ValueError("Event cursor must be a non-negative integer.")
        if since > len(self._events):
            raise ToolExecutionError("event_gap", "Event cursor exceeds the current stream.", recoverable=False,
                                     details={"requested_cursor": since, "latest_cursor": len(self._events),
                                              "oldest_cursor": 1 if self._events else None,
                                              "missing_from": None, "missing_to": None})
        return copy.deepcopy([event for event in self._events if event["cursor"] > since])

    async def push_policy(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        from ..game.protocol import POLICY_REQUEST, Payload, SessionDescriptor, make_envelope

        make_envelope(POLICY_REQUEST, "0" * 32, 0, Payload(arguments=dict(policy)),
                      SessionDescriptor("1" * 32, "2" * 32, 0, 0), "3" * 32)
        if not self._event_active:
            self._event_active = True
            self._emit("session_snapshot", {"origin": self._session_origin, "start_tick": self._session_start_tick,
                                            "pause_state": self.pause_state()})
        self._emit("policy_changed", dict(policy))
        return copy.deepcopy(dict(policy))

    async def push_observations(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        raise ToolExecutionError("unsupported", "This legacy scripted fixture does not simulate native business observation.")

    async def set_paused(self, paused: bool, *, role: str, expected_owner: str | None = None) -> dict[str, Any]:
        if not isinstance(paused, bool) or role not in ("frontdesk", "planner", "policy") or (role in ("frontdesk", "policy") and not paused):
            raise ToolExecutionError("invalid_argument", "paused must be a boolean.")
        if expected_owner is not None:
            if paused or role != "planner" or expected_owner not in ("planner", "frontdesk", "reflex", "automatic_letter"):
                raise ToolExecutionError("invalid_argument", "Invalid expected pause owner.")
            if not self._paused or self._pause_owner != expected_owner:
                raise ToolExecutionError("pause_owner_changed", "The expected agent pause is no longer current.")
        if not paused and self._paused and self._pause_owner in {"user", "unknown"}:
            raise ToolExecutionError("pause_owned", "A user or unknown pause cannot be released by an agent.")
        self._change_pause(paused, role)
        return {"paused": paused, "role": role, **({"expected_owner": expected_owner} if expected_owner is not None else {})}

    def pause_state(self):
        return {"status": "observed", "connection_epoch": "scripted", "colony_id": "scripted",
                "load_epoch": self.load_epoch, "sampled_tick": self._tick, "owner": self._pause_owner,
                "pause_revision": self._pause_revision, "pause_transition_revision": self._pause_transition_revision,
                "paused": self._paused, "speed_paused": self._paused, "force_paused": False,
                "windows_force_paused": False}

    async def idle_query(self, operation, arguments):
        if operation != "read_situation" or arguments:
            raise ValueError("Only the native situation snapshot is supported")
        sample = {"colony_id": "scripted", "load_epoch": self.load_epoch, "map_id": "scripted-map",
                  "started_tick": self._tick, "finished_tick": self._tick, "snapshot_sequence": 0, "atomic": False}
        sections = {name: {"status": "ok", "source": "scripted", "rows": [],
                           "sample_tick": self._tick, "finished_tick": self._tick}
                    for name in ("alerts", "conditions", "threats")}
        attention = {"kind": "attention_snapshot", "sample_tick": self._tick, "map_id": "scripted-map",
                     "sample": sample, **sections}
        return {"response": {"connection_epoch": "scripted", "colony_id": "scripted", "load_epoch": self.load_epoch,
                             "game_tick": self._tick, "snapshot_sequence": 0,
                             "payload": {"data": {"clock": {"pause_state": self.pause_state()},
                                                  "sample": sample, "attention_snapshot": attention}}}}

    def player_set_paused(self, paused):
        if type(paused) is not bool:
            raise ValueError("Pause state must be boolean")
        self._change_pause(paused, "user")

    def _change_pause(self, paused, source):
        changed = self._paused != paused
        self._paused = paused
        if changed:
            self._pause_owner = source if paused else "none"
            self._pause_revision += 1
            self._pause_transition_revision += 1
            self._emit("pause_changed", {"paused": paused, "speed_paused": paused, "force_paused": False,
                                         "windows_force_paused": False, "source": source,
                                         "pause_state": self.pause_state()})

    def _run_hooks(self, point: str) -> None:
        actions, self._hooks[point] = self._hooks[point], []
        for action in actions:
            action(self)

    def _verdict(self, epoch: str, writes: Sequence[_Write]) -> dict[str, Any] | None:
        """None when every expectation holds in the current epoch; otherwise the receipt that says why not."""
        if epoch != self.load_epoch:
            return {"status": "stale_epoch", "load_epoch": self.load_epoch}
        stale = [w for w in writes if self._values.get(w.address) != w.expect]
        if not stale:
            return None
        first = stale[0]
        last_writer, at_tick = self._origin.get(first.address, (None, None))
        if first.part is None:
            expected, observed = first.expect, self._values.get(first.address)
        else:
            # A dict op reports only its mismatched sub-fields, shaped like the request.
            same_op = [w for w in stale if w.op_index == first.op_index]
            expected = {w.part: w.expect for w in same_op}
            observed = {w.part: self._values.get(w.address) for w in same_op}
        return {
            "status": "conflict",
            "op": first.op_index,
            "expected": expected,
            "observed": observed,
            "last_writer": last_writer,
            "at_tick": at_tick,
        }

    def _apply(
        self, writes: Sequence[_Write], *, writer: str, ledger: Callable[[_Write], dict[str, Any] | None]
    ) -> list[tuple[_Write, Any]]:
        """Write every field or none. Returns each write with the value it replaced."""
        saved = copy.deepcopy((self._values, self._origin, self._ledger))
        applied: list[tuple[_Write, Any]] = []
        try:
            for index, write in enumerate(writes):
                if index == self._fail_after:
                    self._fail_after = None
                    raise RuntimeError("Injected failure during write apply.")
                applied.append((write, self._values.get(write.address)))
                self._set(write.address, write.value, writer)
                self._put_ledger(write.address, ledger(write))
        except BaseException:
            self._values, self._origin, self._ledger = saved
            raise
        return applied

    def _set(self, address: Address, value: Any, writer: str) -> None:
        self._values[address] = value
        self._origin[address] = (writer, self._tick)

    def _put_ledger(self, address: Address, entry: dict[str, Any] | None) -> None:
        if entry is None:
            self._ledger.pop(address, None)
        else:
            self._ledger[address] = entry

    def _ai_entry(self, body: Mapping[str, Any], write: _Write) -> dict[str, Any]:
        return {"value": write.value, "changeset_id": body["changeset_id"], "author": body["author"], "tick": self._tick}

    def _readback(self, writes: Sequence[_Write]) -> list[dict[str, Any]]:
        return [
            {"object": w.address[0], "field": w.address[1], "value": self._values.get(w.address)} for w in writes
        ]

    def _emit_write(self, mode: str, receipt: dict[str, Any]) -> None:
        self._request_serial += 1
        self._emit("write", {"mode": mode, "request_id": f"{self._request_serial:032x}",
                             "receipt": copy.deepcopy(receipt)})

    def _emit(self, kind: str, fields: dict[str, Any]) -> None:
        if not self._event_active:
            return
        cursor = len(self._events) + 1
        data = {"cursor": cursor, "kind": kind, **copy.deepcopy(fields)}
        self._events.append({
            "cursor": cursor, "kind": kind, "data": data,
            "connection_epoch": "scripted", "colony_id": "scripted", "load_epoch": self.load_epoch,
            "game_tick": self._tick, "snapshot_sequence": 0,
        })


def _changeset(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != CHANGESET_KEYS:
        raise ToolExecutionError("invalid_changeset", f"A changeset needs exactly these keys: {sorted(CHANGESET_KEYS)}.")
    for key in ("changeset_id", "author", "entry_id", "load_epoch", "idempotency_key"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise ToolExecutionError("invalid_changeset", f"{key} must be a non-empty string.")
    return copy.deepcopy(dict(raw))


def _writes(ops: Any, object_keys: Sequence[str]) -> list[_Write]:
    if not isinstance(ops, list) or not ops:
        raise ToolExecutionError("invalid_changeset", "ops must be a non-empty list.")
    writes: list[_Write] = []
    seen: set[Address] = set()
    for index, op in enumerate(ops):
        if not isinstance(op, Mapping) or not isinstance(op.get("op"), str) or "expect" not in op or "value" not in op:
            raise ToolExecutionError("invalid_changeset", f"op {index} needs op, expect and value.")
        object_id, field = _target(op, index, object_keys)
        expect, value = op["expect"], op["value"]
        if isinstance(expect, dict) or isinstance(value, dict):
            if not (isinstance(expect, dict) and isinstance(value, dict)) or not expect or expect.keys() != value.keys():
                raise ToolExecutionError("invalid_changeset", f"op {index} needs matching dict expect and value.")
            pairs = [(name, expect[name], value[name]) for name in expect]
        else:
            pairs = [(None, expect, value)]
        for part, expected, new in pairs:
            address = (object_id, field if part is None else f"{field}/{part}")
            if address in seen:
                raise ToolExecutionError("invalid_changeset", f"op {index} writes a field already written: {address[1]}.")
            seen.add(address)
            writes.append(_Write(index, address, expected, new, part))
    return writes


def _target(op: Mapping[str, Any], index: int, object_keys: Sequence[str]) -> tuple[str, str]:
    """Object is the single object-key entry; the remaining target keys qualify the field name."""
    keys = [key for key in object_keys if key in op]
    if len(keys) != 1 or not isinstance(op[keys[0]], str):
        raise ToolExecutionError(
            "invalid_changeset", f"op {index} needs exactly one string target among {tuple(object_keys)}."
        )
    qualifiers = [f"{key}={op[key]}" for key in sorted(op) if key not in CONTROL_KEYS and key != keys[0]]
    return op[keys[0]], "/".join([op["op"], *qualifiers])


def _fingerprint(body: Mapping[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, default=str)
