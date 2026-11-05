"""One optional tokenizer for public input and delivery accounting."""

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def tokenizer() -> Any | None:
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except (ImportError, KeyError, RuntimeError, ValueError):
        return None


def count_tokens(text: str) -> tuple[int, str]:
    """Token count via the shared optional tokenizer, with the method named.

    Without a tokenizer, UTF-8 byte length is a conservative upper bound for a
    byte-level tokenizer; 'replace' keeps the count defined for any input.
    """
    encoding = tokenizer()
    if encoding is None:
        return len(text.encode("utf-8", "replace")), "utf8_bytes_upper_bound"
    return len(encoding.encode(text, disallowed_special=())), encoding.name
