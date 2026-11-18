from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import httpx
from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from relay.auth import resolve_proxy_key
from relay.routing import build_upstream_url, resolve_bound_upstream, select_candidates
from relay.storage import ObjectIndexStore, RequestLogStore
from relay.usage import detect_fast_flag, extract_usage
from relay.utils import (
    extract_model_from_request,
    infer_endpoint_family,
    normalize_timeout_seconds,
    parse_json_bytes,
    utc_now_iso,
)


proxy_bp = Blueprint("proxy", __name__)


_SAFE_FORWARD_HEADERS = {
    "accept",
    "accept-language",
    "content-encoding",
    "content-type",
    "idempotency-key",
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
    body = _request_body()
    parsed = parse_json_bytes(body) if request.mimetype == "application/json" else None
    requested_model = extract_model_from_request(request)
    return {
        "body": body,
        "json": parsed,
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
        data: list[tuple[str, str]] = []
        for key, values in request.form.lists():
            for value in values:
                data.append((key, upstream_model if key == "model" else value))

        files: list[tuple[str, tuple[str, Any, str]]] = []
        for key, values in request.files.lists():
            for item in values:
                try:
                    if item.stream and item.stream.seekable():
                        item.stream.seek(0)
                except (AttributeError, OSError):
                    pass
                files.append(
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
        return {"headers": headers, "data": data, "files": files}

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
        "path": request.path,
        "method": request.method,
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
        self.buffer = b""
        self.last_payload: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                continue
            data = stripped[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                self.last_payload = parsed


def _passive_health(upstream_id: str, status_or_success: int | bool, ttfb_ms: float | None) -> None:
    if isinstance(status_or_success, bool):
        success = status_or_success
    else:
        success = int(status_or_success) < 400
    get_state().health_store.record_passive_result(
        upstream_id,
        success=success,
        ttfb_ms=ttfb_ms,
    )


def _mark_upstream_failover(upstream: dict[str, Any], exc: Exception, label: str = "Network error") -> None:
    get_state().health_store.mark_timeout_failover(
        upstream["id"],
        f"{label}: {type(exc).__name__}: {str(exc)}"[:240],
    )


def _maybe_mark_upstream_failover(
    upstream: dict[str, Any],
    exc: Exception,
    auto_failover_enabled: bool,
    label: str = "Network error",
) -> None:
    if auto_failover_enabled:
        _mark_upstream_failover(upstream, exc, label=label)


def _log_request(record: dict[str, Any], proxy_key_id: str) -> None:
    state = get_state()
    state.request_store.append(record)
    state.config_store.mark_proxy_key_used(proxy_key_id)


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
    query_params = request.query_string.decode("utf-8", errors="ignore")

    for index, upstream in enumerate(candidates):
        started = time.perf_counter()
        tracker = _StreamUsageTracker()
        ttfb_ms = None
        response_payload = None
        client = None
        try:
            target_url = build_upstream_url(upstream["base_url"], incoming_path)
            if query_params:
                target_url = f"{target_url}?{query_params}"
            request_kwargs = _build_upstream_request_kwargs(upstream["api_key"], ctx)

            client = httpx.Client(
                timeout=httpx.Timeout(normalize_timeout_seconds(upstream.get("timeout_ms", 120000))),
                follow_redirects=True,
            )
            upstream_response = client.send(
                client.build_request(
                    request.method,
                    target_url,
                    **request_kwargs,
                ),
                stream=True,
            )
            content_type = upstream_response.headers.get("content-type", "")
            is_stream = "text/event-stream" in content_type.lower()

            if upstream_response.status_code >= 500 and auto_failover_enabled and index < len(candidates) - 1:
                _passive_health(upstream["id"], False, None)
                if _is_timeout_status(upstream_response.status_code):
                    _maybe_mark_upstream_failover(
                        upstream,
                        RuntimeError(f"HTTP {upstream_response.status_code} upstream timeout response"),
                        auto_failover_enabled,
                        label="Timeout",
                    )
                last_error = f"upstream {upstream['name']} returned {upstream_response.status_code}"
                upstream_response.close()
                client.close()
                continue

            if is_stream:
                def generate():
                    nonlocal ttfb_ms, response_payload
                    first_chunk_seen = False
                    try:
                        for chunk in upstream_response.iter_bytes():
                            if not first_chunk_seen:
                                ttfb_ms = round((time.perf_counter() - started) * 1000, 2)
                                first_chunk_seen = True
                            tracker.feed(chunk)
                            yield chunk
                    finally:
                        upstream_response.close()
                        client.close()
                        duration_ms = round((time.perf_counter() - started) * 1000, 2)
                        response_payload = tracker.last_payload
                        if response_payload is not None:
                            _index_response_objects(state.object_store, upstream["id"], response_payload)
                        _passive_health(upstream["id"], upstream_response.status_code, ttfb_ms)
                        record = _build_log_record(
                            proxy_key=proxy_key,
                            upstream=upstream,
                            ctx=ctx,
                            status_code=upstream_response.status_code,
                            duration_ms=duration_ms,
                            ttfb_ms=ttfb_ms,
                            response_payload=response_payload,
                        )
                        _log_request(record, proxy_key["id"])

                response_headers = {
                    key: value
                    for key, value in upstream_response.headers.items()
                    if key.lower() not in {"content-length", "transfer-encoding", "content-encoding", "connection"}
                }
                return Response(
                    stream_with_context(generate()),
                    status=upstream_response.status_code,
                    headers=response_headers,
                )

            response_bytes = upstream_response.read()
            ttfb_ms = round((time.perf_counter() - started) * 1000, 2)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            if upstream_response.status_code >= 500 and auto_failover_enabled and index < len(candidates) - 1:
                _passive_health(upstream["id"], False, None)
                if _is_timeout_status(upstream_response.status_code):
                    _maybe_mark_upstream_failover(
                        upstream,
                        RuntimeError(f"HTTP {upstream_response.status_code} upstream timeout response"),
                        auto_failover_enabled,
                        label="Timeout",
                    )
                last_error = f"upstream {upstream['name']} returned {upstream_response.status_code}"
                upstream_response.close()
                client.close()
                continue

            if _is_timeout_status(upstream_response.status_code):
                _maybe_mark_upstream_failover(
                    upstream,
                    RuntimeError(f"HTTP {upstream_response.status_code} upstream timeout response"),
                    auto_failover_enabled,
                    label="Timeout",
                )

            if "application/json" in content_type.lower():
                response_payload = parse_json_bytes(response_bytes)
                if response_payload is not None:
                    _index_response_objects(state.object_store, upstream["id"], response_payload)

            _passive_health(upstream["id"], upstream_response.status_code, ttfb_ms)
            record = _build_log_record(
                proxy_key=proxy_key,
                upstream=upstream,
                ctx=ctx,
                status_code=upstream_response.status_code,
                duration_ms=duration_ms,
                ttfb_ms=ttfb_ms,
                response_payload=response_payload,
            )
            _log_request(record, proxy_key["id"])

            response_headers = {
                key: value
                for key, value in upstream_response.headers.items()
                if key.lower() not in {"content-length", "transfer-encoding", "content-encoding", "connection"}
            }
            upstream_response.close()
            client.close()
            return Response(
                response_bytes,
                status=upstream_response.status_code,
                headers=response_headers,
            )
        except httpx.TimeoutException as exc:
            last_error = str(exc)
            _maybe_mark_upstream_failover(upstream, exc, auto_failover_enabled, label="Timeout")
            _passive_health(upstream["id"], False, None)
            if auto_failover_enabled and index < len(candidates) - 1:
                if client:
                    client.close()
                continue
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            record = _build_log_record(
                proxy_key=proxy_key,
                upstream=upstream,
                ctx=ctx,
                status_code=599,
                duration_ms=duration_ms,
                ttfb_ms=None,
                response_payload=None,
                upstream_error=f"Timeout: {last_error}",
            )
            _log_request(record, proxy_key["id"])
            if client:
                client.close()
        except httpx.HTTPError as exc:
            last_error = str(exc)
            _maybe_mark_upstream_failover(upstream, exc, auto_failover_enabled)
            _passive_health(upstream["id"], False, None)
            if auto_failover_enabled and index < len(candidates) - 1:
                if client:
                    client.close()
                continue
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            record = _build_log_record(
                proxy_key=proxy_key,
                upstream=upstream,
                ctx=ctx,
                status_code=599,
                duration_ms=duration_ms,
                ttfb_ms=None,
                response_payload=None,
                upstream_error=last_error,
            )
            _log_request(record, proxy_key["id"])
            if client:
                client.close()

    return _error(last_error or "All upstreams failed", 502, "api_error")
