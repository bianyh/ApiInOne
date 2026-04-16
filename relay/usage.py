from __future__ import annotations

from typing import Any


def extract_usage(payload: dict[str, Any] | None) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    response_usage = payload.get("response", {}).get("usage") if isinstance(payload, dict) else None
    usage = usage or response_usage or {}
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    cached_tokens = (
        usage.get("input_tokens_details", {}) or {}
    ).get("cached_tokens") or (
        usage.get("prompt_tokens_details", {}) or {}
    ).get("cached_tokens")
    reasoning_tokens = (
        usage.get("output_tokens_details", {}) or {}
    ).get("reasoning_tokens") or (
        usage.get("completion_tokens_details", {}) or {}
    ).get("reasoning_tokens")
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    cache_ratio = round((int(cached_tokens or 0) / max(int(input_tokens or 0), 1)) * 100, 2) if input_tokens else None
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "cache_ratio": cache_ratio,
    }


def detect_fast_flag(payload: dict[str, Any] | None, duration_ms: float) -> bool:
    if isinstance(payload, dict):
        if payload.get("service_tier") == "flex":
            return True
    return duration_ms < 1000
