from __future__ import annotations

import atexit
import os
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from flask import Flask, jsonify, redirect, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from relay.admin import admin_bp
from relay.config_store import ConfigStore
from relay.health import HealthProbeService
from relay.proxy import proxy_bp
from relay.settings import DEFAULT_MAX_REQUEST_BYTES, SESSION_COOKIE_NAME
from relay.state import RelayState
from relay.storage import HealthStore, ObjectIndexStore, RequestLogStore


def create_app(test_config: dict | None = None) -> Flask:
    test_config = test_config or {}
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=test_config.get("SECRET_KEY"),
        MAX_CONTENT_LENGTH=test_config.get("MAX_CONTENT_LENGTH", DEFAULT_MAX_REQUEST_BYTES),
        SESSION_COOKIE_NAME=SESSION_COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("RELAY_COOKIE_SECURE") == "1",
    )

    config_store = ConfigStore(test_config.get("CONFIG_PATH"))
    request_store = RequestLogStore(config_store.data_dir)
    object_store = ObjectIndexStore(config_store.data_dir)
    health_store = HealthStore(config_store.data_dir)
    health_service = HealthProbeService(config_store, health_store)
    upstream_client = httpx.Client(
        timeout=None,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
    )

    state = RelayState(
        config_store=config_store,
        request_store=request_store,
        object_store=object_store,
        health_store=health_store,
        health_service=health_service,
        upstream_client=upstream_client,
    )
    app.extensions["relay_state"] = state

    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = state.config_store.admin_settings["secret_key"]

    if not test_config.get("DISABLE_SCHEDULER"):
        health_service.start()
        atexit.register(upstream_client.close)
        atexit.register(health_service.stop)

    app.register_blueprint(admin_bp)
    app.register_blueprint(proxy_bp)

    @app.get("/")
    def index():
        return redirect(url_for("admin.dashboard"))

    @app.get("/healthz")
    def health_check():
        return jsonify({"status": "ok"})

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(error):
        if request.path.startswith("/v1/"):
            return jsonify({"error": {"type": "request_too_large", "message": "Request body is too large"}}), 413
        return error

    return app
