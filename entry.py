from __future__ import annotations

import asyncio
import copy
import json
import secrets
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    ui,
)

from .agent.delivery.handoff import attach_agent_handoff, count_handoff_tokens
from .agent.model.model_client import (
    MAX_RUN_BUDGET_SECONDS,
    AgentConfigurationError,
    AgentModelSettings,
    build_agent_model_settings,
    parse_agent_model_settings,
)
from .agent.reference.mechanism_tools import compose_implementation_knowledge
from .agent.reference.wiki_tools import compose_wiki_tools, knowledge_directory
from .agent.runtime.audit import AgentAuditRegistry
from .agent.runtime.loop import AgentRuntime
from .configuration import SettingsSaveOperations, persist_settings
from .game.bridge import (
    BridgeBusyError,
    BridgeError,
    BridgeNotConnected,
    BridgeProtocolError,
    BridgeQueryTimeout,
    BridgeServer,
    BridgeStartError,
    BridgeUnsupportedError,
)
from .game.observation import Observation
from .game.protocol import Envelope
from .game.query_tools import CatalogTools
from .game.state import BridgeStateStore, BridgeStatus, DiagnosticRing, json_safe_copy, utc_now
from .knowledge import KnowledgeLibrary
from .reports import ReportError, ReportStore
from .tool_contracts import UNKNOWN_WRITE_OUTCOME_CODES, ToolExecutionError, ToolSetup
from .workspace.runtime import FrontdeskService
from .workspace.session_view import anchor_seq
from .workspace.views import WorkspaceViews

PLUGIN_VERSION = "3.2.0"
DEFAULT_PORT = 48919
DEFAULT_DIAGNOSTIC_CAPACITY = 200
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 128
AUTOPLAY_OPTIONAL_BUDGET_FIELDS = frozenset({"max_cost", "input_price", "output_price", "cache_read_price"})
# Cooperative world/action reads have an eight-second game-side lifetime.
# The transport must allow that operation to return its partial result.
FRAME_READ_OPERATIONS = frozenset({
    "survey_space", "inspect_route", "inspect_state", "inspect_actions", "inspect_threats", "read_situation",
    "inspect_planet", "inspect_factions", "inspect_quests", "inspect_trade", "inspect_research",
    "evaluate_journey", "evaluate_trade",
})


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


def _parse_settings(config: object) -> tuple[BridgeSettings, list[str]]:
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


def _query_summary(response: Envelope) -> dict[str, Any]:
    payload = response.payload.to_dict()
    status = payload.get("status", "failed")
    return {
        "status": status,
        "summary": f"Game query {status}; {len(payload.get('issues', []))} issues.",
        "error_code": payload.get("error_code"),
        "response": response.to_dict(),
    }


def _agent_handoff(result: object) -> dict[str, Any]:
    """Add the single text projection consumed by N.E.K.O's Agent callback."""

    return attach_agent_handoff(result)


def _consultation_handoff(result, question):
    """Carry operational state through the host's bounded text projection."""
    handoff = _agent_handoff(result)
    chinese = any("\u4e00" <= c <= "\u9fff" for c in question)
    notices = []
    if result.get("status") != "ok":
        code = result.get("audit", {}).get("error_code")
        reasons = {
            "agent_run_budget": ("调查超时，未完整完成。", "Consultation incomplete: time limit."),
            "agent_request_budget": ("模型请求次数已达上限，调查未完整完成。", "Consultation incomplete: model request limit."),
            "world_changed": ("游戏世界已改变，需重新咨询。", "The game world changed; consult again."),
            "agent_input_budget": ("输入预算已耗尽，调查未完整完成。", "Consultation incomplete: input limit."),
        }
        message = reasons.get(
            code, ("调查未完整完成，请检查插件面板。", "Consultation incomplete; check the plugin panel.")
        )
        notices.append(message[0 if chinese else 1])
    if result.get("workspace", {}).get("history_saved") is False:
        notices.append("历史未保存。" if chinese else "History not saved.")
    if result.get("continuity", {}).get("bound") is False:
        notices.append(
            "此轮为独立咨询，未接入持续工作。" if chinese else "Standalone consultation; no ongoing workspace attached."
        )
    if notices:
        notice = " ".join(notices)
        summary = notice + " " + handoff["summary"]
        if count_handoff_tokens(summary)[0] > 200:
            summary = notice + (
                " 请在插件面板查看结果与证据。" if chinese else " Check the plugin panel for results and evidence."
            )
        handoff["summary"] = handoff["reply"] = summary
        tokens, tokenizer = count_handoff_tokens(summary)
        handoff["delivery"].update(handoff_tokens=tokens, tokenizer=tokenizer, operational_notice=True)
    return handoff


def _unresponsive_request_deadline(registry, run_id, request_limit):
    """Distinguish a silent endpoint timeout from a run-limited or active stream."""
    if not run_id or not isinstance(request_limit, (int, float)) or isinstance(request_limit, bool):
        return False
    try:
        details = registry.get_details(run_id, event_offset=0, event_limit=100_000) if registry else None
    except Exception:
        return False
    events = details.get("events", []) if isinstance(details, dict) else []
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        data = event.get("data", {})
        if event.get("event") != "model_request_failed" or data.get("error_code") != "model_request_deadline":
            continue
        diagnostics = data.get("diagnostics") or {}
        if (not isinstance(diagnostics, Mapping) or diagnostics.get("first_useful_delta_ms") is not None
                or diagnostics.get("phase") not in {"starting", "connecting", "connected", "tls", "tls_complete", "response_headers", "first_response"}):
            return False
        started = next((item for item in reversed(events[:index]) if item.get("event") == "model_request_started"), None)
        timeout = (started.get("data") or {}).get("timeout_seconds") if started else None
        return type(timeout) in (int, float) and timeout >= request_limit
    return False


@neko_plugin
class NekoRimworldPlugin(NekoPluginBase):
    """Read-only RimWorld bridge with a Hosted TSX diagnostics surface."""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._settings = BridgeSettings()
        self._agent_settings = AgentModelSettings()
        self._state_store = BridgeStateStore()
        self._bridge: BridgeServer | None = None
        self._last_dashboard: dict[str, Any] = {}
        self._settings_action_nonce = secrets.token_urlsafe(32)
        self._reconfigure_lock = asyncio.Lock()
        self._agent_audit = AgentAuditRegistry()
        self._reports = ReportStore(self.data_path("reports"))
        self._catalog_tools = CatalogTools(lambda: self._bridge, self._agent_query_tool)
        self._knowledge_directory = knowledge_directory()
        self._knowledge = KnowledgeLibrary(
            self._knowledge_directory
        )
        self._agent_runtime = AgentRuntime(
            tools_provider=self._load_agent_tools,
            audit_registry=self._agent_audit,
            logger=self.get_logger("agent"),
            report_sink=self._save_report,
        )
        self._workspace_service = None
        self._colony_scheduler = None
        self._autoplay_delivery = None
        self._autoplay_waiters = {}
        self._autoplay_fault = None
        self._autoplay_budget = None
        self._autoplay_settings = {}
        self._frontdesk_entry_locks = {}
        self._consultation_tasks = set()
        self._settings_saves = SettingsSaveOperations()

    @lifecycle(id="startup")
    async def startup(self, **_: object):
        if getattr(self._knowledge, "closed", False):
            self._knowledge = KnowledgeLibrary(
                self._knowledge_directory
            )
        self._knowledge.request_refresh()
        async with self._reconfigure_lock:
            if not await self._stop_existing_bridge():
                self._state_store.transition(
                    BridgeStatus.ERROR,
                    runtime={"phase": "stop_failed", "worker_alive": True},
                )
                self._publish_coarse_status()
                return Ok({"status": "stop_failed"})
            config_error: Exception | None = None
            try:
                config = await self.config.dump(timeout=5.0)
            except Exception as exception:
                config = {}
                config_error = exception

            settings, warnings = _parse_settings(config)
            root = config.get("neko_rimworld", {}) if isinstance(config, Mapping) else {}
            self._autoplay_settings = dict(root.get("autoplay", {})) if isinstance(root, Mapping) and isinstance(root.get("autoplay", {}), Mapping) else {}
            agent_settings, agent_warnings = parse_agent_model_settings(config)
            self._agent_settings = agent_settings
            warnings.extend(agent_warnings)
            self._state_store = BridgeStateStore(DiagnosticRing(settings.diagnostic_log_capacity))

            if config_error is not None:
                incident = self._state_store.record_incident(
                    "configuration",
                    "Plugin configuration could not be loaded; safe defaults were used.",
                    level="error",
                )
                self.logger.error(
                    "[startup] config load failed; safe defaults active incident_id={} error_type={}",
                    incident,
                    type(config_error).__name__,
                )

            for warning in warnings:
                self._state_store.diagnostics.record("warning", "configuration", warning)
                self.logger.warning("[startup] {}", warning)

            return Ok(await self._start_configured_bridge(settings, source="startup"))

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: object):
        if hasattr(self, "_settings_saves"):
            await self._settings_saves.close()
        calls = [task for task in self._consultation_tasks if task is not asyncio.current_task()]
        for task in calls:
            task.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        await self._stop_autoplay()
        await self._agent_runtime.cancel_all()
        workspace = getattr(self, "_workspace_service", None)
        if workspace is not None:
            await workspace.close()
        self._knowledge.close()
        async with self._reconfigure_lock:
            stopped = await self._stop_existing_bridge()
            self._state_store.transition(
                BridgeStatus.STOPPED if stopped else BridgeStatus.ERROR,
                runtime={"phase": "stopped" if stopped else "stop_failed", "worker_alive": not stopped},
                endpoint=None,
                connection=None,
                session=None,
            )
            self._state_store.diagnostics.record(
                "info" if stopped else "error",
                "lifecycle",
                "N.E.K.O RimWorld plugin stopped." if stopped else "Bridge worker did not stop cleanly.",
            )
            self._publish_coarse_status()
            return Ok({"status": "stopped" if stopped else "stop_failed"})

    @ui.context(id="dashboard", title="RimWorld Bridge")
    async def dashboard_ui_context(self, **_: object) -> dict[str, Any]:
        try:
            snapshot = self._state_store.snapshot()
            runtime = snapshot.get("runtime")
            runtime_state = dict(runtime) if isinstance(runtime, Mapping) else {}
            runtime_state["worker_alive"] = bool(self._bridge and self._bridge.running)
            snapshot["runtime"] = runtime_state
            snapshot["configuration"] = {
                "token_configured": self._settings.token_configured,
                "agent_configured": self._agent_settings.configured,
                "agent_api_key_configured": self._agent_settings.api_key_configured,
                "agent_base_url": self._agent_settings.base_url,
                "agent_model": self._agent_settings.model,
                "agent_budgets": {
                    key: getattr(self._agent_settings, key)
                    for key in (
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
                },
            }
            agent_snapshot = self._agent_runtime.snapshot(include_latest_details=False)
            # This process-local nonce makes the credential action callable only by
            # this Hosted UI surface. It is intentionally not displayed or logged.
            snapshot["action_nonce"] = self._settings_action_nonce
            snapshot["settings_saves"] = self._settings_save_operations().snapshot()
            safe = json_safe_copy(snapshot)
            # Keep the lightweight agent summaries under the audit sanitizer's contract.
            # Full run data is read on demand via get_agent_run_details.
            safe["agent"] = agent_snapshot
            safe["consultations_in_progress"] = len(self._consultation_tasks)
            budget = getattr(self, "_autoplay_budget", None)
            safe["autoplay"] = {"enabled": getattr(self, "_colony_scheduler", None) is not None,
                                "fault": getattr(self, "_autoplay_fault", None),
                                "budget": await budget.status() if budget else None,
                                "settings": getattr(self, "_autoplay_settings", {})}
            current = getattr(self._bridge, "session", None)
            if current is not None:
                work = getattr(self, "_workspace_service", None)
                if work is not None and not work.closed:
                    activation = await work.call("latest_event", current.colony_id, "autoplay.activation")
                else:
                    views = WorkspaceViews(self.data_path("workspaces") / "events.sqlite")
                    activation = await asyncio.to_thread(views.latest_activation, current.colony_id)
                safe["autoplay"]["activation"] = activation
            if work := getattr(self, "_workspace_service", None):
                if current is not None:
                    events = await work.call("action_events", current.colony_id)
                    actions = []
                    for event in events:
                        write = event["payload"].get("write") if event["kind"] in {"planner.progress", "autoplay.undo"} else None
                        if isinstance(write, dict) and write.get("stage") == "result" and write.get("mode") in {"commit", "undo"}:
                            receipt = write.get("receipt") or {}
                            actions.append({"seq": event["seq"], "epoch": event["load_epoch"],
                                            "tick": event["game_tick"], "mode": write["mode"],
                                            "status": receipt.get("status"), "changeset_id": receipt.get("changeset_id"),
                                            "undo_available": event["load_epoch"] == current.load_epoch and bool(receipt.get("undo_token"))})
                    safe["autoplay"].update(board=await work.call("board", current.colony_id),
                                            takeovers=await work.call("takeovers", current.colony_id), actions=actions[-50:])
            work = self._workspace_service
            safe["workspaces"] = {
                "revision": work.revision if work is not None else 0,
                "active_count": len(work.active) if work is not None else 0,
            }
            self._last_dashboard = copy.deepcopy(safe)
            return safe
        except Exception:
            incident = self._state_store.record_incident(
                "ui_context",
                "The diagnostics snapshot could not be rendered.",
                level="error",
            )
            self.logger.exception("[ui] dashboard snapshot failed incident_id={}", incident)
            fallback = copy.deepcopy(self._last_dashboard)
            fallback.setdefault("status", BridgeStatus.ERROR)
            fallback["panel_error"] = {
                "incident_id": incident,
                "message": "The diagnostics snapshot failed; showing the last safe state.",
            }
            fallback["action_nonce"] = self._settings_action_nonce
            fallback["updated_at"] = utc_now()
            return json_safe_copy(fallback)

    @ui.action(id="workspace_view", label="Read colony work", refresh_context=False)
    @plugin_entry(
        id="workspace_view",
        name="Read persistent colony work",
        description="Hosted UI read-only access to saved work, exchanges, evidence and audit.",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "work", "turn", "observation", "audit"]},
                "workspace_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "observation_id": {"type": "string"},
                "search": {"type": "string", "maxLength": 256},
                "colony_only": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        timeout=8.0,
        metadata={"agent_auto": False},
    )
    async def workspace_view(
        self, action="list", workspace_id="", turn_id="", observation_id="", offset=0, limit=20, search="", colony_only=False, **_
    ):
        try:
            game = getattr(self._bridge, "session", None)
            view = await asyncio.to_thread(
                WorkspaceViews(self.data_path("workspaces") / "events.sqlite").read,
                action,
                workspace_id=workspace_id,
                turn_id=turn_id,
                observation_id=observation_id,
                offset=offset,
                limit=limit,
                current_colony_id=game.colony_id if game is not None else None,
                search=search,
                colony_only=colony_only,
            )
            service = self._workspace_service
            active = self._agent_runtime.snapshot(include_latest_details=False)["active_runs"]

            def execution(wid):
                running = next((r for r in active if r.get("workspace_id") == wid), None)
                return running or (service.active.get(wid) if service is not None else None)

            if action == "list":
                game = getattr(self._bridge, "session", None)
                for row in view["workspaces"]:
                    row["active"] = execution(row["workspace_id"])
                    row["current_colony"] = game is not None and row["colony_id"] == game.colony_id
            else:
                view["active"] = execution(workspace_id)
            if action == "turn":
                view["history_saved"] = view["result"] is not None and view["status"] not in {"pending", "running"}
                report_id = (view.get("result") or {}).get("report", {}).get("report_id")
                report_id = report_id or ((view.get("handoff") or {}).get("report") or {}).get("report_id")
                if report_id:
                    try:
                        record = await asyncio.to_thread(self._reports.read, report_id)
                        if record.get("workspace_id") != workspace_id:
                            raise ReportError("report_not_found", "The report belongs to another work")
                        view["report"] = {**ReportStore.metadata(record), "full_text": record["full_text"]}
                    except (ReportError, OSError):
                        view["report_unavailable"] = True
            return Ok(view)
        except (ValueError, OSError, sqlite3.Error) as error:
            return Err(SdkError(str(error), code="workspace_view_unavailable"))

    @ui.action(id="get_agent_run_details", label="Read agent run details", refresh_context=False)
    @plugin_entry(
        id="get_agent_run_details",
        name="Read retained agent run",
        description="Hosted UI access to one retained run and a bounded page of audit events.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "event_offset": {"type": "integer", "minimum": 0, "maximum": 512},
                "event_limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        timeout=5.0,
        metadata={"agent_auto": False},
    )
    async def get_agent_run_details(self, run_id: str = "", event_offset: int = 0, event_limit: int = 50, **_: object):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            return Err(SdkError("A valid run ID is required.", code="invalid_run_id"))
        if (
            type(event_offset) is not int
            or not 0 <= event_offset <= 512
            or type(event_limit) is not int
            or not 1 <= event_limit <= 100
        ):
            return Err(SdkError("Invalid event page.", code="invalid_event_page"))
        details = self._agent_audit.get_details(run_id, event_offset, event_limit)
        if details is None:
            return Err(
                SdkError("This run is no longer retained (history eviction or plugin reload).", code="run_not_retained")
            )
        return Ok(details)

    def _settings_save_operations(self):
        if not hasattr(self, "_settings_saves"):
            self._settings_saves = SettingsSaveOperations()
        return self._settings_saves

    @ui.action(id="begin_settings_save", label="Save settings", refresh_context=False)
    @plugin_entry(
        id="begin_settings_save", name="Save panel settings", timeout=3.0,
        description="Start one settings save and return a receipt without waiting for persistence.",
        metadata={"agent_auto": False},
        input_schema={"type": "object", "additionalProperties": False,
            "required": ["operation_id", "kind", "ui_nonce"], "properties": {
                "operation_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                "kind": {"type": "string", "enum": ["bridge", "agent", "autoplay"]},
                "ui_nonce": {"type": "string", "writeOnly": True},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "pairing_token": {"type": "string", "writeOnly": True, "maxLength": 128},
                "base_url": {"type": "string", "maxLength": 2048},
                "model": {"type": "string", "maxLength": 256},
                "api_key": {"type": "string", "writeOnly": True, "maxLength": 4096},
                "budgets": {"type": "object"},
            }},
    )
    async def begin_settings_save(self, operation_id="", kind="", ui_nonce="", _ctx=None, **values):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(SdkError("Refresh the panel before saving.", code="ui_only"))
        if (not isinstance(operation_id, str) or len(operation_id) != 32
                or any(c not in "0123456789abcdef" for c in operation_id) or kind not in {"bridge", "agent", "autoplay"}):
            return Err(SdkError("Invalid settings operation.", code="invalid_settings_operation"))
        allowed = ({"port", "pairing_token"} if kind == "bridge" else
                   {"budgets"} if kind == "autoplay" else {"base_url", "model", "api_key", "budgets"})
        if set(values) - allowed:
            return Err(SdkError("Unsupported settings fields.", code="invalid_settings_operation"))
        # Freeze validated invocation input; the existing business save owns all
        # validation, rollback and activation. No UI request waits on that work.
        arguments = copy.deepcopy(values)
        async def execute():
            save = (self.save_bridge_settings if kind == "bridge" else
                    self._save_autoplay_settings if kind == "autoplay" else self.save_agent_settings)
            return await save(ui_nonce=ui_nonce, **arguments)
        try:
            return Ok(self._settings_save_operations().start(operation_id, kind, execute))
        except ValueError as error:
            return Err(SdkError("A settings save is already pending or unavailable.", code=str(error)))

    async def _save_autoplay_settings(self, ui_nonce="", budgets=None):
        from .workspace.budget import BudgetSettings

        if not self._valid_settings_ui_nonce(ui_nonce) or not isinstance(budgets, dict):
            return Err(SdkError("Invalid settings.", code="invalid_autoplay_settings"))
        try:
            if set(budgets) - set(BudgetSettings.__dataclass_fields__):
                raise ValueError("Unsupported budget field")
            current = await self.config.dump(timeout=5.0)
            root = current.get("neko_rimworld", {}) if isinstance(current, Mapping) else {}
            section = root.get("autoplay", {}) if isinstance(root, Mapping) else {}
            if not isinstance(section, Mapping):
                raise ValueError("Existing autoplay settings are invalid")
            encoded = {name: "" if value is None and name in AUTOPLAY_OPTIONAL_BUDGET_FIELDS else value
                       for name, value in budgets.items()}
            merged = {**section, **encoded}
            normalized = {name: None if name in AUTOPLAY_OPTIONAL_BUDGET_FIELDS and value == "" else value
                          for name, value in merged.items() if name in BudgetSettings.__dataclass_fields__}
            BudgetSettings(**normalized)
            await persist_settings(self.config, {"neko_rimworld": {"autoplay": merged}})
            self._autoplay_settings = dict(merged)
            return Ok({"summary": "Autoplay budget saved; applies on next enable."})
        except (ValueError, TypeError, OSError, RuntimeError) as error:
            return Err(SdkError(str(error), code="invalid_autoplay_settings"))

    @ui.action(id="get_settings_save", label="Check settings save", refresh_context=False)
    @plugin_entry(
        id="get_settings_save", name="Read settings save receipt", timeout=3.0,
        description="Read the status of a panel settings save; never repeats a write.",
        metadata={"agent_auto": False},
        input_schema={"type": "object", "additionalProperties": False,
            "required": ["operation_id"], "properties": {
                "operation_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}},
    )
    async def get_settings_save(self, operation_id="", **_):
        try:
            return Ok(self._settings_save_operations().read(operation_id))
        except (ValueError, TypeError):
            return Err(SdkError("This save receipt is no longer available.", code="settings_receipt_unavailable"))

    @ui.action(
        id="save_bridge_settings",
        label="Save bridge settings",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_bridge_settings",
        name="Save RimWorld bridge settings",
        description="Save local bridge credentials and immediately restart the loopback listener.",
        input_schema={
            "type": "object",
            "properties": {
                "port": {"type": "integer", "minimum": 1, "maximum": 65_535},
                "pairing_token": {
                    "type": "string",
                    "writeOnly": True,
                    "maxLength": MAX_TOKEN_LENGTH,
                    "description": "Leave empty to retain the currently configured token.",
                },
                "ui_nonce": {
                    "type": "string",
                    "writeOnly": True,
                    "minLength": 1,
                },
            },
            "required": ["port", "pairing_token", "ui_nonce"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=15.0,
        metadata={"agent_auto": False},
    )
    async def save_bridge_settings(
        self,
        port: int = DEFAULT_PORT,
        pairing_token: str = "",
        ui_nonce: str = "",
        **_: object,
    ):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(
                SdkError(
                    "Open or refresh this plugin's Hosted UI before changing bridge credentials.",
                    code="ui_only",
                )
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            return Err(SdkError("Port must be an integer from 1 to 65535.", code="invalid_port"))
        if not isinstance(pairing_token, str):
            return Err(SdkError("Pairing token must be text.", code="invalid_pairing_token"))

        supplied_token = pairing_token.strip()
        if supplied_token and not BridgeSettings(pairing_token=supplied_token).token_configured:
            return Err(
                SdkError(
                    "Pairing token must contain 32-128 UTF-16 code units.",
                    code="invalid_pairing_token",
                )
            )

        async with self._reconfigure_lock:
            try:
                current_config = await self.config.dump(timeout=5.0)
            except Exception as exception:
                incident = self._record_unexpected(
                    "settings_config_read",
                    "Bridge settings could not be read.",
                    exception,
                )
                return Err(
                    SdkError(
                        "Bridge settings could not be read.",
                        code="configuration_read_failed",
                        incident_id=incident,
                    )
                )

            current_settings, _warnings = _parse_settings(current_config)
            effective_token = supplied_token or current_settings.pairing_token
            if not BridgeSettings(pairing_token=effective_token).token_configured:
                return Err(
                    SdkError(
                        "Enter a 32-128 character pairing token before saving.",
                        code="pairing_token_required",
                    )
                )

            next_settings = BridgeSettings(
                port=port,
                pairing_token=effective_token,
                diagnostic_log_capacity=current_settings.diagnostic_log_capacity,
            )
            bridge = self._bridge
            if self._settings == next_settings and bridge is not None and bridge.running:
                active_port = bridge.address[1]
                self._settings_action_nonce = secrets.token_urlsafe(32)
                return Ok(
                    {
                        "status": "listening",
                        "summary": f"RimWorld bridge is already listening on 127.0.0.1:{active_port}.",
                        "port": active_port,
                        "token_configured": True,
                    }
                )
            if not await self._stop_existing_bridge():
                self._state_store.transition(
                    BridgeStatus.ERROR,
                    runtime={"phase": "stop_failed", "worker_alive": True},
                )
                self._state_store.diagnostics.record(
                    "error",
                    "configuration",
                    "New settings were not saved because the existing bridge worker did not stop.",
                )
                self._publish_coarse_status()
                return Err(
                    SdkError(
                        "New settings were not saved; the existing listener could not be stopped and was retained.",
                        code="bridge_stop_failed",
                        new_settings_saved=False,
                        previous_listener_retained=True,
                    )
                )

            activation = await self._start_configured_bridge(next_settings, source="settings")
            if activation["status"] != "listening":
                restored, restoration = await self._restore_previous_settings(
                    current_settings,
                    candidate_status=str(activation["status"]),
                )
                error_code = "bridge_stop_failed" if activation["status"] == "stop_failed" else "bridge_start_error"
                restoration_text = (
                    "The previous bridge state was restored."
                    if restored
                    else "The previous bridge state could not be restored."
                )
                return Err(
                    SdkError(
                        f"New settings were not saved because the candidate listener failed to start. {restoration_text}",
                        code=error_code,
                        incident_id=activation.get("incident_id"),
                        new_settings_saved=False,
                        previous_state_restored=restored,
                        previous_state=restoration.get("status"),
                        restore_incident_id=restoration.get("incident_id"),
                    )
                )

            try:
                await persist_settings(self.config,
                    {
                        "neko_rimworld": {
                            "port": next_settings.port,
                            "pairing_token": next_settings.pairing_token,
                        }
                    },
                )
            except Exception as exception:
                incident = self._record_unexpected(
                    "settings_config_write",
                    "Bridge settings could not be saved.",
                    exception,
                )
                if not await self._stop_existing_bridge():
                    self._state_store.transition(
                        BridgeStatus.ERROR,
                        runtime={"phase": "rollback_stop_failed", "worker_alive": True},
                    )
                    self._state_store.diagnostics.record(
                        "error",
                        "configuration",
                        "New settings were not saved and the candidate listener could not be stopped.",
                    )
                    self._publish_coarse_status()
                    return Err(
                        SdkError(
                            "New settings were not saved; the candidate listener could not be stopped, so the previous listener was not restored.",
                            code="configuration_update_failed",
                            incident_id=incident,
                            new_settings_saved=False,
                            previous_state_restored=False,
                            candidate_listener_retained=True,
                        )
                    )

                restored, restoration = await self._restore_previous_settings(
                    current_settings,
                    candidate_status="stopped",
                )
                restoration_text = (
                    "The previous bridge state was restored."
                    if restored
                    else "The previous bridge state could not be restored."
                )
                return Err(
                    SdkError(
                        f"New settings were not saved because configuration persistence failed. {restoration_text}",
                        code="configuration_update_failed",
                        incident_id=incident,
                        new_settings_saved=False,
                        previous_state_restored=restored,
                        previous_state=restoration.get("status"),
                        restore_incident_id=restoration.get("incident_id"),
                    )
                )

            self._settings_action_nonce = secrets.token_urlsafe(32)
            return Ok(
                {
                    "status": "listening",
                    "summary": f"RimWorld bridge is listening on 127.0.0.1:{activation['port']}.",
                    "port": activation["port"],
                    "token_configured": True,
                }
            )

    @ui.action(
        id="save_agent_settings",
        label="Save agent settings",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_agent_settings",
        name="Save RimWorld research agent settings",
        description="Hosted UI configuration action for the plugin-owned model client.",
        input_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "model": {"type": "string", "minLength": 1, "maxLength": 256},
                "api_key": {
                    "type": "string",
                    "writeOnly": True,
                    "maxLength": 4096,
                    "description": "Leave empty to retain the currently configured API key.",
                },
                "ui_nonce": {"type": "string", "writeOnly": True, "minLength": 1},
                "budgets": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "connect_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 30},
                        "first_response_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                        "stream_idle_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                        "request_timeout_seconds": {"type": "number", "minimum": 3, "maximum": 60},
                        "run_budget_seconds": {"type": "number", "minimum": 10, "maximum": 86400},
                        "max_model_calls": {"type": "integer", "minimum": 1, "maximum": 10000},
                        "max_input_tokens": {"type": "integer", "minimum": 1, "maximum": 1000000000},
                        "finalization_reserve_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                        "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 131072},
                    },
                },
            },
            "required": ["base_url", "model", "api_key", "ui_nonce"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=15.0,
        metadata={"agent_auto": False},
    )
    async def save_agent_settings(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        ui_nonce: str = "",
        budgets: dict[str, Any] | None = None,
        **_: object,
    ):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(
                SdkError(
                    "Open or refresh this plugin's Hosted UI before changing agent credentials.",
                    code="ui_only",
                )
            )
        if "preferred_cycles" in _ or (isinstance(budgets, dict) and "preferred_cycles" in budgets):
            return Err(SdkError("preferred_cycles is retired; remove it from the agent configuration.", code="invalid_agent_settings"))
        if not isinstance(base_url, str) or not isinstance(model, str) or not isinstance(api_key, str):
            return Err(SdkError("Agent settings must be text values.", code="invalid_agent_settings"))
        allowed_budgets = {
            "connect_timeout_seconds",
            "first_response_timeout_seconds",
            "stream_idle_timeout_seconds",
            "request_timeout_seconds",
            "run_budget_seconds",
            "max_model_calls",
            "max_input_tokens",
            "finalization_reserve_seconds",
            "max_output_tokens",
        }
        if budgets is not None and (not isinstance(budgets, dict) or set(budgets) - allowed_budgets):
            return Err(SdkError("Unsupported budget fields.", code="invalid_agent_settings"))
        selected_budgets = {key: getattr(self._agent_settings, key) for key in allowed_budgets}
        selected_budgets.update(budgets or {})
        effective_key = api_key.strip() or self._agent_settings.api_key
        try:
            next_settings = build_agent_model_settings(
                base_url=base_url,
                model=model,
                api_key=effective_key,
                request_timeout_seconds=selected_budgets["request_timeout_seconds"],
                run_budget_seconds=selected_budgets["run_budget_seconds"],
                max_model_calls=selected_budgets["max_model_calls"],
                max_input_tokens=selected_budgets["max_input_tokens"],
                max_output_tokens=selected_budgets["max_output_tokens"],
                temperature=self._agent_settings.temperature,
                connect_timeout_seconds=selected_budgets["connect_timeout_seconds"],
                first_response_timeout_seconds=selected_budgets["first_response_timeout_seconds"],
                stream_idle_timeout_seconds=selected_budgets["stream_idle_timeout_seconds"],
                finalization_reserve_seconds=selected_budgets["finalization_reserve_seconds"],
                stream_include_usage=self._agent_settings.stream_include_usage,
            )
        except AgentConfigurationError as exception:
            return Err(SdkError(str(exception), code="invalid_agent_settings"))

        try:
            await persist_settings(self.config,
                {
                    "neko_rimworld": {
                        "agent": {
                            "base_url": next_settings.base_url,
                            "model": next_settings.model,
                            "api_key": next_settings.api_key,
                            "request_timeout_seconds": next_settings.request_timeout_seconds,
                            "run_budget_seconds": next_settings.run_budget_seconds,
                            "max_model_calls": next_settings.max_model_calls,
                            "max_input_tokens": next_settings.max_input_tokens,
                            "max_output_tokens": next_settings.max_output_tokens,
                            "temperature": next_settings.temperature,
                            "connect_timeout_seconds": next_settings.connect_timeout_seconds,
                            "first_response_timeout_seconds": next_settings.first_response_timeout_seconds,
                            "stream_idle_timeout_seconds": next_settings.stream_idle_timeout_seconds,
                            "finalization_reserve_seconds": next_settings.finalization_reserve_seconds,
                            "stream_include_usage": next_settings.stream_include_usage,
                        }
                    }
                },
            )
        except Exception as exception:
            incident = self._record_unexpected(
                "agent_settings_write",
                "Agent settings could not be saved.",
                exception,
            )
            return Err(
                SdkError(
                    "Agent settings could not be saved.",
                    code="configuration_update_failed",
                    incident_id=incident,
                )
            )

        self._agent_settings = next_settings
        self._settings_action_nonce = secrets.token_urlsafe(32)
        return Ok(
            {
                "status": "configured",
                "summary": f"RimWorld research agent configured with model '{next_settings.model}'.",
                "base_url": next_settings.base_url,
                "model": next_settings.model,
                "api_key_configured": next_settings.api_key_configured,
            }
        )

    @ui.action(
        id="rimworld_consult",
        label="Run agent test",
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="rimworld_consult",
        name="Consult RimWorld specialist",
        description=(
            "Ask the RimWorld specialist a question in natural language. It investigates the colony, "
            "continues relevant previous work, and retrieves historical reports and evidence when needed. "
            "Express changes of topic, goals or requested scope in the question."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "The user's RimWorld question, preserving their language and intent.",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=MAX_RUN_BUDGET_SECONDS + 10,
        metadata={"llm_result_fields": ["summary"]},
    )
    async def rimworld_consult(self, question: str = "", *, _ctx: object = None):
        task = asyncio.current_task()
        self._consultation_tasks.add(task)
        try:
            game = getattr(self._bridge, "session", None)
            if game is None:
                return await self._consultation(question, _ctx, game)
            locks = getattr(self, "_frontdesk_entry_locks", None)
            if locks is None:
                locks = self._frontdesk_entry_locks = {}
            async with locks.setdefault(game.colony_id, asyncio.Lock()):
                current = getattr(self._bridge, "session", None)
                if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
                    return Err(SdkError("The active colony changed while waiting; retry in the current colony.", code="world_changed"))
                return await self._consultation(question, _ctx, game)
        except ToolExecutionError as error:
            if error.code == "world_changed":
                return Err(SdkError("The active colony changed; retry in the current colony.", code="world_changed"))
            raise
        finally:
            self._consultation_tasks.discard(task)

    async def start_colony_scheduler(self, choices):
        """Explicit opt-in for the local scheduler; startup never enables it."""
        from .agent.runtime.colony_scheduler import ColonyScheduler

        if self._colony_scheduler is not None:
            raise RuntimeError("Scheduler already started")
        game = getattr(self._bridge, "session", None)
        if game is None or not self._agent_settings.configured:
            raise ValueError("A connected colony and configured model are required")
        service = self._workspace_service
        if service is None or service.closed:
            service = FrontdeskService(self.data_path("workspaces") / "events.sqlite", self._agent_runtime,
                                       game_session=lambda: getattr(self._bridge, "session", None))
            self._workspace_service = service
        def event_binding():
            bridge = self._bridge
            current = getattr(bridge, "session", None)
            return (getattr(bridge, "connection_key", None),
                    getattr(current, "colony_id", None), getattr(current, "load_epoch", None))

        setup = await self._load_agent_tools()
        required = {"colony_write.transactions.v1", "colony_write.ledger_snapshot.v1",
                    "colony_events.v1", "colony_pause.v1", "colony_pause.policy.v1"}
        if (setup.write_port is None or setup.write_identity is None or setup.write_identity() is None
                or not required <= set(setup.write_port.capabilities) or setup.write_catalog is None
                or setup.ledger_query is None or setup.write_fields_query is None):
            raise ValueError("The connected Mod does not provide all autoplay capabilities")
        from .workspace.budget import BudgetSettings, ColonyBudget

        config = await self.config.dump(timeout=5.0)
        section = config.get("neko_rimworld", {}).get("autoplay", {})
        budget_settings = BudgetSettings(**{
            k: None if k in AUTOPLAY_OPTIONAL_BUDGET_FIELDS and v == "" else v
            for k, v in section.items() if k in BudgetSettings.__dataclass_fields__})
        if (self._autoplay_budget is not None and self._autoplay_budget.game.colony_id != game.colony_id
                and self._autoplay_budget.pending):
            raise ValueError("A previous colony has an unrecorded usage receipt")
        if self._autoplay_budget is None or self._autoplay_budget.game.colony_id != game.colony_id:
            self._autoplay_budget = ColonyBudget(service, game, budget_settings)
        else:
            self._autoplay_budget.game = game
            self._autoplay_budget.settings = budget_settings
            await self._autoplay_budget.reenable()
        async def scoped_failure(role, result, bound_game=None):
            await self._autoplay_failure(role, result, bound_game, origin_scheduler=scheduler)
        scheduler = ColonyScheduler(service, self._agent_settings, event_provider=self._load_agent_tools, binding=event_binding,
                                    budget=self._autoplay_budget, failure_handler=scoped_failure)
        identity = setup.write_identity()
        current = getattr(self._bridge, "session", None)
        if (identity is None or tuple(identity[1:]) != (game.colony_id, game.load_epoch) or current is None
                or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch)
                or getattr(self._bridge, "connection_key", None) != identity[0]):
            raise ValueError("The active colony changed while enabling autoplay")
        await service.call("observe_epoch", game.colony_id, game.load_epoch, game.game_tick,
                           command_binding_check=lambda: event_binding() == tuple(identity))
        if event_binding() != tuple(identity):
            raise ValueError("The active colony changed while enabling autoplay")
        await service.call("record_autoplay_activation", game.colony_id, game.load_epoch, game.game_tick, choices,
                           command_binding_check=lambda: event_binding() == tuple(identity))
        scheduler.start()
        scheduler.resume(game)
        self._colony_scheduler = scheduler
        self._autoplay_fault = None
        self._autoplay_delivery = asyncio.create_task(self._deliver_autoplay(scheduler))
        return scheduler

    async def _consultation(self, question, _ctx, game):
        scheduler = getattr(self, "_colony_scheduler", None)
        request_id = _ctx.get("run_id") if isinstance(_ctx, Mapping) else None
        connection = getattr(self._bridge, "connection_key", None)
        if (game is None or not isinstance(question, str)
                or not isinstance(request_id, str) or not request_id
                or not isinstance(connection, str) or not connection):
            return await self._consultation_impl(question, _ctx, game)
        key = (id(scheduler), connection, game.colony_id, game.load_epoch, request_id, question)
        inflight = getattr(self, "_consultation_inflight", None)
        if inflight is None:
            inflight = self._consultation_inflight = {}
        task = inflight.get(key)
        if task is None:
            async def run_bound():
                if (getattr(self, "_colony_scheduler", None) is not scheduler
                        or getattr(self._bridge, "connection_key", None) != connection):
                    return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
                return await self._consultation_impl(question, _ctx, game)
            task = asyncio.create_task(run_bound())
            inflight[key] = task
            self._consultation_tasks.add(task)
            def clear_inflight(done):
                self._consultation_tasks.discard(done)
                if inflight.get(key) is done:
                    inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(clear_inflight)
        return copy.deepcopy(await asyncio.shield(task))

    async def _consultation_impl(self, question, _ctx, game):
        if not isinstance(question, str) or not question.strip():
            return Err(SdkError("A RimWorld question is required.", code="invalid_question"))
        normalized = question.strip()
        if len(normalized) > 2000:
            return Err(SdkError("The RimWorld question is too long.", code="invalid_question"))
        settings = self._agent_settings
        if not settings.configured:
            return Err(
                SdkError(
                    "Configure the plugin-owned research model in the Hosted UI before consulting it.",
                    code="agent_not_configured",
                )
            )
        if game is None:
            return Err(SdkError("A connected colony is required for persistent frontdesk work.", code="colony_unavailable"))
        scheduler = getattr(self, "_colony_scheduler", None)
        if scheduler is not None:
            if scheduler.halted:
                return Err(SdkError("Autoplay is stopped after a failure; check the panel.", code="autoplay_halted"))
            if scheduler._autoplay_colony != game.colony_id:
                return Err(SdkError("Autoplay was enabled for another colony; enable it here first.", code="world_changed"))
            request_id = _ctx.get("run_id") if isinstance(_ctx, Mapping) else None
            request_id = request_id if isinstance(request_id, str) and request_id else None
            if request_id:
                retained = await scheduler.service.call("retained_response", game.colony_id, game.load_epoch,
                                                        question, request_id)
                if retained is not None:
                    events = await scheduler.service.call("events_by_kind", game.colony_id,
                                                          ("frontdesk.wake", "frontdesk.run_binding", "autoplay.stale_delivery"))
                    anchor = anchor_seq(retained)
                    wakes = [event for event in events if event["kind"] == "frontdesk.wake"
                             and event["load_epoch"] == retained["load_epoch"]
                             and anchor_seq(event) == anchor and event["seq"] < retained["seq"]]
                    wake_seq = wakes[-1]["seq"] if wakes else None
                    bindings = [event for event in events if event["kind"] == "frontdesk.run_binding"
                                and event["payload"].get("wake_seq") == wake_seq
                                and event["load_epoch"] == retained["load_epoch"] and event["seq"] < retained["seq"]]
                    stale = any(event["kind"] == "autoplay.stale_delivery"
                                and event["payload"].get("delivery_id") == wake_seq for event in events)
                    current = getattr(self._bridge, "session", None)
                    connection = getattr(self._bridge, "connection_key", None)
                    if (wake_seq is None or not bindings or stale or not connection
                            or bindings[-1]["payload"].get("connection") != connection or current is None
                            or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch)):
                        return Err(SdkError("The prepared response belongs to an earlier connection or expired world; start a new request.", code="world_changed"))
                    delivery = json.loads(retained["delivery_payload"]) if retained.get("delivery_payload") else {}
                    if delivery.get("handoff") is not None:
                        return Ok(delivery["handoff"])
                    return Ok(_consultation_handoff(retained["payload"]["result"], normalized))
            waiter_connection = getattr(self._bridge, "connection_key", None)
            if not isinstance(waiter_connection, str) or not waiter_connection:
                return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
            bound_game = SimpleNamespace(colony_id=game.colony_id, load_epoch=game.load_epoch,
                                         game_tick=game.game_tick, connection_epoch=waiter_connection)
            try:
                seq = await scheduler.wake_frontdesk(bound_game, question, request_id=request_id)
            except (ToolExecutionError, ValueError):
                return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
            if getattr(self._bridge, "connection_key", None) != waiter_connection:
                return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
            binding = (scheduler, waiter_connection, game.colony_id, game.load_epoch)
            future = self._autoplay_waiters.get(seq)
            if future is not None and getattr(future, "_autoplay_binding", None) != binding:
                return Err(SdkError("The prepared response belongs to another connection.", code="world_changed"))
            if future is None:
                future = asyncio.get_running_loop().create_future()
                future._autoplay_binding = binding
                self._autoplay_waiters[seq] = future
                def clear_waiter(done):
                    if self._autoplay_waiters.get(seq) is done:
                        self._autoplay_waiters.pop(seq, None)
                future.add_done_callback(clear_waiter)
            result = await asyncio.shield(future)
            if result.get("report", {}).get("saved"):
                result["report"]["export"] = await self._export_report(result["report"]["report_id"], _ctx)
            return Ok(_consultation_handoff(result, normalized))
        try:
            connection = getattr(self._bridge, "connection_key", None)
            if not isinstance(connection, str) or not connection:
                return Err(SdkError("The colony connection changed; retry in the current colony.", code="world_changed"))
            def binding_check():
                current = getattr(self._bridge, "session", None)
                return (current is not None and (current.colony_id, current.load_epoch) == (game.colony_id, game.load_epoch)
                        and getattr(self._bridge, "connection_key", None) == connection)
            service = getattr(self, "_workspace_service", None)
            if service is None or service.closed:
                service = FrontdeskService(
                    self.data_path("workspaces") / "events.sqlite", self._agent_runtime,
                    game_session=lambda: getattr(self._bridge, "session", None),
                )
                self._workspace_service = service
            await service.settle_pending(game.colony_id)
            request_id = _ctx.get("run_id") if isinstance(_ctx, Mapping) else None
            request_id = request_id if isinstance(request_id, str) and request_id else None
            retained_response = await service.call("retained_response", game.colony_id, game.load_epoch, question, request_id) if request_id else None
            current = getattr(self._bridge, "session", None)
            if current is None or (current.colony_id, current.load_epoch) != (game.colony_id, game.load_epoch):
                return Err(SdkError("The active colony changed during lookup; retry in the current colony.", code="world_changed"))
            if retained_response is not None:
                payload = retained_response["payload"]
                terminal = (payload.get("result") or {}).get("terminal") or {}
                scheduled = "trigger_seq" in payload or terminal.get("kind") in {"reply", "update_only"}
                if not scheduled:
                    events = await service.call("events_by_kind", game.colony_id,
                                                ("frontdesk.wake", "frontdesk.run_binding"))
                    wake_ids = {event["seq"] for event in events if event["kind"] == "frontdesk.wake"
                                and event["load_epoch"] == retained_response["load_epoch"]
                                and anchor_seq(event) == anchor_seq(retained_response)
                                and event["seq"] < retained_response["seq"]}
                    scheduled = any(event["kind"] == "frontdesk.run_binding"
                                    and event["load_epoch"] == retained_response["load_epoch"]
                                    and event["seq"] < retained_response["seq"]
                                    and event["payload"].get("wake_seq") in wake_ids for event in events)
                if scheduled:
                    return Err(SdkError("This prepared autoplay response has expired; start a new request in the current mode.",
                                        code="world_changed"))
            if retained_response is not None and retained_response.get("delivery_payload"):
                saved_delivery = json.loads(retained_response["delivery_payload"])
                if saved_delivery.get("handoff") is not None:
                    return Ok(saved_delivery["handoff"])
            if retained_response is not None:
                result = copy.deepcopy(retained_response["payload"]["result"])
                result["workspace"] = {"workspace_id": game.colony_id, "turn_id": str(retained_response["payload"]["utterance_seq"]), "state": "waiting", "history_saved": True}
            else:
                result = await service.consult(game, question, settings, request_id=request_id, binding_check=binding_check)
            if not result["workspace"]["history_saved"]:
                return Err(SdkError("The completed response could not be saved; retry the same request.", code="workspace_unavailable"))
        except (OSError, sqlite3.Error, ValueError) as exception:
            incident = self._record_unexpected("workspace", "Persistent work could not be completed.", exception)
            return Err(SdkError("The ongoing work could not be saved or loaded; retry after checking storage.", code="workspace_unavailable", details={"incident_id": incident}))
        if result.get("report", {}).get("saved"):
            result["report"]["export"] = await self._export_report(result["report"]["report_id"], _ctx)
        handoff = _consultation_handoff(result, normalized)
        workspace = result.get("workspace")
        if workspace is not None:
            payload = {
                "utterance_seq": int(workspace["turn_id"]), "summary": handoff["summary"],
                "delivery": handoff["delivery"], "report": result.get("report"),
                "prepared_at": utc_now(), "meaning": "Prepared host response; not a receipt of N.E.K.O consumption.", "handoff": handoff,
            }
            try:
                await service.call("record", game.colony_id, game.load_epoch, game.game_tick, "frontdesk", "agent.delivery", payload)
                service.revision += 1
            except (OSError, sqlite3.Error, RuntimeError):
                return Err(SdkError("The prepared response could not be saved; retry the same request.", code="delivery_unavailable"))
        return Ok(handoff)

    @ui.action(id="autoplay_mode", label="Set autoplay mode", refresh_context=True)
    @plugin_entry(id="autoplay_mode", name="Set autoplay mode", timeout=15.0,
                  description="Explicitly enable or disable autoplay for this process.",
                  metadata={"agent_auto": False},
                  input_schema={"type": "object", "properties": {"enabled": {"type": "boolean"},
                                "ui_nonce": {"type": "string", "writeOnly": True},
                                "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
                                "constraints": {"type": "string", "minLength": 1, "maxLength": 2000},
                                "goal_edited": {"type": "boolean"}, "constraints_edited": {"type": "boolean"}}, "required": ["enabled", "ui_nonce"],
                                "additionalProperties": False})
    async def autoplay_mode(self, enabled=False, ui_nonce="", goal="Prioritize survival and stability.",
                            constraints="No cannibalism or live organ harvesting.", goal_edited=False,
                            constraints_edited=False, **_):
        if not self._valid_settings_ui_nonce(ui_nonce) or type(enabled) is not bool:
            return Err(SdkError("Invalid panel action.", code="invalid_ui_action"))
        lock = getattr(self, "_autoplay_mode_lock", None)
        if lock is None:
            lock = self._autoplay_mode_lock = asyncio.Lock()
        async with lock:
            return await self._set_autoplay_mode(enabled, goal, constraints, goal_edited, constraints_edited)

    async def _set_autoplay_mode(self, enabled, goal, constraints, goal_edited, constraints_edited):
        try:
            if enabled and self._colony_scheduler is None:
                choices = {"goal": goal, "constraints": constraints, "goal_edited": goal_edited,
                           "constraints_edited": constraints_edited}
                if (any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in (goal, constraints))
                        or type(goal_edited) is not bool or type(constraints_edited) is not bool):
                    raise ValueError("Goal and constraints must be nonempty text of at most 2000 characters")
                if ((not goal_edited and goal != "Prioritize survival and stability.") or
                        (not constraints_edited and constraints != "No cannibalism or live organ harvesting.")):
                    raise ValueError("Unedited choices must match the displayed defaults")
                await self.start_colony_scheduler(choices)
            elif not enabled:
                game = getattr(self._bridge, "session", None)
                scheduler = self._colony_scheduler
                try:
                    if game is not None and scheduler is not None:
                        await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                                     "user", "autoplay.disabled", {})
                finally:
                    await self._stop_autoplay()
            return Ok({"enabled": self._colony_scheduler is not None})
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
            return Err(SdkError(str(error), code="autoplay_unavailable"))

    async def _stop_autoplay(self):
        scheduler = getattr(self, "_colony_scheduler", None)
        inflight = getattr(self, "_consultation_inflight", {})
        tasks = [task for key, task in list(inflight.items())
                 if scheduler is not None and key[0] == id(scheduler) and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        worker = getattr(self, "_autoplay_delivery", None)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._autoplay_delivery = None
        for future in getattr(self, "_autoplay_waiters", {}).values():
            if not future.done():
                future.cancel()
        scheduler = getattr(self, "_colony_scheduler", None)
        if scheduler is not None:
            await scheduler.close()
            self._colony_scheduler = None

    @ui.action(id="autoplay_undo", label="Undo action", refresh_context=True)
    @plugin_entry(id="autoplay_undo", name="Undo a colony action", timeout=15.0,
                  description="Undo a current-epoch commit after checking user takeovers.",
                  metadata={"agent_auto": False},
                  input_schema={"type": "object", "properties": {"seq": {"type": "integer"},
                                "ui_nonce": {"type": "string", "writeOnly": True}}, "required": ["seq", "ui_nonce"],
                                "additionalProperties": False})
    async def autoplay_undo(self, seq=0, ui_nonce="", **_):
        if not self._valid_settings_ui_nonce(ui_nonce) or type(seq) is not int:
            return Err(SdkError("Invalid panel action.", code="invalid_ui_action"))
        scheduler = self._colony_scheduler
        if scheduler is None or scheduler.halted:
            return Err(SdkError("Autoplay must be running to undo an action.", code="undo_unavailable"))
        current = getattr(self._bridge, "session", None)
        if current is None:
            return Err(SdkError("Current colony unavailable.", code="world_changed"))
        bound_game = SimpleNamespace(colony_id=current.colony_id, load_epoch=current.load_epoch,
                                     game_tick=current.game_tick,
                                     connection_epoch=getattr(self._bridge, "connection_key", None))
        async with scheduler.write_lock:
            return await self._autoplay_undo_locked(seq, scheduler, bound_game=bound_game)

    async def _autoplay_undo_locked(self, seq, scheduler, *, bound_game=None):
        failure_game = None
        try:
            from .agent.write.service import WriteService
            from .workspace.takeovers import TakeoverControl

            game = getattr(self._bridge, "session", None)
            work = self._workspace_service
            if game is None or work is None:
                raise ValueError("Current colony unavailable")
            failure_game = bound_game or SimpleNamespace(colony_id=game.colony_id, load_epoch=game.load_epoch,
                                                          game_tick=game.game_tick,
                                                          connection_epoch=getattr(self._bridge, "connection_key", None))
            game = failure_game
            expected = (game.connection_epoch, game.colony_id, game.load_epoch)
            def check_binding():
                current = getattr(self._bridge, "session", None)
                return (all(isinstance(value, str) and value for value in expected)
                        and self._colony_scheduler is scheduler and not scheduler.halted and current is not None
                        and (getattr(self._bridge, "connection_key", None), current.colony_id,
                             current.load_epoch) == expected)
            def require_binding():
                if not check_binding():
                    raise ToolExecutionError("world_changed", "The colony connection changed before undo.", recoverable=False)
            require_binding()
            event = await work.call("event", game.colony_id, seq)
            require_binding()
            write = event["payload"].get("write") if event and event["kind"] == "planner.progress" else None
            receipt = write.get("receipt") if isinstance(write, dict) else None
            if (event is None or not isinstance(write, dict) or event["load_epoch"] != game.load_epoch or write.get("stage") != "result"
                    or write.get("mode") != "commit" or not isinstance(receipt, dict)
                    or receipt.get("status") != "committed" or not receipt.get("undo_token")):
                raise ValueError("Current-epoch undo is unavailable")
            setup = await self._load_agent_tools()
            require_binding()
            if setup.write_identity is None or setup.write_identity() != expected:
                raise ToolExecutionError("world_changed", "The undo tool is bound to another connection.", recoverable=False)
            def identity():
                return expected if check_binding() and setup.write_identity() == expected else None
            guard = TakeoverControl(work, setup.ledger_query, identity, setup.write_fields_query)
            async def record(event_data):
                await work.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                "plugin", "autoplay.undo", {"source_seq": seq, "write": event_data})
            writer = WriteService(setup.write_catalog, [], setup.write_port, identity,
                                  record=record, guard=guard)
            await work.call("record", game.colony_id, game.load_epoch, game.game_tick, "user",
                            "autoplay.undo_requested", {"source_seq": seq})
            require_binding()
            result = await writer.undo({"token": receipt["undo_token"]})
            try:
                message = f"User requested undo of action {seq}; game reported {result.get('status', 'unknown')}. Review the affected task and receipt."
                await scheduler.wake(game, source="plugin", text=message)
                await scheduler.wake_frontdesk(game, message, source="plugin")
            except Exception:
                await self._autoplay_failure("scheduler", {"audit": {"error_code": "workspace_unavailable"}}, failure_game, origin_scheduler=scheduler)
                return Ok({"status": result.get("status"), "review_wake": "unconfirmed"})
            return Ok({"status": result.get("status")})
        except ToolExecutionError as error:
            if error.code == "undo_outcome_unknown":
                await self._autoplay_failure("scheduler", {"audit": {"error_code": "undo_outcome_unknown"}}, failure_game, origin_scheduler=scheduler)
                return Err(SdkError("Undo outcome is unknown; check the game and action log before retrying.",
                                    code="undo_outcome_unknown"))
            if error.code == "write_audit_failed":
                outcome = error.details.get("outcome")
                reconciliation = outcome != "not_sent"
                failure = {"audit": {"error_code": "workspace_unavailable"},
                           "write_recording": {"mode": "undo", "stage": "audit", "outcome": outcome or "unknown",
                                               "needs_reconciliation": reconciliation}}
                await self._autoplay_failure("scheduler", failure, failure_game, origin_scheduler=scheduler)
                message = ("Undo was not sent because its audit record was not saved." if not reconciliation else
                           "Undo outcome or receipt is unconfirmed; check the game and action log before retrying.")
                return Err(SdkError(message,
                                    code=error.code, details=error.details))
            return Err(SdkError(str(error), code=error.code))
        except BridgeQueryTimeout:
            await self._autoplay_failure("scheduler", {"audit": {"error_code": "undo_outcome_unknown"}}, failure_game, origin_scheduler=scheduler)
            return Err(SdkError("Undo outcome is unknown; check the game and action log before retrying.",
                                code="undo_outcome_unknown"))
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            return Err(SdkError(str(error), code="undo_unavailable"))

    async def _deliver_autoplay(self, scheduler):
        try:
            await self._delivery_loop(scheduler)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._autoplay_failure("frontdesk", {"audit": {"error_code": "workspace_unavailable"}}, origin_scheduler=scheduler)

    async def _delivery_loop(self, scheduler):
        while True:
            item = await scheduler.next_frontdesk_result()
            ident, result = item["delivery_id"], item["result"]
            terminal = result.get("terminal", {}).get("kind")
            pending = scheduler._pending_frontdesk
            through = item.get("through_seq", ident)
            waiters = [future for seq, future in self._autoplay_waiters.items()
                       if ident is not None and through is not None and ident <= seq <= through and not future.done()]
            if item.get("colony_id") is not None:
                bridge = getattr(self, "_bridge", None)
                current = getattr(bridge, "session", None)
                connection_changed = item.get("require_connection") and (
                    not item.get("connection_epoch") or getattr(bridge, "connection_key", None) != item["connection_epoch"])
                if (current is None or (current.colony_id, current.load_epoch) != (item["colony_id"], item["load_epoch"])
                        or connection_changed):
                    if pending is not None:
                        await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                            pending["game_tick"], "plugin", "autoplay.stale_delivery",
                            {"delivery_id": ident, "reason": "world_changed"})
                    for waiter in waiters:
                        waiter.set_result({"status": "failed", "audit": {"error_code": "world_changed"}})
                    if pending is not None:
                        await scheduler.ack_frontdesk_result(ident)
                    continue
            if pending is not None and terminal != "update_only":
                handoff = _consultation_handoff(result, "")
                await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                    pending["game_tick"], "frontdesk", "agent.delivery",
                    {"utterance_seq": int(result["workspace"]["turn_id"]), "summary": handoff["summary"],
                     "delivery": handoff["delivery"], "report": result.get("report"), "prepared_at": utc_now(),
                     "meaning": "Prepared host response; not a receipt of N.E.K.O consumption.", "handoff": handoff})
            if waiters:
                if pending is not None:
                    await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                        pending["game_tick"], "plugin", "autoplay.host_submission",
                        {"delivery_id": ident, "outcome": "user_handoff", "meaning": "Prepared for the waiting host request; not user consumption."})
                for waiter in waiters:
                    waiter.set_result(result)
            elif terminal != "update_only" and not item.get("failed"):
                handoff = _consultation_handoff(result, "")
                pending = scheduler._pending_frontdesk
                if pending is not None:
                    events = await scheduler.service.call("events_by_kind", pending["colony_id"], ("autoplay.host_submission",))
                    already_attempted = any(e["kind"] == "autoplay.host_submission"
                                            and e["payload"].get("delivery_id") == ident for e in events)
                    if not already_attempted:
                        # Record the attempt before crossing the SDK boundary. A crash in the
                        # gap leaves the outcome unknown, never a reason for unlimited replay.
                        await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                            pending["game_tick"], "plugin", "autoplay.host_submission",
                            {"delivery_id": ident, "outcome": "attempting", "meaning": "Submission outcome may be unknown; host may drop without a client."})
                        try:
                            submission = self.ctx.push_message(source="neko_rimworld", visibility=["chat"],
                                ai_behavior="blind", parts=[{"type": "text", "text": handoff["summary"]}])
                            outcome = "submitted" if isinstance(submission, dict) and submission.get("submitted") is True else "rejected"
                        except Exception:
                            outcome = "rejected"
                        await scheduler.service.call("record", pending["colony_id"], pending["load_epoch"],
                            pending["game_tick"], "plugin", "autoplay.host_submission_result",
                            {"delivery_id": ident, "outcome": outcome, "meaning": "Local submission only; host may drop without a client."})
            if ident is not None and not item.get("failed") and terminal != "update_only":
                await scheduler.ack_frontdesk_result(ident)

    async def _autoplay_failure(self, role, result, bound_game=None, *, origin_scheduler=None):
        scheduler = self._colony_scheduler
        current = getattr(self._bridge, "session", None)
        code = (result.get("audit") or {}).get("error_code") or "run_failed"
        origin_connection = getattr(bound_game, "connection_epoch", None)
        observed_connection = getattr(self._bridge, "connection_key", None)
        recording = result.get("write_recording")
        needs_reconciliation = isinstance(recording, Mapping) and recording.get("needs_reconciliation") is True
        same_world_reconciliation = (
            (code in UNKNOWN_WRITE_OUTCOME_CODES or needs_reconciliation) and scheduler is not None and origin_scheduler is scheduler
            and bound_game is not None and current is not None
            and (current.colony_id, current.load_epoch) == (bound_game.colony_id, bound_game.load_epoch)
            and isinstance(origin_connection, str) and bool(origin_connection)
            and isinstance(observed_connection, str) and bool(observed_connection)
            and origin_connection != observed_connection
        )
        stale = (origin_scheduler is not None and origin_scheduler is not scheduler) or (
            bound_game is not None and (current is None or
                (current.colony_id, current.load_epoch) != (bound_game.colony_id, bound_game.load_epoch) or
                (getattr(bound_game, "connection_epoch", None) is not None and
                 getattr(self._bridge, "connection_key", None) != bound_game.connection_epoch)))
        if stale and not same_world_reconciliation:
            source = origin_scheduler or scheduler
            audit = result.get("audit") or {}
            if bound_game is not None:
                stale_binding = (source, getattr(bound_game, "connection_epoch", None),
                                 bound_game.colony_id, bound_game.load_epoch)
                for waiter in self._autoplay_waiters.values():
                    if getattr(waiter, "_autoplay_binding", None) == stale_binding and not waiter.done():
                        waiter.set_result({"status": "failed", "audit": {"error_code": "world_changed"}})
            if source is not None and bound_game is not None and hasattr(source, "service"):
                try:
                    await source.service.call("record", bound_game.colony_id, bound_game.load_epoch,
                        getattr(bound_game, "game_tick", 0), "plugin", "autoplay.stale_failure",
                        {"role": role, "code": audit.get("error_code") or "run_failed",
                         "phase": audit.get("phase"), "exception_type": audit.get("exception_type"),
                         "run_id": audit.get("run_id"), "wake_seq": audit.get("wake_seq"),
                         "diagnostic_seq": audit.get("diagnostic_seq"), "diagnostic_status": audit.get("diagnostic_status"),
                         "connection_epoch": getattr(bound_game, "connection_epoch", None)})
                except Exception:
                    logger = getattr(self, "logger", None)
                    if logger is not None:
                        logger.warning("Stale autoplay failure could not be recorded")
            return
        if scheduler is None or (scheduler.halted and not same_world_reconciliation):
            return
        scheduler.halted = True
        if scheduler.budget is not None:
            scheduler.budget.blocked = True
        if same_world_reconciliation:
            current_task = asyncio.current_task()
            for name in ("_worker", "_frontdesk_worker"):
                worker = getattr(scheduler, name, None)
                if worker is not None and worker is not current_task and not worker.done():
                    worker.cancel()
        endpoint = code in {"model_transport", "model_transport_timeout", "model_first_response_timeout",
                            "model_incomplete_stream", "model_http_error", "model_stream_error"}
        if code == "model_request_deadline":
            endpoint = _unresponsive_request_deadline(
                getattr(self, "_agent_audit", None), (result.get("audit") or {}).get("run_id"),
                getattr(getattr(scheduler, "settings", None), "request_timeout_seconds", None))
        kind = "endpoint" if endpoint else "budget" if code == "agent_cumulative_budget" else "execution"
        audit = result.get("audit") or {}
        self._autoplay_fault = {"role": role, "code": code, "kind": kind, "pause": "unknown",
                                "phase": None, "exception_type": None, "run_id": None, "wake_seq": None,
                                "diagnostic_seq": None, "diagnostic_status": None,
                                "origin_connection_epoch": origin_connection if same_world_reconciliation else observed_connection}
        if same_world_reconciliation:
            self._autoplay_fault["observed_connection_epoch"] = observed_connection
        for key in ("phase", "exception_type", "run_id", "wake_seq", "diagnostic_seq", "diagnostic_status"):
            value = audit.get(key)
            if (isinstance(value, str) and value and len(value) <= 128) or (key in {"wake_seq", "diagnostic_seq"} and type(value) is int):
                self._autoplay_fault[key] = value
        failure_binding = (scheduler, getattr(self._bridge, "connection_key", None),
                           getattr(current, "colony_id", None), getattr(current, "load_epoch", None))
        for waiter in self._autoplay_waiters.values():
            binding = getattr(waiter, "_autoplay_binding", failure_binding)
            origin_binding = (scheduler, origin_connection, current.colony_id, current.load_epoch) if same_world_reconciliation else None
            if (binding == failure_binding or binding == origin_binding) and not waiter.done():
                waiter.set_result({**result, "status": result.get("status", "failed")})
        game = getattr(self._bridge, "session", None)
        if game is None or (bound_game is not None and (game.colony_id, game.load_epoch) != (bound_game.colony_id, bound_game.load_epoch)):
            self._autoplay_fault["pause"] = "unconfirmed" if endpoint else "unknown"
            await self._audit_failure_notification(scheduler, bound_game, role, code, kind, "pause unconfirmed")
            return
        try:
            await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                         "plugin", "autoplay.halted", dict(self._autoplay_fault))
        except Exception:
            self._autoplay_fault["record"] = "unconfirmed"
        if endpoint:
            try:
                setup = await self._load_agent_tools()
                identity = setup.write_identity() if setup.write_identity else None
                if (identity is None or tuple(identity[1:]) != (game.colony_id, game.load_epoch)
                        or (bound_game is not None and getattr(bound_game, "connection_epoch", identity[0]) != identity[0])):
                    raise ValueError("The game binding changed")
                await setup.write_port.set_paused(True, role="policy")
                self._autoplay_fault["pause"] = "requested"
                await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                                             "plugin", "autoplay.policy_pause", {"role": role, "code": code})
            except Exception:
                if self._autoplay_fault["pause"] != "requested":
                    self._autoplay_fault["pause"] = "unconfirmed"
                else:
                    self._autoplay_fault["record"] = "unconfirmed"
        pause_text = "pause requested" if self._autoplay_fault["pause"] == "requested" else "pause unconfirmed"
        await self._audit_failure_notification(scheduler, bound_game or game, role, code, kind, pause_text)

    async def _audit_failure_notification(self, scheduler, game, role, code, kind, pause_text):
        text = (f"RimWorld autoplay: {role} model endpoint failed ({code}); {pause_text}." if kind == "endpoint"
                else f"RimWorld autoplay stopped: {role}, {code}. Game pause state is unknown.")
        payload = {"role": role, "code": code, "kind": kind, "text": text, "ai_behavior": "blind",
                   "origin_connection_epoch": getattr(game, "connection_epoch", None),
                   "scope_kind": "failure_origin", "host_delivery_binding": "unavailable",
                   "outcome_meaning": "Local SDK submission only; user consumption is not confirmed."}
        async def record(phase, outcome=None):
            if game is None or not hasattr(game, "game_tick"):
                self._autoplay_fault["notification_record"] = "unknown"
                return
            try:
                await scheduler.service.call("record", game.colony_id, game.load_epoch, game.game_tick,
                    "plugin", "autoplay.failure_notification", {**payload, "phase": phase, **({"outcome": outcome} if outcome else {})})
            except Exception:
                self._autoplay_fault["notification_record"] = "unknown"
        await record("attempting")
        try:
            submission = self.ctx.push_message(source="neko_rimworld", visibility=["chat"], ai_behavior="blind",
                parts=[{"type": "text", "text": text}])
            outcome = "submitted" if isinstance(submission, dict) and submission.get("submitted") is True else "rejected"
        except Exception:
            outcome = "unknown"
        self._autoplay_fault["notification"] = outcome
        await record("result", outcome)

    def _notify_endpoint_failure(self, role, code, pause_text):
        try:
            self.ctx.push_message(source="neko_rimworld", visibility=["chat"], ai_behavior="blind",
                parts=[{"type": "text", "text": f"RimWorld autoplay: {role} model endpoint failed ({code}); {pause_text}."}])
        except Exception:
            pass

    def _notify_autoplay_stop(self, role, code):
        try:
            self.ctx.push_message(source="neko_rimworld", visibility=["chat"], ai_behavior="blind",
                parts=[{"type": "text", "text": f"RimWorld autoplay stopped: {role}, {code}. Game pause state is unknown."}])
        except Exception:
            pass

    async def _load_agent_tools(self) -> ToolSetup:
        setup = await self._catalog_tools.load()
        # Import runs outside this consultation. A ready database version is
        # pinned immediately; a first import never consumes the model deadline.
        self._knowledge.request_refresh()
        setup = compose_wiki_tools(setup, self._knowledge)
        setup = await compose_implementation_knowledge(setup)
        return setup

    async def _save_report(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._reports.save, record)

    async def _export_report(self, report_id: str, context: object) -> dict[str, Any]:
        run_id = context.get("run_id") if isinstance(context, Mapping) else None
        if not run_id:
            return {"status": "unavailable", "reason": "no_host_run"}
        try:
            record = await asyncio.to_thread(self._reports.read, report_id)
        except Exception:
            return {"status": "unavailable", "reason": "export_failed", "report_retained": None,
                    "diagnostic": {"stage": "report_read", "kind": "error"}}
        try:
            outcome = await asyncio.wait_for(
                self.export_push(
                    export_type="text",
                    run_id=run_id,
                    text=ReportStore.markdown(record),
                    label="RimWorld report " + report_id,
                    description="Full historical report (" + record["state"] + ")",
                    mime="text/markdown",
                    delivery="silent",
                    timeout=2.0,
                ),
                timeout=2.5,
            )
            if isinstance(outcome, Mapping) and outcome.get("success") is False:
                return {"status": "unavailable", "reason": "export_failed", "report_retained": True,
                        "diagnostic": {"stage": "export_push", "kind": "rejected"}}
            return {"status": "exported"}
        except Exception as error:
            diagnostic = {"stage": "export_push", "kind": "error"}
            if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
                diagnostic["kind"] = "timeout"
            elif isinstance(error, SdkError):
                diagnostic["kind"] = "sdk_error"
                # SDK code values can be arbitrary strings; expose only recognized constants.
                code = getattr(error, "code", None)
                if isinstance(code, str) and code in {"TIMEOUT", "CAPABILITY_UNAVAILABLE", "TRANSPORT_ERROR"}:
                    diagnostic["sdk_code"] = code
            return {"status": "unavailable", "reason": "export_failed", "report_retained": True,
                    "diagnostic": diagnostic}

    @ui.action(id="rimworld_report", label="Read saved reports", refresh_context=False)
    @plugin_entry(
        id="rimworld_report",
        name="Retrieve RimWorld reports",
        description="Hosted UI access to saved historical reports; list or read one report and export its full artifact.",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read"]},
                "report_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=8.0,
        metadata={"agent_auto": False, "llm_result_fields": ["summary"]},
    )
    async def rimworld_report(
        self, action: str = "list", report_id: str = "", offset: int = 0, limit: int = 5, **kwargs: object
    ):
        if (
            not isinstance(action, str)
            or action not in {"list", "read"}
            or type(offset) is not int
            or not 0 <= offset <= 1000
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            return Err(SdkError("Invalid report action or page.", code="invalid_report_request"))
        try:
            if action == "read":
                record = await asyncio.to_thread(self._reports.read, report_id)
                exported = await self._export_report(report_id, kwargs.get("_ctx"))
                summary = f"Historical {record['state']} report {report_id} is in the panel."
                summary += (
                    " Full artifact exported." if exported["status"] == "exported" else " Host export unavailable."
                )
                return Ok({"summary": summary, "report": record, "export": exported})
            result = await asyncio.to_thread(self._reports.list, offset, limit)
            summary = f"{result['total']} saved historical reports. IDs on this page:"
            included = 0
            for row in result["reports"]:
                title = str(row.get("question") or "").replace("\n", " ")[:50]
                line = f"\n{row['report_id']} ({row['state']}, {str(row['created_at'])[:19]}): {title}"
                continuation = f"\nContinue list at offset {offset + included + 1}."
                if count_handoff_tokens(summary + line + continuation)[0] > 180:
                    # Keep exact IDs discoverable even without a tokenizer or with a long title.
                    line = f"\n{row['report_id']} ({row['state']})"
                    if count_handoff_tokens(summary + line + continuation)[0] > 180:
                        break
                summary += line
                included += 1
            if offset + included < result["total"]:
                summary += f"\nContinue list at offset {offset + included}."
            result["summary"] = summary
            return Ok(result)
        except (ReportError, OSError, ValueError) as error:
            return Err(
                SdkError(
                    "The saved report could not be read; it may have expired or been removed.",
                    code=getattr(error, "code", "report_read_failed"),
                )
            )

    @ui.action(
        id="probe_game_overview",
        label="Probe game overview",
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="probe_game_overview",
        name="Probe RimWorld game overview",
        description="Request the colony overview section from the authenticated local RimWorld session.",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["summary"],
        timeout=5.0,
        metadata={"agent_auto": False},
    )
    async def probe_game_overview(self, **_: object):
        bridge = self._bridge
        if bridge is None:
            return Err(SdkError("RimWorld bridge is not listening.", code="bridge_not_running"))
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    bridge.request,
                    "query",
                    {"requests": [
                        {"from": "game", "select": ["paused", "current_map_id"]},
                        {"from": "maps", "select": ["id", "name", "is_current", "is_player_home", "colonist_count", "has_visible_threat"]},
                    ]},
                    2.5,
                ),
                timeout=3.5,
            )
            result = _query_summary(payload)
            if result["status"] not in {"ok", "partial"}:
                return Err(
                    SdkError(
                        result["summary"],
                        code=str(result.get("error_code") or "rimworld_query_failed"),
                        query_status=result["status"],
                    )
                )
            return Ok(result)
        except BridgeNotConnected:
            return Err(SdkError("No authenticated RimWorld game session is available.", code="game_offline"))
        except BridgeQueryTimeout as exception:
            return Err(
                SdkError(
                    "RimWorld did not answer the overview probe in time.",
                    code="query_timeout",
                    incident_id=exception.incident_id,
                )
            )
        except BridgeBusyError:
            return Err(SdkError("The RimWorld bridge is busy; retry later.", code="bridge_busy"))
        except BridgeUnsupportedError:
            return Err(
                SdkError(
                    "The connected RimWorld Mod does not support the game query capability.",
                    code="unsupported",
                )
            )
        except BridgeProtocolError as exception:
            return Err(
                SdkError(
                    "RimWorld returned an invalid or failed query response.",
                    code="protocol_error",
                    protocol_code=exception.protocol_code,
                    incident_id=exception.incident_id,
                )
            )
        except BridgeError as exception:
            return Err(SdkError("The RimWorld bridge request failed.", code=exception.code))
        except Exception:
            incident = self._state_store.record_incident(
                "probe_internal",
                "The colony overview probe failed unexpectedly.",
                level="error",
            )
            self.logger.exception("[probe] unexpected failure incident_id={}", incident)
            return Err(
                SdkError(
                    "The colony overview probe failed unexpectedly.",
                    code="bridge_internal",
                    incident_id=incident,
                )
            )

    async def _agent_query_tool(self, arguments: dict[str, Any]) -> Observation:
        operation = arguments.get("operation")
        query_arguments = arguments.get("arguments")
        if not isinstance(operation, str) or not operation.strip() or len(operation) > 128:
            raise ToolExecutionError("invalid_arguments", "operation must be non-empty text, at most 128 characters.")
        if not isinstance(query_arguments, dict):
            raise ToolExecutionError("invalid_arguments", "arguments must be a JSON object.")
        bridge = self._bridge
        if bridge is None:
            raise ToolExecutionError("bridge_not_running", "The RimWorld bridge is not listening.")
        idle_only = arguments.get("idle_only", False)
        if type(idle_only) is not bool:
            raise ToolExecutionError("invalid_arguments", "idle_only must be boolean.")
        request_options = {}
        if idle_only:
            binding = arguments.get("binding")
            session = bridge.session
            if (not isinstance(binding, (tuple, list)) or len(binding) != 3 or session is None
                    or (session.colony_id, session.load_epoch) != tuple(binding[1:])):
                raise ToolExecutionError("game_offline", "The bound game session changed.")
            request_options = {"idle_only": True, "expected_connection": binding[0], "expected_session": session}
        # Catalog initialization can also be expensive on the first modded
        # game query; do not remove every game tool after a 2.5-second warmup.
        response_timeout = 9.0 if operation in FRAME_READ_OPERATIONS or operation in {"query", "query_catalog"} else 2.5
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(bridge.request, operation, query_arguments, response_timeout, **request_options),
                timeout=response_timeout + 1.0,
            )
        except BridgeNotConnected as exception:
            raise ToolExecutionError(
                "game_offline", "No authenticated active RimWorld session is available."
            ) from exception
        except BridgeQueryTimeout as exception:
            raise ToolExecutionError(
                "query_timeout", "RimWorld did not answer the colony query in time."
            ) from exception
        except BridgeBusyError as exception:
            raise ToolExecutionError("bridge_busy", "The RimWorld bridge is busy; retry later.") from exception
        except BridgeUnsupportedError as exception:
            raise ToolExecutionError(
                "unsupported", "The connected Mod does not support generic queries."
            ) from exception
        except BridgeProtocolError as exception:
            raise ToolExecutionError("protocol_error", "RimWorld returned an invalid colony response.") from exception
        except BridgeError as exception:
            raise ToolExecutionError(exception.code, "The RimWorld bridge request failed.") from exception
        except Exception as exception:
            incident = self._record_unexpected(
                "agent_query_tool",
                "The agent colony tool failed unexpectedly.",
                exception,
            )
            raise ToolExecutionError(
                "agent_tool_internal",
                f"The colony tool failed unexpectedly; incident {incident}.",
            ) from None

        return Observation.from_response(operation, query_arguments, payload)

    def _valid_settings_ui_nonce(self, candidate: object) -> bool:
        if not isinstance(candidate, str):
            return False
        try:
            return secrets.compare_digest(candidate, self._settings_action_nonce)
        except TypeError:
            return False

    async def _restore_previous_settings(
        self,
        settings: BridgeSettings,
        *,
        candidate_status: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Restore the persisted bridge state after a pre-commit failure."""

        if candidate_status == "stop_failed":
            return False, {"status": "candidate_listener_retained"}
        restoration = await self._start_configured_bridge(settings, source="settings_rollback")
        restored = restoration["status"] in {"listening", "waiting_for_configuration"}
        if restored:
            self._state_store.diagnostics.record(
                "warning",
                "configuration",
                "The previous bridge state was restored after a settings failure.",
            )
            self._publish_coarse_status()
        return restored, restoration

    async def _start_configured_bridge(
        self,
        settings: BridgeSettings,
        *,
        source: str,
    ) -> dict[str, Any]:
        """Apply validated settings after the previous listener has stopped."""

        self._settings = settings
        self._state_store.transition(
            BridgeStatus.STARTING,
            endpoint=f"127.0.0.1:{settings.port}",
            port=settings.port,
            runtime={"phase": "starting", "worker_alive": False, "started_at": utc_now()},
            configuration={"token_configured": settings.token_configured},
        )
        if not settings.token_configured:
            self._state_store.diagnostics.record(
                "warning",
                "configuration",
                "Configure a 32-128 character pairing token before starting the listener.",
            )
            self._state_store.transition(
                BridgeStatus.STOPPED,
                runtime={"phase": "waiting_for_configuration", "worker_alive": False},
            )
            self._publish_coarse_status()
            return {"status": "waiting_for_configuration", "port": settings.port}

        try:
            bridge = BridgeServer(
                settings.pairing_token,
                self._state_store,
                self.get_logger("bridge"),
                port=settings.port,
                report_status=self._report_bridge_status,
            )
            self._bridge = bridge
            host, port = bridge.start()
        except BridgeStartError as exception:
            stopped = await self._stop_existing_bridge()
            self.logger.error(
                "[{}] bridge start failed code={} incident_id={}",
                source,
                exception.code,
                exception.incident_id,
            )
            self._state_store.transition(
                BridgeStatus.ERROR,
                runtime={"phase": "degraded" if stopped else "stop_failed", "worker_alive": not stopped},
            )
            self._publish_coarse_status()
            return {
                "status": "degraded" if stopped else "stop_failed",
                "port": settings.port,
                "incident_id": exception.incident_id,
            }
        except Exception as exception:
            incident = self._record_unexpected(
                f"{source}_bridge_start",
                "The bridge failed while applying settings.",
                exception,
            )
            stopped = await self._stop_existing_bridge()
            self._state_store.transition(
                BridgeStatus.ERROR,
                runtime={"phase": "error" if stopped else "stop_failed", "worker_alive": not stopped},
            )
            self._publish_coarse_status()
            return {
                "status": "degraded" if stopped else "stop_failed",
                "port": settings.port,
                "incident_id": incident,
            }

        self._state_store.update(
            runtime={"phase": "running", "worker_alive": True, "started_at": utc_now()},
            endpoint=f"{host}:{port}",
        )
        self._state_store.diagnostics.record("info", "lifecycle", "N.E.K.O RimWorld plugin started.")
        self._publish_coarse_status()
        return {"status": "listening", "host": host, "port": port}

    def _record_unexpected(self, scope: str, message: str, exception: Exception) -> str:
        """Record an unexpected failure without allowing credential text into logs."""

        incident = self._state_store.record_incident(scope, message, level="error")
        error_type = type(exception).__name__
        # Some configuration transports include request values in exception text.
        # Raise a new credential-free exception so logger.exception retains a
        # traceback without serializing the original exception or its message.
        try:
            raise RuntimeError(f"{scope} failed with {error_type}; details were redacted.") from None
        except RuntimeError:
            self.logger.exception(
                "[{}] unexpected failure incident_id={} error_type={}",
                scope,
                incident,
                error_type,
            )
        return incident

    async def _stop_existing_bridge(self) -> bool:
        bridge = self._bridge
        if bridge is None:
            return True
        try:
            stopped = bool(await asyncio.to_thread(bridge.stop, 3.0))
            if stopped and self._bridge is bridge:
                self._bridge = None
            return stopped
        except Exception as exception:
            self._record_unexpected(
                "shutdown_internal",
                "The bridge raised while stopping.",
                exception,
            )
            return False

    def _report_bridge_status(self, status: dict[str, Any]) -> None:
        try:
            self.report_status(json_safe_copy(status))
        except Exception:
            self.logger.warning("[status] bridge status update failed")

    def _publish_coarse_status(self) -> None:
        self._report_bridge_status(self._state_store.coarse_status())
