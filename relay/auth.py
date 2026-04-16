from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable

from flask import current_app, jsonify, request, session

from relay.config_store import hash_proxy_key
from relay.settings import SESSION_IDLE_TIMEOUT_SECONDS


def get_state():
    return current_app.extensions["relay_state"]


def authenticate_admin(username: str, password: str) -> bool:
    admin_settings = get_state().config_store.admin_settings
    return username == admin_settings["username"] and password == admin_settings["password"]


def login_admin() -> dict[str, Any]:
    session["admin_authenticated"] = True
    session["csrf_token"] = hash_proxy_key(
        f"csrf:{request.remote_addr}:{request.user_agent.string}:{time.time()}"
    )
    session["last_seen"] = time.time()
    return {
        "authenticated": True,
        "username": get_state().config_store.admin_settings["username"],
        "csrf_token": session["csrf_token"],
    }


def logout_admin() -> None:
    session.clear()


def _is_session_valid() -> bool:
    if not session.get("admin_authenticated"):
        return False
    last_seen = session.get("last_seen")
    if not isinstance(last_seen, (int, float)):
        return False
    if time.time() - last_seen > SESSION_IDLE_TIMEOUT_SECONDS:
        session.clear()
        return False
    session["last_seen"] = time.time()
    return True


def is_admin_authenticated() -> bool:
    return _is_session_valid()


def require_admin(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _is_session_valid():
            return jsonify({"error": {"message": "Authentication required"}}), 401
        return view(*args, **kwargs)

    return wrapper


def require_csrf(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-CSRF-Token", "")
        if not session.get("admin_authenticated"):
            return jsonify({"error": {"message": "Authentication required"}}), 401
        if not token or token != session.get("csrf_token"):
            return jsonify({"error": {"message": "Invalid CSRF token"}}), 403
        return view(*args, **kwargs)

    return wrapper


def resolve_proxy_key(raw_header: str | None) -> dict[str, Any] | None:
    if not raw_header or not raw_header.startswith("Bearer "):
        return None
    raw_key = raw_header.replace("Bearer ", "", 1).strip()
    if not raw_key:
        return None
    proxy_key = get_state().config_store.get_proxy_key_by_hash(hash_proxy_key(raw_key))
    if proxy_key and proxy_key.get("enabled"):
        return proxy_key
    return None
