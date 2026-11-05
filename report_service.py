"""Host-facing access to saved reports: off-thread storage, listing and export."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Mapping

from plugin.sdk.plugin import SdkError

from .agent.delivery.handoff import count_handoff_tokens
from .reports import ReportStore

# The listing summary is the host's LLM-visible projection of one page.
LIST_SUMMARY_TOKEN_BUDGET = 180


class ReportService:
    def __init__(self, *, store: Callable[[], ReportStore], export_push: Callable[..., Awaitable[object]]):
        self._store = store
        self._export_push = export_push

    async def save(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._store().save, record)

    async def export(self, report_id: str, context: object) -> dict[str, Any]:
        run_id = context.get("run_id") if isinstance(context, Mapping) else None
        if not run_id:
            return {"status": "unavailable", "reason": "no_host_run"}
        try:
            record = await asyncio.to_thread(self._store().read, report_id)
        except Exception:
            return {"status": "unavailable", "reason": "export_failed", "report_retained": None,
                    "diagnostic": {"stage": "report_read", "kind": "error"}}
        try:
            bundled = bool(record.get("attachments"))
            export_content = ({"export_type": "binary", "binary_data": await asyncio.to_thread(self._store().export_bundle, report_id),
                               "mime": "application/zip"} if bundled else
                              {"export_type": "text", "text": ReportStore.markdown(record), "mime": "text/markdown"})
            outcome = await asyncio.wait_for(
                self._export_push(
                    **export_content,
                    run_id=run_id,
                    label="RimWorld report " + report_id,
                    description="Full historical report (" + record["state"] + ")",
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

    async def read(self, report_id: str, context: object) -> dict[str, Any]:
        """One full report for the panel, exported to the host run when there is one."""
        record = await asyncio.to_thread(self._store().read, report_id)
        exported = await self.export(report_id, context)
        summary = f"Historical {record['state']} report {report_id} is in the panel."
        summary += " Full artifact exported." if exported["status"] == "exported" else " Host export unavailable."
        return {"summary": summary, "report": record, "export": exported}

    async def list_page(self, offset: int, limit: int) -> dict[str, Any]:
        """One page of report metadata with a bounded summary that keeps exact IDs discoverable."""
        result = await asyncio.to_thread(self._store().list, offset, limit)
        summary = f"{result['total']} saved historical reports. IDs on this page:"
        included = 0
        for row in result["reports"]:
            title = str(row.get("question") or "").replace("\n", " ")[:50]
            line = f"\n{row['report_id']} ({row['state']}, {str(row['created_at'])[:19]}): {title}"
            continuation = f"\nContinue list at offset {offset + included + 1}."
            if count_handoff_tokens(summary + line + continuation)[0] > LIST_SUMMARY_TOKEN_BUDGET:
                # Keep exact IDs discoverable even without a tokenizer or with a long title.
                line = f"\n{row['report_id']} ({row['state']})"
                if count_handoff_tokens(summary + line + continuation)[0] > LIST_SUMMARY_TOKEN_BUDGET:
                    break
            summary += line
            included += 1
        if offset + included < result["total"]:
            summary += f"\nContinue list at offset {offset + included}."
        result["summary"] = summary
        return result
