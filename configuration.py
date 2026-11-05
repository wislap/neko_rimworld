"""Plugin settings: schema, normalization, persistence and save receipts.

Saving never occupies the UI request channel.
"""

from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .agent.model.model_client import AgentConfigurationError, AgentModelSettings, build_agent_model_settings
from .game.bridge import BridgeServer
from .game.state import DiagnosticRing, utc_now
from .workspace.budget import BudgetSettings

DEFAULT_PORT = BridgeServer.DEFAULT_PORT
DEFAULT_DIAGNOSTIC_CAPACITY = 200
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 128
AGENT_BUDGET_FIELDS = (
    "connect_timeout_seconds",
    "first_response_timeout_seconds",
    "stream_idle_timeout_seconds",
    "request_timeout_seconds",
    "run_budget_seconds",
    "max_model_calls",
    "max_input_tokens",
    "finalization_reserve_seconds",
    "max_output_tokens",
)
AUTOPLAY_OPTIONAL_BUDGET_FIELDS = frozenset({"max_cost", "input_price", "output_price", "cache_read_price"})
# Fields each panel save kind may submit besides its operation identity.
SETTINGS_SAVE_FIELDS = {
    "bridge": frozenset({"port", "pairing_token"}),
    "agent": frozenset({"base_url", "model", "api_key", "budgets", "image_input_enabled", "image_input_tokens_per_image"}),
    "autoplay": frozenset({"budgets"}),
}


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    port: int = DEFAULT_PORT
    pairing_token: str = ""
    diagnostic_log_capacity: int = DEFAULT_DIAGNOSTIC_CAPACITY

    @property
    def token_configured(self) -> bool:
        try:
            length = len(self.pairing_token.encode("utf-16-le", "strict")) // 2
        except UnicodeEncodeError:
            return False
        return MIN_TOKEN_LENGTH <= length <= MAX_TOKEN_LENGTH


def parse_bridge_settings(config: object) -> tuple[BridgeSettings, list[str]]:
    warnings: list[str] = []
    root = config if isinstance(config, Mapping) else {}
    raw = root.get("neko_rimworld")
    section = raw if isinstance(raw, Mapping) else {}

    port_value = section.get("port", DEFAULT_PORT)
    if isinstance(port_value, bool) or not isinstance(port_value, int) or not 1 <= port_value <= 65_535:
        warnings.append(f"Invalid port; using {DEFAULT_PORT}.")
        port = DEFAULT_PORT
    else:
        port = port_value

    token_value = section.get("pairing_token", "")
    token = token_value.strip() if isinstance(token_value, str) else ""
    token_candidate = BridgeSettings(pairing_token=token)
    if token and not token_candidate.token_configured:
        warnings.append("Pairing token must contain 32-128 characters; listener was not started.")
        token = ""

    capacity_value = section.get("diagnostic_log_capacity", DEFAULT_DIAGNOSTIC_CAPACITY)
    if (
        isinstance(capacity_value, bool)
        or not isinstance(capacity_value, int)
        or not 20 <= capacity_value <= DiagnosticRing.MAX_EVENTS
    ):
        warnings.append(f"Invalid diagnostic_log_capacity; using {DEFAULT_DIAGNOSTIC_CAPACITY}.")
        capacity = DEFAULT_DIAGNOSTIC_CAPACITY
    else:
        capacity = capacity_value

    return BridgeSettings(port, token, capacity), warnings


def parse_autoplay_settings(config: object) -> dict[str, Any]:
    """The stored autoplay section shown in the panel; an invalid section reads as empty."""
    root = config.get("neko_rimworld", {}) if isinstance(config, Mapping) else {}
    section = root.get("autoplay", {}) if isinstance(root, Mapping) else {}
    return dict(section) if isinstance(section, Mapping) else {}


def autoplay_budget_settings(section: Mapping[str, Any]) -> BudgetSettings:
    """Build budget settings from a stored section; stored "" means an unset optional field."""
    return BudgetSettings(**{
        name: None if name in AUTOPLAY_OPTIONAL_BUDGET_FIELDS and value == "" else value
        for name, value in section.items() if name in BudgetSettings.__dataclass_fields__})


async def save_autoplay_budgets(config, budgets: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist budget fields over the stored section; returns the stored section."""
    if set(budgets) - set(BudgetSettings.__dataclass_fields__):
        raise ValueError("Unsupported budget field")
    current = await config.dump(timeout=5.0)
    root = current.get("neko_rimworld", {}) if isinstance(current, Mapping) else {}
    section = root.get("autoplay", {}) if isinstance(root, Mapping) else {}
    if not isinstance(section, Mapping):
        raise ValueError("Existing autoplay settings are invalid")
    encoded = {name: "" if value is None and name in AUTOPLAY_OPTIONAL_BUDGET_FIELDS else value
               for name, value in budgets.items()}
    merged = {**section, **encoded}
    autoplay_budget_settings(merged)
    await persist_settings(config, {"neko_rimworld": {"autoplay": merged}})
    return dict(merged)


def updated_agent_settings(
    current: AgentModelSettings,
    *,
    base_url: object,
    model: object,
    api_key: object,
    budgets: object,
    image_input_enabled: bool | None,
    image_input_tokens_per_image: int | None,
    extra: Mapping[str, object],
) -> AgentModelSettings:
    """Apply one panel submission over the current settings; an empty key keeps the stored key."""
    if "preferred_cycles" in extra or (isinstance(budgets, dict) and "preferred_cycles" in budgets):
        raise AgentConfigurationError("preferred_cycles is retired; remove it from the agent configuration.")
    if not isinstance(base_url, str) or not isinstance(model, str) or not isinstance(api_key, str):
        raise AgentConfigurationError("Agent settings must be text values.")
    if budgets is not None and (not isinstance(budgets, dict) or set(budgets) - set(AGENT_BUDGET_FIELDS)):
        raise AgentConfigurationError("Unsupported budget fields.")
    selected = {key: getattr(current, key) for key in AGENT_BUDGET_FIELDS}
    selected.update(budgets or {})
    return build_agent_model_settings(
        base_url=base_url,
        model=model,
        api_key=api_key.strip() or current.api_key,
        request_timeout_seconds=selected["request_timeout_seconds"],
        run_budget_seconds=selected["run_budget_seconds"],
        max_model_calls=selected["max_model_calls"],
        max_input_tokens=selected["max_input_tokens"],
        max_output_tokens=selected["max_output_tokens"],
        temperature=current.temperature,
        connect_timeout_seconds=selected["connect_timeout_seconds"],
        first_response_timeout_seconds=selected["first_response_timeout_seconds"],
        stream_idle_timeout_seconds=selected["stream_idle_timeout_seconds"],
        finalization_reserve_seconds=selected["finalization_reserve_seconds"],
        stream_include_usage=current.stream_include_usage,
        image_input_enabled=current.image_input_enabled if image_input_enabled is None else image_input_enabled,
        image_input_tokens_per_image=(current.image_input_tokens_per_image if image_input_tokens_per_image is None
                                      else image_input_tokens_per_image),
    )


def agent_settings_document(settings: AgentModelSettings) -> dict[str, Any]:
    """The persisted ``neko_rimworld.agent`` section for validated settings."""
    return {
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key": settings.api_key,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "run_budget_seconds": settings.run_budget_seconds,
        "max_model_calls": settings.max_model_calls,
        "max_input_tokens": settings.max_input_tokens,
        "max_output_tokens": settings.max_output_tokens,
        "temperature": settings.temperature,
        "connect_timeout_seconds": settings.connect_timeout_seconds,
        "first_response_timeout_seconds": settings.first_response_timeout_seconds,
        "stream_idle_timeout_seconds": settings.stream_idle_timeout_seconds,
        "finalization_reserve_seconds": settings.finalization_reserve_seconds,
        "stream_include_usage": settings.stream_include_usage,
        "image_input_enabled": settings.image_input_enabled,
        "image_input_tokens_per_image": settings.image_input_tokens_per_image,
    }


class SettingsSaveOperations:
    """One settings write at a time; receipts contain no submitted values."""

    def __init__(self):
        self.receipts = OrderedDict()
        self.task = None

    def start(self, ident, kind, execute):
        if ident in self.receipts:
            if self.receipts[ident]["kind"] != kind:
                raise ValueError("operation_conflict")
            return self.read(ident)
        if self.task is not None and not self.task.done():
            raise ValueError("settings_busy")
        while len(self.receipts) >= 20:
            self.receipts.popitem(last=False)
        self.receipts[ident] = {
            "operation_id": ident, "kind": kind, "state": "saving",
            "persisted": None, "applied": None, "started_at": utc_now(),
        }
        self.task = asyncio.create_task(self._run(ident, execute), name="rimworld-settings-save")
        return self.read(ident)

    async def _run(self, ident, execute):
        receipt = self.receipts[ident]
        try:
            result = await execute()
            if result.is_ok():
                receipt.update(state="saved", persisted=True, applied=True)
            else:
                code = result.error.code
                uncertain = code in {"configuration_update_failed", "configuration_status_unknown"}
                receipt.update(state="unconfirmed" if uncertain else "failed",
                               error_code=code, persisted=None if uncertain else False,
                               applied=None if uncertain else False)
        except asyncio.CancelledError:
            receipt.update(state="unconfirmed", error_code="save_interrupted")
            raise
        except Exception:
            # Never retain exceptions: a transport may echo submitted credentials.
            receipt.update(state="unconfirmed", error_code="save_unconfirmed")
        finally:
            receipt["finished_at"] = utc_now()

    def read(self, ident):
        if ident not in self.receipts:
            raise ValueError("settings_receipt_unavailable")
        return copy.deepcopy(self.receipts[ident])

    def snapshot(self):
        return copy.deepcopy(list(self.receipts.values()))

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def persist_settings(config, patch):
    """Accept a write acknowledgement or an authoritative matching readback.

    dump() may be an optimistic process cache after a timeout. The SDK's explicit
    effective-config read bypasses that cache and respects the active profile.
    Neither submitted values nor their digests leave this call.
    """
    try:
        await config.update(patch, timeout=5.0)
    except Exception:
        ctx = getattr(config, "ctx", None)
        readback = getattr(ctx, "get_own_effective_config", None)
        if readback is None:
            raise
        try:
            from plugin.sdk.shared.core.config import unwrap_config_payload

            response = await asyncio.wait_for(readback(timeout=3.0), 3.5)
            if not isinstance(response, dict) or response.get("success") is False:
                raise ValueError("configuration_read_not_confirmed")
            actual = unwrap_config_payload(response)
            if not _contains(actual, patch):
                raise ValueError("configuration_not_confirmed")
        except Exception:
            raise RuntimeError("Configuration persistence is unconfirmed") from None


def _contains(actual, expected):
    return isinstance(actual, dict) and all(
        key in actual and (_contains(actual[key], value) if isinstance(value, dict)
                           else type(actual[key]) is type(value) and actual[key] == value)
        for key, value in expected.items()
    )
