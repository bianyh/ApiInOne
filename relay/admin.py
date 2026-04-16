from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request, session

from relay.auth import (
    authenticate_admin,
    is_admin_authenticated,
    login_admin,
    logout_admin,
    require_admin,
    require_csrf,
)
from relay.upstream_discovery import UpstreamDiscoveryError, discover_upstream_models


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def get_state():
    return current_app.extensions["relay_state"]


def _existing_upstream_map() -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in get_state().config_store.list_upstreams()
        if item.get("id")
    }


def _resolve_upstream_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    existing = _existing_upstream_map()
    current = existing.get(payload.get("id") or payload.get("upstream_id") or "", {})
    app_settings = get_state().config_store.get_app_settings()
    return {
        "id": payload.get("id") or current.get("id"),
        "name": (payload.get("name") or current.get("name") or "").strip(),
        "base_url": (payload.get("base_url") or current.get("base_url") or "").strip(),
        "api_key": payload.get("api_key") or current.get("api_key") or "",
        "timeout_ms": int(
            payload.get("timeout_ms")
            or current.get("timeout_ms")
            or app_settings.get("request_timeout_ms", 120000)
        ),
    }


def _auto_fill_models(upstream: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolve_upstream_credentials(upstream)
    discovered = discover_upstream_models(
        resolved["base_url"],
        resolved["api_key"],
        resolved["timeout_ms"],
    )
    upstream["allowed_models"] = discovered["models"]
    upstream["models_detected_at"] = discovered["discovered_at"]
    return upstream


@admin_bp.get("")
@admin_bp.get("/")
def dashboard():
    app_settings = get_state().config_store.get_app_settings()
    return render_template(
        "admin.html",
        page_title=app_settings.get("title", "API 中转站"),
    )


@admin_bp.get("/api/session")
def session_status():
    if not is_admin_authenticated():
        return jsonify({"authenticated": False})
    return jsonify(
        {
            "authenticated": True,
            "username": get_state().config_store.admin_settings["username"],
            "csrf_token": session.get("csrf_token"),
        }
    )


@admin_bp.post("/api/session")
def create_session():
    payload = request.get_json(silent=True) or {}
    if not authenticate_admin(payload.get("username", ""), payload.get("password", "")):
        return jsonify({"error": {"message": "用户名或密码错误"}}), 401
    return jsonify(login_admin())


@admin_bp.delete("/api/session")
@require_admin
@require_csrf
def delete_session():
    logout_admin()
    return jsonify({"ok": True})


@admin_bp.get("/api/upstreams")
@require_admin
def list_upstreams():
    return jsonify({"items": get_state().config_store.masked_upstreams()})


@admin_bp.put("/api/upstreams")
@require_admin
@require_csrf
def save_upstreams():
    payload = request.get_json(silent=True) or {}
    upstreams = payload.get("upstreams")
    if not isinstance(upstreams, list):
        return jsonify({"error": {"message": "upstreams must be a list"}}), 400
    normalized_upstreams = []
    for upstream in upstreams:
        item = dict(upstream)
        should_discover = bool(item.get("auto_discover_models"))
        allowed_models = [value for value in item.get("allowed_models", []) if str(value).strip()]
        if should_discover or not allowed_models:
            try:
                item = _auto_fill_models(item)
            except UpstreamDiscoveryError as exc:
                return jsonify({"error": {"message": str(exc)}}), 400
        normalized_upstreams.append(item)
    saved = get_state().config_store.save_upstreams(normalized_upstreams)
    return jsonify({"items": get_state().config_store.masked_upstreams(), "count": len(saved)})


@admin_bp.post("/api/upstreams/discover")
@require_admin
@require_csrf
def discover_upstream():
    payload = request.get_json(silent=True) or {}
    try:
        resolved = _resolve_upstream_credentials(payload)
        result = discover_upstream_models(
            resolved["base_url"],
            resolved["api_key"],
            resolved["timeout_ms"],
        )
    except UpstreamDiscoveryError as exc:
        return jsonify({"error": {"message": str(exc)}}), 400
    return jsonify({"item": result})


@admin_bp.get("/api/proxy-keys")
@require_admin
def list_proxy_keys():
    return jsonify({"items": get_state().config_store.sanitized_proxy_keys()})


@admin_bp.post("/api/proxy-keys")
@require_admin
@require_csrf
def create_proxy_key():
    payload = request.get_json(silent=True) or {}
    metadata, raw_key = get_state().config_store.create_proxy_key(
        payload.get("name", ""),
        payload.get("note", ""),
    )
    sanitized = dict(metadata)
    sanitized.pop("key_hash", None)
    return jsonify({"item": sanitized, "plaintext_key": raw_key}), 201


@admin_bp.patch("/api/proxy-keys/<key_id>")
@require_admin
@require_csrf
def update_proxy_key(key_id: str):
    payload = request.get_json(silent=True) or {}
    updated = get_state().config_store.update_proxy_key(key_id, payload)
    if not updated:
        return jsonify({"error": {"message": "Key not found"}}), 404
    sanitized = dict(updated)
    sanitized.pop("key_hash", None)
    return jsonify({"item": sanitized})


@admin_bp.get("/api/settings")
@require_admin
def get_settings():
    return jsonify(
        {
            "item": get_state().config_store.get_app_settings(),
        }
    )


@admin_bp.put("/api/settings")
@require_admin
@require_csrf
def update_settings():
    payload = request.get_json(silent=True) or {}
    allowed_fields = {
        "title",
        "route_mode",
        "probe_interval_seconds",
        "request_timeout_ms",
        "passive_window_size",
    }
    patch = {key: value for key, value in payload.items() if key in allowed_fields}
    updated = get_state().config_store.update_app_settings(patch)
    get_state().health_service.reload_schedule()
    return jsonify({"item": updated})


@admin_bp.get("/api/requests")
@require_admin
def get_requests():
    args = request.args
    result = get_state().request_store.query(
        page=int(args.get("page", 1)),
        page_size=int(args.get("page_size", 50)),
        proxy_key_id=args.get("proxy_key_id") or None,
        upstream_id=args.get("upstream_id") or None,
        model=args.get("model") or None,
        status_group=args.get("status_group") or None,
        days=int(args.get("days", 30)),
    )
    return jsonify(result)


@admin_bp.get("/api/stats")
@require_admin
def get_stats():
    days = int(request.args.get("days", 7))
    return jsonify(get_state().request_store.build_summary(days=days))


@admin_bp.get("/api/health")
@require_admin
def get_health():
    upstreams = get_state().config_store.list_upstreams()
    health = get_state().health_store.get_all()
    items = []
    for upstream in upstreams:
        entry = dict(upstream)
        entry["api_key"] = ""
        entry["api_key_masked"] = f"{upstream['api_key'][:8]}..." if upstream.get("api_key") else ""
        entry["health"] = health.get(upstream["id"], {})
        items.append(entry)
    return jsonify({"items": items})


@admin_bp.post("/api/health/probe")
@require_admin
@require_csrf
def probe_health():
    payload = request.get_json(silent=True) or {}
    upstream_id = payload.get("upstream_id")
    probe_model = payload.get("probe_model")
    upstreams = get_state().config_store.list_upstreams()
    if upstream_id:
        upstream = next((item for item in upstreams if item["id"] == upstream_id), None)
        if not upstream:
            return jsonify({"error": {"message": "Upstream not found"}}), 404
        result = get_state().health_service.probe_one(upstream, probe_model_override=probe_model)
        return jsonify({"item": result})
    return jsonify({"items": get_state().health_service.probe_all()})


@admin_bp.post("/api/health/probe/jobs")
@require_admin
@require_csrf
def start_probe_job():
    payload = request.get_json(silent=True) or {}
    upstream_id = payload.get("upstream_id")
    probe_model = payload.get("probe_model")
    probe_models = payload.get("probe_models") if isinstance(payload.get("probe_models"), dict) else {}
    upstreams = get_state().config_store.list_upstreams()
    if upstream_id:
        upstream = next((item for item in upstreams if item["id"] == upstream_id), None)
        if not upstream:
            return jsonify({"error": {"message": "Upstream not found"}}), 404
        selected_models = dict(probe_models)
        if isinstance(probe_model, str) and probe_model.strip():
            selected_models[upstream_id] = probe_model.strip()
        job = get_state().health_service.start_probe_job([upstream], probe_models=selected_models)
        return jsonify({"item": job}), 202

    targets = [item for item in upstreams if item.get("enabled") and item.get("probe_enabled")]
    if not targets:
        return jsonify({"error": {"message": "No probe-enabled upstreams found"}}), 400
    normalized_probe_models = {
        key: str(value).strip()
        for key, value in probe_models.items()
        if isinstance(value, str) and str(value).strip()
    }
    job = get_state().health_service.start_probe_job(targets, probe_models=normalized_probe_models)
    return jsonify({"item": job}), 202


@admin_bp.get("/api/health/probe/jobs/<job_id>")
@require_admin
def get_probe_job(job_id: str):
    job = get_state().health_service.get_probe_job(job_id)
    if not job:
        return jsonify({"error": {"message": "Probe job not found"}}), 404
    return jsonify({"item": job})
