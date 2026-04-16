from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "all-api-in-one"
DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_DATA_DIRNAME = "data"
SESSION_COOKIE_NAME = "relay_admin_session"

# Admin credentials are kept on the backend. Public repo defaults are placeholders;
# set environment variables before deploying the service publicly.
ADMIN_USERNAME = os.getenv("RELAY_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("RELAY_ADMIN_PASSWORD", "change-me-before-deploy")
ADMIN_SECRET_KEY = os.getenv("RELAY_ADMIN_SECRET_KEY", "change-this-secret-key-before-deploy")
SESSION_IDLE_TIMEOUT_SECONDS = 60 * 60 * 8


DEFAULT_CONFIG = {
    "app": {
        "title": "API 中转站",
        "route_mode": "ordered_failover",
        "probe_interval_seconds": 300,
        "request_timeout_ms": 120000,
        "passive_window_size": 50,
    },
    "upstreams": [],
    "proxy_keys": [],
}
