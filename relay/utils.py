from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import Request


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_timeout_seconds(timeout_ms: Any, default_ms: int = 120000, minimum_ms: int = 50) -> float:
    try:
        resolved_ms = int(timeout_ms)
    except (TypeError, ValueError):
        resolved_ms = int(default_ms)
    if resolved_ms <= 0:
        resolved_ms = int(default_ms)
    resolved_ms = max(resolved_ms, int(minimum_ms))
    return float(resolved_ms) / 1000.0


def parse_json_bytes(data: bytes) -> dict[str, Any] | None:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def extract_model_from_request(request: Request) -> str | None:
    if request.method not in {"POST", "PUT"}:
        return None
    payload = None
    if request.mimetype == "application/json":
        payload = request.get_json(silent=True)
    elif request.mimetype in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        model = request.form.get("model")
        return model.strip() if model else None
    if isinstance(payload, dict):
        model = payload.get("model")
        return str(model).strip() if model else None
    return None


def infer_endpoint_family(path: str) -> str:
    if path.startswith("/v1/chat/completions"):
        return "chat.completions"
    if path.startswith("/v1/responses"):
        return "responses"
    if path.startswith("/v1/embeddings"):
        return "embeddings"
    if path.startswith("/v1/images"):
        return "images"
    if path.startswith("/v1/audio"):
        return "audio"
    if path.startswith("/v1/files"):
        return "files"
    if path.startswith("/v1/vector_stores"):
        return "vector_stores"
    if path.startswith("/v1/assistants"):
        return "assistants"
    if path.startswith("/v1/threads"):
        return "threads"
    if path.startswith("/v1/batches"):
        return "batches"
    return "other"


def should_expect_indexed_object(path: str) -> bool:
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 3 or segments[0] != "v1":
        return False
    root = segments[1]
    if root == "responses" and segments[2] in {"compact", "input_tokens"}:
        return False
    object_roots = {
        "files",
        "vector_stores",
        "assistants",
        "threads",
        "batches",
        "responses",
    }
    return root in object_roots
