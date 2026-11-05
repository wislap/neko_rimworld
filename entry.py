from __future__ import annotations

import asyncio
import copy
import secrets
import sqlite3
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

from .agent.model.model_client import (
    MAX_RUN_BUDGET_SECONDS,
    AgentConfigurationError,
    AgentModelSettings,
    parse_agent_model_settings,
)
from .agent.reference.mechanism_tools import compose_implementation_knowledge
from .agent.reference.wiki_tools import compose_wiki_tools, knowledge_directory
from .agent.runtime.audit import AgentAuditRegistry
from .agent.runtime.frontdesk_service import FrontdeskService
from .agent.runtime.loop import AgentRuntime
from .autoplay import DEFAULT_CONSTRAINTS, DEFAULT_GOAL, Autoplay
from .configuration import (
    AGENT_BUDGET_FIELDS,
    DEFAULT_PORT,
    MAX_TOKEN_LENGTH,
    SETTINGS_SAVE_FIELDS,
    BridgeSettings,
    SettingsSaveOperations,
    agent_settings_document,
    parse_autoplay_settings,
    parse_bridge_settings,
    persist_settings,
    save_autoplay_budgets,
    updated_agent_settings,
)
from .consultation import Consultations
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
from .report_service import ReportService
from .reports import ReportError, ReportStore
from .tool_contracts import ToolExecutionError, ToolSetup
from .workspace.views import WorkspaceViews

PLUGIN_VERSION = "3.4.0"
# Cooperative world/action reads have an eight-second game-side lifetime.
# The transport must allow that operation to return its partial result.
FRAME_READ_OPERATIONS = frozenset({
    "survey_space", "inspect_route", "inspect_state", "inspect_actions", "inspect_threats", "read_situation",
    "inspect_planet", "inspect_factions", "inspect_quests", "inspect_trade", "inspect_research",
    "evaluate_journey", "evaluate_trade",
})


def _query_summary(response: Envelope) -> dict[str, Any]:
    payload = response.payload.to_dict()
    status = payload.get("status", "failed")
    return {
        "status": status,
        "summary": f"Game query {status}; {len(payload.get('issues', []))} issues.",
        "error_code": payload.get("error_code"),
        "response": response.to_dict(),
    }


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
        self._workspace_service = None
        self._settings_saves = SettingsSaveOperations()
        self._compose_services()
        self._agent_runtime = AgentRuntime(
            tools_provider=self._load_agent_tools,
            audit_registry=self._agent_audit,
            logger=self.get_logger("agent"),
            report_sink=self._report_service.save,
        )

    def _compose_services(self) -> None:
        """Wire the plugin's collaborators to its live host state, read at call time."""
        self._report_service = ReportService(
            store=lambda: self._reports,
            export_push=lambda **export: self.export_push(**export),
        )
        self._autoplay = Autoplay(
            bridge=lambda: self._bridge,
            agent_settings=lambda: self._agent_settings,
            frontdesk=self._frontdesk_service,
            workspace=lambda: self._workspace_service,
            load_tools=lambda: self._load_agent_tools(),
            config=lambda: self.config,
            push_message=lambda **message: self.ctx.push_message(**message),
            agent_audit=lambda: self._agent_audit,
            logger=lambda: self.logger,
        )
        self._consultations = Consultations(
            bridge=lambda: self._bridge,
            agent_settings=lambda: self._agent_settings,
            autoplay=self._autoplay,
            frontdesk=self._frontdesk_service,
            reports=self._report_service,
            record_unexpected=self._record_unexpected,
        )

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

            settings, warnings = parse_bridge_settings(config)
            self._autoplay.settings = parse_autoplay_settings(config)
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
        await self._settings_saves.close()
        await self._consultations.cancel_all()
        await self._autoplay.stop()
        await self._agent_runtime.cancel_all()
        workspace = self._workspace_service
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
                "image_input_enabled": self._agent_settings.image_input_enabled,
                "image_input_tokens_per_image": self._agent_settings.image_input_tokens_per_image,
                "agent_budgets": {key: getattr(self._agent_settings, key) for key in AGENT_BUDGET_FIELDS},
            }
            agent_snapshot = self._agent_runtime.snapshot(include_latest_details=False)
            # This process-local nonce makes the credential action callable only by
            # this Hosted UI surface. It is intentionally not displayed or logged.
            snapshot["action_nonce"] = self._settings_action_nonce
            snapshot["settings_saves"] = self._settings_saves.snapshot()
            safe = json_safe_copy(snapshot)
            # Keep the lightweight agent summaries under the audit sanitizer's contract.
            # Full run data is read on demand via get_agent_run_details.
            safe["agent"] = agent_snapshot
            safe["consultations_in_progress"] = len(self._consultations.tasks)
            safe["autoplay"] = await self._autoplay.panel_state()
            safe["autoplay"]["attention"] = {"available": None, "level": None, "source_status": "unavailable", "reasons": [],
                "next_daily_tick": None, "next_escalated_tick": None, "leases": [], "sampled_tick": None, "load_epoch": None}
            safe["autoplay"]["pause_watchdog"] = {"paused": None, "owner": None, "sampled_tick": None,
                "source_status": "unavailable", "wake_count": None, "notification": None}
            attention_unknown = copy.deepcopy(safe["autoplay"]["attention"])
            watchdog_unknown = copy.deepcopy(safe["autoplay"]["pause_watchdog"])
            bridge = self._bridge
            current = getattr(bridge, "session", None)
            scheduler = self._autoplay.scheduler
            connection = getattr(bridge, "connection_key", None)
            live_work = self._workspace_service
            scope = (connection, getattr(current, "colony_id", None), getattr(current, "load_epoch", None))
            view_colony, view_epoch = scope[1:]

            def scope_changed():
                latest = getattr(self._bridge, "session", None)
                latest_scope = (getattr(self._bridge, "connection_key", None), getattr(latest, "colony_id", None),
                                getattr(latest, "load_epoch", None))
                return (self._bridge is not bridge or latest_scope != scope
                        or self._autoplay.scheduler is not scheduler
                        or self._workspace_service is not live_work)
            if (current is not None and scheduler is not None and hasattr(scheduler, "pause_state")
                    and live_work is not None and not live_work.closed and isinstance(connection, str) and connection):
                available = getattr(bridge, "attention_available", None)
                display = await live_work.call("attention_display", view_colony, view_epoch, connection)
                if isinstance(display, dict):
                    safe["autoplay"]["attention"].update(display)
                    safe["autoplay"]["attention"]["available"] = available if type(available) is bool else None
                state = scheduler.pause_state
                if (isinstance(state, dict) and state.get("status") == "observed"
                        and (state.get("connection_epoch"), state.get("colony_id"), state.get("load_epoch"))
                        == (connection, view_colony, view_epoch)):
                    watchdog = safe["autoplay"]["pause_watchdog"]
                    watchdog.update(paused=state["paused"], owner=state["owner"], sampled_tick=state["sampled_tick"],
                                    source_status="observed")
                    if (state["paused"] and state["speed_paused"] and not state["force_paused"]
                            and not state["windows_force_paused"]
                            and state["owner"] in {"planner", "frontdesk", "reflex", "automatic_letter"}):
                        episode = [connection, view_colony, view_epoch,
                                   state["pause_revision"], state["pause_transition_revision"]]
                        records = await live_work.call("pause_watchdog_history", view_colony, view_epoch)
                        records = [event for event in records if event["kind"] == "pause.watchdog"
                                   and event["payload"].get("episode") == episode]
                        watchdog["wake_count"] = max((event["payload"].get("count", 0) for event in records
                                                      if event["payload"].get("action") == "wake"), default=0)
                        watchdog["notification"] = "not_sent"
                        attempts = [event for event in records if event["payload"].get("action") in {"notify_attempt", "notify"}]
                        results = [event for event in records if event["payload"].get("action") == "notify_result"]
                        if attempts:
                            outcome = results[-1]["payload"].get("outcome") if results and results[-1]["seq"] > attempts[-1]["seq"] else "unknown"
                            watchdog["notification"] = outcome if outcome in {"submitted", "rejected", "unknown"} else "unknown"
            if current is not None:
                work = self._workspace_service
                if work is not None and not work.closed:
                    activation = await work.call("latest_event", view_colony, "autoplay.activation")
                else:
                    views = WorkspaceViews(self.data_path("workspaces") / "events.sqlite")
                    activation = await asyncio.to_thread(views.latest_activation, view_colony)
                safe["autoplay"]["activation"] = activation
                if work is not None and not work.closed:
                    safe["autoplay"]["colony_work"] = await work.call("colony_work", view_colony, view_epoch, connection)
                else:
                    safe["autoplay"]["colony_work"] = await asyncio.to_thread(
                        views.colony_work, view_colony, view_epoch, connection)
            if work := self._workspace_service:
                if current is not None:
                    events = await work.call("action_events", view_colony)
                    actions = []
                    for event in events:
                        write = event["payload"].get("write") if event["kind"] in {"planner.progress", "autoplay.undo"} else None
                        if isinstance(write, dict) and write.get("stage") == "result" and write.get("mode") in {"commit", "undo"}:
                            receipt = write.get("receipt") or {}
                            actions.append({"seq": event["seq"], "epoch": event["load_epoch"],
                                            "tick": event["game_tick"], "mode": write["mode"],
                                            "status": receipt.get("status"), "changeset_id": receipt.get("changeset_id"),
                                            "undo_available": event["load_epoch"] == view_epoch and bool(receipt.get("undo_token"))})
                    safe["autoplay"].update(board=await work.call("board", view_colony),
                                            takeovers=await work.call("takeovers", view_colony), actions=actions[-50:])
            work = self._workspace_service
            safe["workspaces"] = {
                "revision": work.revision if work is not None else 0,
                "active_count": len(work.active) if work is not None else 0,
            }
            if scope_changed():
                safe["autoplay"]["attention"] = attention_unknown
                safe["autoplay"]["pause_watchdog"] = watchdog_unknown
                safe["autoplay"].pop("colony_work", None)
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
            if "scope_changed" in locals() and scope_changed():
                fallback.setdefault("autoplay", {})["attention"] = attention_unknown
                fallback["autoplay"]["pause_watchdog"] = watchdog_unknown
                fallback["autoplay"].pop("colony_work", None)
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
                "image_input_enabled": {"type": "boolean"},
                "image_input_tokens_per_image": {"type": "integer", "minimum": 0, "maximum": 1000000},
            }},
    )
    async def begin_settings_save(self, operation_id="", kind="", ui_nonce="", _ctx=None, **values):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(SdkError("Refresh the panel before saving.", code="ui_only"))
        if (not isinstance(operation_id, str) or len(operation_id) != 32
                or any(c not in "0123456789abcdef" for c in operation_id) or kind not in SETTINGS_SAVE_FIELDS):
            return Err(SdkError("Invalid settings operation.", code="invalid_settings_operation"))
        if set(values) - SETTINGS_SAVE_FIELDS[kind]:
            return Err(SdkError("Unsupported settings fields.", code="invalid_settings_operation"))
        # Freeze validated invocation input; the existing business save owns all
        # validation, rollback and activation. No UI request waits on that work.
        arguments = copy.deepcopy(values)
        async def execute():
            save = (self.save_bridge_settings if kind == "bridge" else
                    self._save_autoplay_settings if kind == "autoplay" else self.save_agent_settings)
            return await save(ui_nonce=ui_nonce, **arguments)
        try:
            return Ok(self._settings_saves.start(operation_id, kind, execute))
        except ValueError as error:
            return Err(SdkError("A settings save is already pending or unavailable.", code=str(error)))

    async def _save_autoplay_settings(self, ui_nonce="", budgets=None):
        if not self._valid_settings_ui_nonce(ui_nonce) or not isinstance(budgets, dict):
            return Err(SdkError("Invalid settings.", code="invalid_autoplay_settings"))
        try:
            self._autoplay.settings = await save_autoplay_budgets(self.config, budgets)
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
            return Ok(self._settings_saves.read(operation_id))
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

            current_settings, _warnings = parse_bridge_settings(current_config)
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
                "image_input_enabled": {"type": "boolean"},
                "image_input_tokens_per_image": {"type": "integer", "minimum": 0, "maximum": 1000000},
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
        image_input_enabled: bool | None = None,
        image_input_tokens_per_image: int | None = None,
        **_: object,
    ):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(
                SdkError(
                    "Open or refresh this plugin's Hosted UI before changing agent credentials.",
                    code="ui_only",
                )
            )
        try:
            next_settings = updated_agent_settings(
                self._agent_settings, base_url=base_url, model=model, api_key=api_key, budgets=budgets,
                image_input_enabled=image_input_enabled, image_input_tokens_per_image=image_input_tokens_per_image,
                extra=_,
            )
        except AgentConfigurationError as exception:
            return Err(SdkError(str(exception), code="invalid_agent_settings"))

        try:
            await persist_settings(self.config, {"neko_rimworld": {"agent": agent_settings_document(next_settings)}})
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
        return await self._consultations.consult(question, _ctx)

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
    async def autoplay_mode(self, enabled=False, ui_nonce="", goal=DEFAULT_GOAL,
                            constraints=DEFAULT_CONSTRAINTS, goal_edited=False,
                            constraints_edited=False, **_):
        if not self._valid_settings_ui_nonce(ui_nonce) or type(enabled) is not bool:
            return Err(SdkError("Invalid panel action.", code="invalid_ui_action"))
        return await self._autoplay.set_mode(enabled, goal, constraints, goal_edited, constraints_edited)

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
        return await self._autoplay.undo(seq)

    async def _load_agent_tools(self) -> ToolSetup:
        setup = await self._catalog_tools.load()
        # Import runs outside this consultation. A ready database version is
        # pinned immediately; a first import never consumes the model deadline.
        self._knowledge.request_refresh()
        setup = compose_wiki_tools(setup, self._knowledge)
        setup = await compose_implementation_knowledge(setup)
        return setup

    def _frontdesk_service(self) -> FrontdeskService:
        """The live persistent work service, reopened after it was closed."""
        service = self._workspace_service
        if service is None or service.closed:
            service = FrontdeskService(self.data_path("workspaces") / "events.sqlite", self._agent_runtime,
                                       game_session=lambda: getattr(self._bridge, "session", None))
            self._workspace_service = service
        service.media_store = self._reports.media_store
        return service

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
                return Ok(await self._report_service.read(report_id, kwargs.get("_ctx")))
            return Ok(await self._report_service.list_page(offset, limit))
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
