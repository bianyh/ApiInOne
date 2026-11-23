from __future__ import annotations

import time
from typing import Any

import httpx

from relay.routing import build_upstream_url
from relay.utils import utc_now_iso


class UpstreamDiscoveryError(Exception):
    pass


def discover_upstream_models(base_url: str, api_key: str) -> dict[str, Any]:
    normalized_base_url = (base_url or "").strip().rstrip("/")
    normalized_api_key = (api_key or "").strip()
    if not normalized_base_url:
        raise UpstreamDiscoveryError("Base URL 不能为空")
    if not normalized_api_key:
        raise UpstreamDiscoveryError("API Key 不能为空")

    started = time.perf_counter()
    try:
        with httpx.Client(
            timeout=None,
            follow_redirects=True,
        ) as client:
            response = client.get(
                build_upstream_url(normalized_base_url, "/v1/models"),
                headers={"Authorization": f"Bearer {normalized_api_key}"},
            )
    except httpx.HTTPError as exc:
        raise UpstreamDiscoveryError(f"模型探查失败: {exc}") from exc

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if response.status_code >= 400:
        detail = response.text[:300] if response.text else f"HTTP {response.status_code}"
        raise UpstreamDiscoveryError(f"模型探查失败: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamDiscoveryError("模型探查失败: 上游返回的不是 JSON 模型列表") from exc
    model_ids = sorted(
        {
            str(item.get("id")).strip()
            for item in payload.get("data", [])
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
    )
    if not model_ids:
        raise UpstreamDiscoveryError("上游返回成功，但模型列表为空")

    return {
        "base_url": normalized_base_url,
        "models": model_ids,
        "model_count": len(model_ids),
        "latency_ms": latency_ms,
        "status_code": response.status_code,
        "discovered_at": utc_now_iso(),
    }
