from __future__ import annotations

from dataclasses import dataclass

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
