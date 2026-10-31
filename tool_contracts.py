"""Tool capabilities and negotiated game setup, independent of dispatch policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .game.observation import Observation

MAX_TOOL_RESULT_CHARS = 131_072


def binding_is_current(check: Callable[[], bool] | None) -> bool:
    """Optional run authority must be explicitly true; uncertainty fails closed."""
    if check is None:
        return True
    try:
        return check() is True
    except Exception:  # noqa: BLE001 - unavailable authority must not admit an operation
        return False


class ToolExecutionError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, recoverable: bool = True, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.details = details or {}


# A write may already have taken effect; the owning run must stop after recording it.
UNKNOWN_WRITE_OUTCOME_CODES = frozenset({"undo_outcome_unknown", "write_receipt_lost"})


class ReceiptLost(RuntimeError):
    """A write may have been applied but its reply never arrived; retry with the same idempotency key."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__("Write receipt lost; retry the same changeset with its idempotency key.")
        self.idempotency_key = idempotency_key


ToolHandler = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any] | Observation]]


@runtime_checkable
class GameWritePort(Protocol):
    """Write and event methods of the game port. Reads stay on ToolHandler."""

    async def dry_run(self, changeset: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def commit(self, changeset: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def undo(self, token: str) -> Mapping[str, Any]: ...

    async def events(self, since: int = 0) -> list[Mapping[str, Any]]:
        """Events with cursor greater than since, oldest first. Cursor is scoped to the connection,
        colony and load epoch. push_policy enables the stream; an empty definition list enables
        events without pause reflection. The caller keeps the cursor; reading does not consume it.
        A change of binding leaves the old stream's tail unknown."""
        ...

    async def push_policy(self, policy: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def set_paused(self, paused: bool, *, role: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    terminal: bool = False
    exclusive: bool = False
    max_result_chars: int = MAX_TOOL_RESULT_CHARS

    def schema(self) -> dict[str, Any]:
        description = self.description
        if self.exclusive or self.terminal:
            description += " Call this tool alone in a separate tool batch."
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolSetup:
    tools: tuple[ToolSpec, ...]
    directory: dict[str, Any] | None = None
    #: Ready immutable Def-knowledge view for this consultation, attached by
    #: compose_wiki_tools. Consumers (wiki tool, Def-name recovery) share one
    #: frozen snapshot; None keeps every lookup on the native exact path.
    knowledge_view: Any = None
    #: Internal preparation source, deliberately absent from the model tool set.
    situation_provider: Any = None
    #: Trusted origin of observations, set by the tool provider, never by game data.
    observation_source: str = "native"
    write_catalog: dict[str, Any] | None = None
    write_port: GameWritePort | None = None
    write_identity: Callable[[], tuple[str, str, str] | None] | None = None
    idle_query: Any = None
    ledger_query: Any = None
    write_fields_query: Any = None

    def __post_init__(self):
        if self.observation_source not in {"native", "offline_captured_graph"}:
            raise ValueError("unsupported observation source")


PUBLIC_QUERY_VERSION = "public_query.v2"
WORLD_OBSERVATION_VERSION = "world_observation.v2.3"
