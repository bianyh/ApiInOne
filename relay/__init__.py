from __future__ import annotations

import atexit

from flask import Flask, redirect, url_for

from relay.admin import admin_bp
from relay.config_store import ConfigStore
from relay.health import HealthProbeService
from relay.proxy import proxy_bp
from relay.settings import SESSION_COOKIE_NAME
from relay.state import RelayState
from relay.storage import HealthStore, ObjectIndexStore, RequestLogStore


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=test_config.get("SECRET_KEY") if test_config else None,
        SESSION_COOKIE_NAME=SESSION_COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,
    )

    config_store = ConfigStore(test_config.get("CONFIG_PATH") if test_config else None)
    request_store = RequestLogStore(config_store.data_dir)
    object_store = ObjectIndexStore(config_store.data_dir)
    health_store = HealthStore(config_store.data_dir)
    health_service = HealthProbeService(config_store, health_store)

    state = RelayState(
        config_store=config_store,
        request_store=request_store,
        object_store=object_store,
        health_store=health_store,
        health_service=health_service,
    )
    app.extensions["relay_state"] = state

    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = state.config_store.admin_settings["secret_key"]

    if not (test_config and test_config.get("DISABLE_SCHEDULER")):
        health_service.start()

    app.register_blueprint(admin_bp)
    app.register_blueprint(proxy_bp)

    @app.get("/")
    def index():
        return redirect(url_for("admin.dashboard"))

    if not (test_config and test_config.get("DISABLE_SCHEDULER")):
        atexit.register(state.health_service.stop)

    return app
