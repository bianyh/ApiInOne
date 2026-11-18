from __future__ import annotations

from typing import Any

from relay.storage import HealthStore, ObjectIndexStore
from relay.utils import should_expect_indexed_object


def build_upstream_url(base_url: str, incoming_path: str) -> str:
    normalized_base = base_url.rstrip("/")
    if normalized_base.endswith("/v1") and incoming_path.startswith("/v1/"):
        return f"{normalized_base}{incoming_path[3:]}"
    return f"{normalized_base}{incoming_path}"


def select_candidates(
    upstreams: list[dict[str, Any]],
    route_mode: str,
    health_store: HealthStore,
    model: str | None = None,
    bound_upstream_id: str | None = None,
    endpoint_family: str | None = None,
    health_failover_enabled: bool = True,
) -> list[dict[str, Any]]:
    enabled = [item for item in upstreams if item.get("enabled") and item.get("base_url") and item.get("api_key")]
    if bound_upstream_id:
        enabled = [item for item in enabled if item["id"] == bound_upstream_id]
    if model:
        enabled = [
            item for item in enabled
            if not item.get("allowed_models")
            or "*" in item.get("allowed_models", [])
            or model in item.get("allowed_models", [])
        ]
    if endpoint_family:
        enabled = [
            item for item in enabled
            if "*" in item.get("endpoint_capabilities", ["*"])
            or endpoint_family in item.get("endpoint_capabilities", ["*"])
        ]
    enabled.sort(key=lambda item: int(item.get("order", 9999)))
    health = health_store.get_all()
    if not health_failover_enabled:
        if route_mode != "fastest_first":
            return enabled
        return sorted(
            enabled,
            key=lambda item: (
                health.get(item["id"], {}).get("last_probe_success") is False,
                float(health.get(item["id"], {}).get("passive_avg_ttfb_ms") or 10**9),
                int(item.get("order", 9999)),
            ),
        )

    active = [item for item in enabled if not health.get(item["id"], {}).get("timeout_failover_active")]
    suppressed = [item for item in enabled if health.get(item["id"], {}).get("timeout_failover_active")]

    if route_mode != "fastest_first":
        return active + suppressed

    if active:
        primary = active
        fallback = suppressed
    else:
        primary = suppressed
        fallback = []

    ranked = sorted(
        primary,
        key=lambda item: (
            health.get(item["id"], {}).get("last_probe_success") is False,
            float(health.get(item["id"], {}).get("passive_avg_ttfb_ms") or 10**9),
            int(item.get("order", 9999)),
        ),
    )
    return ranked + fallback


def resolve_bound_upstream(path: str, object_store: ObjectIndexStore) -> tuple[str | None, bool]:
    if not should_expect_indexed_object(path):
        return None, False

    segments = [segment for segment in path.split("/") if segment]
    match = object_store.match_from_segments(segments[2:])
    if match:
        return match["upstream_id"], False
    return None, True
