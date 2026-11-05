"""Provider-reported usage totals. Missing measurements are never assumed to be zero."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .tokenization import count_tokens

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "cache_miss_tokens")


def input_reservation(encoded_text: str, image_tokens: int = 0) -> dict[str, Any]:
    """Meter text and explicit image reservations; inline base64 is never tokenized.

    Provider prompt usage remains the authoritative combined measurement when
    present. The image allocation here is admission accounting, not a bill.
    """
    text_tokens, method = count_tokens(encoded_text)
    byte_count = len(encoded_text.encode("utf-8"))
    return {"text_tokens": text_tokens, "image_tokens_reserved": image_tokens,
            "tokens": text_tokens + image_tokens, "conservative_tokens": max(text_tokens, byte_count) + image_tokens,
            "method": method}


def summarize_usage(request_count: int, usages: Iterable[object]) -> dict[str, Any]:
    sums = dict.fromkeys(TOKEN_FIELDS, 0)
    coverage = dict.fromkeys(TOKEN_FIELDS, 0)
    reported = 0
    for raw in usages:
        if not isinstance(raw, Mapping):
            continue
        values = {key: raw[key] for key in TOKEN_FIELDS if type(raw.get(key)) is int and raw[key] >= 0}
        if "total_tokens" not in values and "prompt_tokens" in values and "completion_tokens" in values:
            values["total_tokens"] = values["prompt_tokens"] + values["completion_tokens"]
        if any(key in values for key in TOKEN_FIELDS[:3]):
            reported += 1
        for key, value in values.items():
            sums[key] += value
            coverage[key] += 1
    return {
        **{key: sums[key] if coverage[key] or request_count == 0 else None for key in TOKEN_FIELDS},
        "reported_requests": reported,
        "unreported_requests": max(0, request_count - reported),
        "coverage": coverage,
        "complete": all(coverage[key] == request_count for key in TOKEN_FIELDS[:3]),
    }


def combine_run_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"runs": len(summaries)}
    for key in (
        "model_calls",
        "model_responses",
        "model_failures",
        "model_cancellations",
        "tool_calls",
        "tool_failures",
    ):
        result[key] = sum(item.get(key, 0) for item in summaries)
    usage = {}
    coverage = {key: sum(item["usage"]["coverage"][key] for item in summaries) for key in TOKEN_FIELDS}
    for key in TOKEN_FIELDS:
        usage[key] = (
            sum(item["usage"][key] or 0 for item in summaries) if coverage[key] or result["model_calls"] == 0 else None
        )
    usage.update(
        coverage=coverage,
        reported_requests=sum(item["usage"]["reported_requests"] for item in summaries),
        unreported_requests=sum(item["usage"]["unreported_requests"] for item in summaries),
        complete=all(item["usage"]["complete"] for item in summaries),
    )
    result["usage"] = usage
    return result
