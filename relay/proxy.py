from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from itertools import chain
from tempfile import SpooledTemporaryFile
from typing import Any
from urllib.parse import urlencode

import httpx
from flask import Blueprint, Response, current_app, jsonify, request
from werkzeug.wsgi import LimitedStream

from relay.auth import resolve_proxy_key
from relay.response_metadata import read_response_metadata
from relay.routing import build_upstream_url, resolve_bound_upstream, select_candidates
from relay.state import RelayState
from relay.storage import ObjectIndexStore
from relay.usage import detect_fast_flag, extract_usage
from relay.utils import (
    extract_model_from_request,
    infer_endpoint_family,
    normalize_timeout_seconds,
    parse_json_bytes,
    utc_now_iso,
)


proxy_bp = Blueprint("proxy", __name__)
logger = logging.getLogger(__name__)
_MAX_CAPTURE_BYTES = 4 * 1024**2


_SAFE_FORWARD_HEADERS = {
    "accept",
    "accept-language",
    "content-encoding",
    "content-type",
    "idempotency-key",
    "range",
    "if-range",
    "if-none-match",
    "if-modified-since",
    "openai-beta",
    "openai-organization",
    "openai-project",
    "openai-version",
    "anthropic-beta",
    "anthropic-version",
}


def get_state():
    return current_app.extensions["relay_state"]


def _error(message: str, status_code: int = 400, error_type: str = "invalid_request_error"):
    return jsonify({"error": {"message": message, "type": error_type}}), status_code


def _request_body() -> bytes:
    return request.get_data(cache=True) or b""


def _extract_request_context(path: str) -> dict[str, Any]:
    upload = None
    length = request.content_length
    if request.mimetype == "multipart/form-data" and length is not None and length > _MAX_CAPTURE_BYTES:
        source = request.environ["wsgi.input"]
        if hasattr(source, "seekable") and source.seekable():
            # Validate the body limit through Werkzeug before rewinding the WSGI input.
            request.stream
            upload = (source, source.tell(), length)
    body = b"" if upload is not None else _request_body()
    parsed = request.get_json(silent=True) if request.mimetype == "application/json" else None
    requested_model = extract_model_from_request(request)
    return {
        "body": body,
        "upload": upload,
        "json": parsed,
        "path": path,
        "method": request.method,
        "model": requested_model,
        "requested_model": requested_model,
        "upstream_model": requested_model,
        "model_mapped": False,
        "endpoint_family": infer_endpoint_family(path),
    }


def _apply_model_mapping(ctx: dict[str, Any], model_mappings: dict[str, str]) -> None:
    requested_model = ctx.get("requested_model")
    upstream_model = model_mappings.get(requested_model, requested_model) if requested_model else None
    ctx["upstream_model"] = upstream_model
    ctx["model_mapped"] = bool(requested_model and upstream_model and requested_model != upstream_model)


def _proxy_headers(upstream_api_key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {upstream_api_key}"}
    for key, value in request.headers.items():
        normalized = key.lower()
        if normalized not in _SAFE_FORWARD_HEADERS:
            continue
        headers[key] = value
    return headers


def _drop_header(headers: dict[str, str], name: str) -> None:
    for key in list(headers.keys()):
        if key.lower() == name:
            headers.pop(key, None)


def _merged_models():
    models = {}
    for upstream in get_state().config_store.list_upstreams():
        if not upstream.get("enabled"):
            continue
        for model in upstream.get("allowed_models", []):
            if model == "*":
                continue
            models.setdefault(model, {"id": model, "object": "model", "owned_by": upstream["name"]})
    for source_model, target_model in get_state().config_store.get_app_settings().get("model_mappings", {}).items():
        if not source_model or not target_model:
            continue
        models.setdefault(
            source_model,
            {"id": source_model, "object": "model", "owned_by": f"mapped_to:{target_model}"},
        )
    return {"object": "list", "data": list(models.values())}


def _build_upstream_request_kwargs(upstream_api_key: str, ctx: dict[str, Any]) -> dict[str, Any]:
    headers = _proxy_headers(upstream_api_key)
    body = ctx["body"]
    if not ctx.get("model_mapped"):
        if ctx["upload"] is not None:
            source, start, length = ctx["upload"]
            source.seek(start)
            headers["Content-Length"] = str(length)
            return {"headers": headers, "content": LimitedStream(source, length)}
        return {"headers": headers, "content": body if body else None}

    upstream_model = ctx.get("upstream_model")
    if not upstream_model:
        return {"headers": headers, "content": body if body else None}

    if request.mimetype == "application/json" and isinstance(ctx.get("json"), dict):
        payload = dict(ctx["json"])
        payload["model"] = upstream_model
        rewritten = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return {"headers": headers, "content": rewritten}

    if request.mimetype == "application/x-www-form-urlencoded":
        form_items: list[tuple[str, str]] = []
        for key, values in request.form.lists():
            for value in values:
                form_items.append((key, upstream_model if key == "model" else value))
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return {"headers": headers, "content": urlencode(form_items).encode("utf-8")}

    if request.mimetype == "multipart/form-data":
        parts = []
        for key, values in request.form.lists():
            for value in values:
                parts.append((key, (None, upstream_model if key == "model" else value)))

        for key, values in request.files.lists():
            for item in values:
                try:
                    if item.stream and item.stream.seekable():
                        item.stream.seek(0)
                except (AttributeError, OSError):
                    pass
                parts.append(
                    (
                        key,
                        (
                            item.filename or "upload",
                            item.stream,
                            item.content_type or "application/octet-stream",
                        ),
                    )
                )
        _drop_header(headers, "content-type")
        _drop_header(headers, "content-encoding")
        return {"headers": headers, "files": parts}

    return {"headers": headers, "content": body if body else None}


def _index_response_objects(store: ObjectIndexStore, upstream_id: str, payload: Any) -> None:
    def object_ids(value: Any) -> Iterator[tuple[str, str | None]]:
        if isinstance(value, dict):
            object_id = value.get("id")
            object_type = value.get("object")
            if isinstance(object_id, str):
                yield object_id, object_type if isinstance(object_type, str) else None
            for child in value.values():
                yield from object_ids(child)
        elif isinstance(value, list):
            for child in value:
                yield from object_ids(child)

    store.upsert_many(object_ids(payload), upstream_id)


def _build_log_record(
    proxy_key: dict[str, Any],
    upstream: dict[str, Any],
    ctx: dict[str, Any],
    status_code: int,
    duration_ms: float,
    ttfb_ms: float | None,
    response_payload: dict[str, Any] | None,
    upstream_error: str | None = None,
) -> dict[str, Any]:
    usage = extract_usage(response_payload)
    total_tokens = usage.get("total_tokens")
    return {
        "created_at": utc_now_iso(),
        "path": ctx["path"],
        "method": ctx["method"],
        "endpoint_family": ctx["endpoint_family"],
        "model": ctx["requested_model"],
        "requested_model": ctx["requested_model"],
        "upstream_model": ctx["upstream_model"],
        "model_mapped": ctx["model_mapped"],
        "proxy_key_id": proxy_key["id"],
        "proxy_key_name": proxy_key["name"],
        "upstream_id": upstream["id"],
        "upstream_name": upstream["name"],
        "status_code": status_code,
        "duration_ms": round(duration_ms, 2),
        "ttfb_ms": round(ttfb_ms, 2) if ttfb_ms is not None else None,
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "total_tokens": total_tokens,
        "cache_ratio": usage.get("cache_ratio"),
        "fast": detect_fast_flag(response_payload, duration_ms),
        "million_units": round((float(total_tokens or 0) / 1_000_000), 6),
        "upstream_error": upstream_error,
    }


class _StreamUsageTracker:
    def __init__(self) -> None:
        self.buffer = SpooledTemporaryFile(max_size=_MAX_CAPTURE_BYTES, mode="w+b")
        self.created_payload: dict[str, Any] | None = None
        self.last_payload: dict[str, Any] | None = None
        self.failed = False
        self.completed = False

    def feed(self, chunk: bytes) -> None:
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline == -1 else newline
            self.buffer.write(chunk[start:end])
            if newline == -1:
                return
            size = self.buffer.tell()
            self._parse_line(size)
            if size > _MAX_CAPTURE_BYTES:
                self.buffer.close()
                self.buffer = SpooledTemporaryFile(max_size=_MAX_CAPTURE_BYTES, mode="w+b")
            else:
                self.buffer.seek(0)
                self.buffer.truncate()
            start = newline + 1

    def close(self) -> None:
        self.buffer.close()

    def _parse_line(self, size: int) -> None:
        self.buffer.seek(0)
        if self.buffer.read(5) != b"data:":
            return
        if size > _MAX_CAPTURE_BYTES:
            parsed = read_response_metadata(self.buffer)
        else:
            data = self.buffer.read().strip()
            if not data:
                return
            if data == b"[DONE]":
                self.completed = True
                return
            try:
                parsed = json.loads(data)
            except (ValueError, RecursionError):
                return
        if not isinstance(parsed, dict):
            return
        event_type = parsed.get("type")
        if event_type == "response.created":
            self.created_payload = parsed
        if event_type == "response.completed":
            self.completed = True
        if event_type in ("response.failed", "error") or isinstance(parsed.get("error"), dict):
            self.failed = True
        if self.last_payload is None or any(key in parsed for key in ("usage", "response", "objects")):
            self.last_payload = parsed


def _passive_health(state: RelayState, upstream_id: str, status_code: int, ttfb_ms: float | None) -> None:
    try:
        state.health_store.record_passive_result(upstream_id, success=status_code < 400, ttfb_ms=ttfb_ms)
    except (OSError, ValueError):
        logger.exception("Failed to update upstream health for %s", upstream_id)


def _mark_upstream_failover(upstream: dict[str, Any], exc: Exception, label: str = "Network error") -> None:
    try:
        get_state().health_store.mark_timeout_failover(
            upstream["id"],
            f"{label}: {type(exc).__name__}: {str(exc)}"[:240],
        )
    except (OSError, ValueError):
        logger.exception("Failed to update upstream failover state for %s", upstream["id"])


def _maybe_mark_upstream_failover(
    upstream: dict[str, Any],
    exc: Exception,
    auto_failover_enabled: bool,
    label: str = "Network error",
) -> None:
    if auto_failover_enabled:
        _mark_upstream_failover(upstream, exc, label=label)


def _record_result(
    state: RelayState,
    proxy_key: dict[str, Any],
    upstream: dict[str, Any],
    ctx: dict[str, Any],
    status_code: int,
    started: float,
    ttfb_ms: float | None,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    indexed_payload: dict[str, Any] | None = None,
) -> None:
    duration_ms = (time.perf_counter() - started) * 1000
    if payload is not None or indexed_payload is not None:
        try:
            _index_response_objects(state.object_store, upstream["id"], [indexed_payload, payload])
        except (OSError, sqlite3.Error):
            logger.exception("Failed to index response objects for %s", upstream["id"])
    if status_code != 499:
        _passive_health(state, upstream["id"], status_code, ttfb_ms)
    try:
        state.request_store.append(_build_log_record(
            proxy_key, upstream, ctx, status_code, duration_ms, ttfb_ms, payload, error
        ))
        state.config_store.mark_proxy_key_used(proxy_key["id"])
    except (OSError, sqlite3.Error, ValueError, TypeError):
        logger.exception("Failed to record request to %s", upstream["id"])


def _forward_stream(
    upstream_response: httpx.Response,
    chunks: Iterator[bytes],
    state: RelayState,
    proxy_key: dict[str, Any],
    upstream: dict[str, Any],
    ctx: dict[str, Any],
    started: float,
    ttfb_ms: float | None = None,
    track_sse: bool = False,
    capture_json: bool = False,
    raw_file: bool = False,
) -> Response:
    # Keep only log metadata once the upstream has received the request.
    metadata = {key: value for key, value in ctx.items() if key not in {"body", "json", "upload"}}

    def generate():
        nonlocal ttfb_ms
        status = upstream_response.status_code
        error = None
        tracker = _StreamUsageTracker() if track_sse else None
        capture = SpooledTemporaryFile(max_size=_MAX_CAPTURE_BYTES, mode="w+b") if capture_json else None
        payload = None
        try:
            for chunk in chunks:
                if ttfb_ms is None:
                    ttfb_ms = (time.perf_counter() - started) * 1000
                if tracker is not None:
                    try:
                        tracker.feed(chunk)
                    except OSError:
                        logger.exception("Failed to capture SSE metadata")
                        tracker.close()
                        tracker = None
                if capture is not None:
                    try:
                        capture.write(chunk)
                    except OSError:
                        logger.exception("Failed to capture JSON response metadata")
                        capture.close()
                        capture = None
                yield chunk
            if tracker is not None and tracker.failed and status < 400:
                status, error = 502, "Upstream sent a stream error event"
            if capture is not None:
                try:
                    capture.seek(0)
                    payload = read_response_metadata(capture)
                except OSError:
                    logger.exception("Failed to inspect JSON response metadata")
        except GeneratorExit:
            if tracker is not None and tracker.failed:
                status, error = 502, "Upstream sent a stream error event"
            elif tracker is None or not tracker.completed:
                status, error = 499, "Client disconnected"
            raise
        except httpx.HTTPError as exc:
            status = 504 if isinstance(exc, httpx.TimeoutException) else 502
            error = f"Upstream stream interrupted: {type(exc).__name__}"
            raise
        finally:
            upstream_response.close()
            if capture is not None:
                capture.close()
            if tracker is not None:
                tracker.close()
            _record_result(
                state, proxy_key, upstream, metadata, status, started, ttfb_ms,
                payload=tracker.last_payload if tracker else payload,
                error=error,
                indexed_payload=tracker.created_payload if tracker else None,
            )

    response = Response(
        generate(), status=upstream_response.status_code, headers=_response_headers(upstream_response, raw_file)
    )
    response.call_on_close(upstream_response.close)
    return response


def _response_headers(upstream_response: httpx.Response, raw_file: bool = False) -> dict[str, str]:
    excluded = {"transfer-encoding", "connection", "set-cookie"}
    if not raw_file:
        excluded.update({"content-length", "content-encoding"})
    return {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in excluded
    }


def _request_url(path: str) -> str:
    if not request.query_string:
        return path
    return f"{path}?{request.query_string.decode('utf-8', errors='ignore')}"


def _is_timeout_status(status_code: int) -> bool:
    return status_code in {408, 504}


@proxy_bp.route("/v1/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def relay_request(subpath: str):
    state = get_state()
    proxy_key = resolve_proxy_key(request.headers.get("Authorization"))
    if not proxy_key:
        return _error("Invalid relay API key", 401, "authentication_error")

    incoming_path = f"/v1/{subpath}"
    if request.method == "GET" and incoming_path == "/v1/models":
        return jsonify(_merged_models())

    ctx = _extract_request_context(incoming_path)
    bound_upstream_id, requires_known_object = resolve_bound_upstream(incoming_path, state.object_store)
    if requires_known_object and not bound_upstream_id:
        return _error(
            "The requested object is not indexed by this relay.",
            404,
            "relay_object_not_indexed",
        )

    app_settings = state.config_store.get_app_settings()
    model_mappings = app_settings.get("model_mappings") if isinstance(app_settings.get("model_mappings"), dict) else {}
    _apply_model_mapping(ctx, model_mappings)
    route_mode = app_settings.get("route_mode", "ordered_failover")
    auto_failover_enabled = bool(app_settings.get("auto_failover_enabled", True))
    candidates = select_candidates(
        state.config_store.list_upstreams(),
        route_mode="fastest_first" if route_mode == "fastest_first" else "ordered_failover",
        health_store=state.health_store,
        model=ctx["upstream_model"],
        bound_upstream_id=bound_upstream_id,
        endpoint_family=ctx["endpoint_family"],
        health_failover_enabled=auto_failover_enabled,
    )
    if not candidates:
        if ctx["upstream_model"]:
            message = f"No upstream supports model '{ctx['upstream_model']}'"
            if ctx["model_mapped"]:
                message = f"No upstream supports mapped model '{ctx['upstream_model']}' for requested model '{ctx['requested_model']}'"
            return _error(message, 404, "model_not_found")
        return _error("No available upstream configured.", 503, "api_error")
    if not auto_failover_enabled:
        candidates = candidates[:1]

    last_error = None
    client = state.upstream_client

    for index, upstream in enumerate(candidates):
        started = time.perf_counter()
        upstream_response = None
        can_fail_over = auto_failover_enabled and index < len(candidates) - 1
        try:
            target_url = build_upstream_url(upstream["base_url"], _request_url(incoming_path))
            request_kwargs = _build_upstream_request_kwargs(upstream["api_key"], ctx)
            upstream_response = client.send(
                client.build_request(
                    request.method,
                    target_url,
                    timeout=httpx.Timeout(normalize_timeout_seconds(upstream.get("timeout_ms", 120000))),
                    **request_kwargs,
                ),
                stream=True,
            )
            status = upstream_response.status_code
            content_type = upstream_response.headers.get("content-type", "").lower()
            if _is_timeout_status(status):
                _maybe_mark_upstream_failover(
                    upstream, RuntimeError(f"HTTP {status} upstream timeout response"),
                    auto_failover_enabled, label="Timeout",
                )
            if can_fail_over and (status >= 500 or status in {408, 429}):
                upstream_response.close()
                _passive_health(state, upstream["id"], status, None)
                last_error = f"upstream {upstream['name']} returned {status}"
                continue

            is_sse = "text/event-stream" in content_type
            raw_file = not is_sse and incoming_path.startswith("/v1/files/") and incoming_path.endswith("/content")
            chunks = upstream_response.iter_raw() if raw_file else upstream_response.iter_bytes()
            if is_sse or raw_file:
                return _forward_stream(
                    upstream_response, chunks, state, proxy_key, upstream, ctx, started,
                    track_sse=is_sse, raw_file=raw_file,
                )

            response_bytes = bytearray()
            ttfb_ms = None
            for chunk in chunks:
                if ttfb_ms is None:
                    ttfb_ms = (time.perf_counter() - started) * 1000
                if len(response_bytes) + len(chunk) > _MAX_CAPTURE_BYTES:
                    return _forward_stream(
                        upstream_response, chain((bytes(response_bytes), chunk), chunks),
                        state, proxy_key, upstream, ctx, started, ttfb_ms, capture_json="application/json" in content_type,
                    )
                response_bytes.extend(chunk)
            upstream_response.close()
            payload = parse_json_bytes(response_bytes) if "application/json" in content_type else None
            _record_result(state, proxy_key, upstream, ctx, status, started, ttfb_ms, payload)
            return Response(bytes(response_bytes), status=status, headers=_response_headers(upstream_response))
        except httpx.HTTPError as exc:
            if upstream_response is not None:
                upstream_response.close()
            label = "Timeout" if isinstance(exc, httpx.TimeoutException) else "Network error"
            last_error = f"{label}: {type(exc).__name__}"
            _maybe_mark_upstream_failover(upstream, exc, auto_failover_enabled, label=label)
            if can_fail_over:
                _passive_health(state, upstream["id"], 599, None)
                continue
            _record_result(state, proxy_key, upstream, ctx, 599, started, None, error=last_error)

    return _error(last_error or "All upstreams failed", 502, "api_error")
