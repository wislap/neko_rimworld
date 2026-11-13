"""Shared shape of sampled native background messages."""

from __future__ import annotations

import hashlib
import json

from ..model.tokenization import count_tokens


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def unavailable_background(header, token_budget, code, message, *, observation=None):
    """A disclosed source failure, never a fabricated empty or current snapshot."""
    failure = {"status": "unavailable", "error_code": code, "message": message}
    content = header + compact_json(failure)
    # Native errors are bounded on the wire, but disclose only their stable code
    # if an exceptional diagnostic cannot fit. Full error stays in the audit.
    if count_tokens(content)[0] > token_budget:
        failure.pop("message")
        content = header + compact_json(failure)
    tokens, method = count_tokens(content)
    return {
        **failure,
        "observation_id": observation.get("observation_id") if observation else None,
        "raw_sha256": hashlib.sha256(compact_json(observation).encode()).hexdigest() if observation else None,
        "projection_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "cache_hit": False, "sample": None, "checked_tick": None,
        "content": content, "tokens": tokens, "tokenizer": method, "token_budget": token_budget,
    }
