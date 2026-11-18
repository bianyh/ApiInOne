from __future__ import annotations

from dataclasses import dataclass

import httpx

from relay.config_store import ConfigStore
from relay.health import HealthProbeService
from relay.storage import HealthStore, ObjectIndexStore, RequestLogStore


@dataclass(slots=True)
class RelayState:
    config_store: ConfigStore
    request_store: RequestLogStore
    object_store: ObjectIndexStore
    health_store: HealthStore
    health_service: HealthProbeService
    upstream_client: httpx.Client
