from __future__ import annotations

import copy
import json
import secrets
import threading
import time
from typing import Any

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

from relay.config_store import ConfigStore
from relay.routing import build_upstream_url
from relay.storage import HealthStore
from relay.utils import normalize_timeout_seconds, utc_now_iso


_NON_TEXT_MODEL_HINTS = (
    "embedding",
    "tts",
    "whisper",
    "transcribe",
    "moderation",
    "image",
    "vision",
    "realtime",
)
DEFAULT_PROBE_MODEL = "gpt-5.4"
DEFAULT_PROBE_ENDPOINT = "chat.completions"
PROBE_ENDPOINTS = {"chat.completions", "responses"}
PROBE_ALL_JOB_ID = "probe_all"
TIMEOUT_RECOVERY_JOB_ID = "probe_timeout_recovery"


def _step(stage: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "stage": stage,
        "status": status,
        "message": message,
        "timestamp": utc_now_iso(),
    }
    payload.update(extra)
    return payload


def _record_step(
    steps: list[dict[str, Any]],
    callback,
    stage: str,
    status: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    entry = _step(stage, status, message, **extra)
    steps.append(entry)
    if callback:
        callback(copy.deepcopy(entry))
    return entry


def _validate_models_probe_response(response: httpx.Response) -> tuple[bool, str | None, list[str]]:
    if response.status_code >= 400:
        detail = response.text.strip()
        if detail:
            detail = detail[:240]
            return False, f"HTTP {response.status_code}: {detail}", []
        return False, f"HTTP {response.status_code}", []

    try:
        payload = response.json()
    except ValueError:
        return False, "Probe response was not valid JSON", []

    if not isinstance(payload, dict):
        return False, "Probe response JSON was not an object", []

    data = payload.get("data")
    if not isinstance(data, list):
        return False, "Probe response did not contain a model list", []

    model_ids = [
        item["id"].strip()
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
    ]
    if not model_ids:
        return False, "Probe response contained no valid model ids", []

    return True, None, model_ids


def _supports_endpoint(upstream: dict[str, Any], endpoint_family: str) -> bool:
    capabilities = upstream.get("endpoint_capabilities") or ["*"]
    return "*" in capabilities or endpoint_family in capabilities


def _default_probe_endpoint_for(upstream: dict[str, Any]) -> str:
    capabilities = upstream.get("endpoint_capabilities") or ["*"]
    if "*" in capabilities or "chat.completions" in capabilities:
        return DEFAULT_PROBE_ENDPOINT
    if "responses" in capabilities:
        return "responses"
    return DEFAULT_PROBE_ENDPOINT


def _extract_text_from_chat_payload(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for choice in payload.get("choices", []):
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            chunks.append(delta["content"])
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            chunks.append(message["content"])
    return "".join(chunk for chunk in chunks if chunk)


def _extract_text_from_response_payload(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    delta = payload.get("delta")
    if payload.get("type") == "response.output_text.delta" and isinstance(delta, str):
        chunks.append(delta)

    response_payload = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str):
        chunks.append(output_text)

    output = response_payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunk for chunk in chunks if chunk)


def _extract_text_from_probe_payload(endpoint_family: str, payload: dict[str, Any]) -> str:
    if endpoint_family == "responses":
        return _extract_text_from_response_payload(payload)
    return _extract_text_from_chat_payload(payload)


def _append_unique_text(target: list[str], text: str) -> None:
    if not isinstance(text, str):
        return
    if not text.strip():
        return
    if target and target[-1] == text:
        return
    target.append(text)


def _validate_probe_response(
    client: httpx.Client,
    upstream: dict[str, Any],
    model_id: str,
    endpoint_family: str,
    progress_callback=None,
) -> tuple[bool, str | None, str, int, str | None]:
    headers = {"Authorization": f"Bearer {upstream['api_key']}"}

    if endpoint_family == "chat.completions":
        payload = {
            "model": model_id,
            "stream": True,
            "max_completion_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
        target_url = build_upstream_url(upstream["base_url"], "/v1/chat/completions")
    elif endpoint_family == "responses":
        payload = {
            "model": model_id,
            "stream": True,
            "max_output_tokens": 16,
            "input": "hi",
        }
        target_url = build_upstream_url(upstream["base_url"], "/v1/responses")
    else:
        return False, f"Unsupported probe endpoint: {endpoint_family}", endpoint_family, 0, None

    if not _supports_endpoint(upstream, endpoint_family):
        return False, f"Probe request requires {endpoint_family} capability", endpoint_family, 0, None

    if progress_callback:
        progress_callback(
            _step(
                "response_request",
                "running",
                f"正在向 /v1/{'chat/completions' if endpoint_family == 'chat.completions' else 'responses'} 发送流式 hi 探活请求",
                endpoint=endpoint_family,
                model=model_id,
            )
        )

    response = client.send(
        client.build_request("POST", target_url, headers=headers, json=payload),
        stream=True,
    )
    try:
        if response.status_code >= 400:
            detail = response.read().decode("utf-8", errors="ignore").strip()
            if detail:
                detail = detail[:240]
                return False, f"Probe hi request failed: HTTP {response.status_code}: {detail}", endpoint_family, response.status_code, None
            return False, f"Probe hi request failed: HTTP {response.status_code}", endpoint_family, response.status_code, None

        content_type = response.headers.get("content-type", "").lower()
        saw_valid_payload = False
        collected_text_parts: list[str] = []
        buffer = ""

        if "text/event-stream" in content_type:
            if progress_callback:
                progress_callback(
                    _step(
                        "response_stream",
                        "running",
                        "已连通上游，正在等待流式内容",
                        endpoint=endpoint_family,
                        model=model_id,
                    )
                )
            for chunk in response.iter_text():
                if not chunk:
                    continue
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    stripped = line.strip()
                    if not stripped.startswith("data:"):
                        continue
                    data = stripped[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    saw_valid_payload = True
                    text = _extract_text_from_probe_payload(endpoint_family, parsed)
                    if (
                        endpoint_family == "responses"
                        and parsed.get("type") == "response.completed"
                        and collected_text_parts
                    ):
                        text = ""
                    _append_unique_text(collected_text_parts, text)
        else:
            try:
                response_body = response.read()
                payload_obj = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                body_preview = response_body.decode("utf-8", errors="ignore").strip()[:240] if "response_body" in locals() else ""
                if body_preview:
                    return False, f"Probe hi response was not JSON: {body_preview}", endpoint_family, response.status_code, None
                return False, "Probe hi response was not JSON", endpoint_family, response.status_code, None
            if isinstance(payload_obj, dict):
                saw_valid_payload = True
                text = _extract_text_from_probe_payload(endpoint_family, payload_obj)
                _append_unique_text(collected_text_parts, text)

        if not saw_valid_payload:
            return False, "Probe hi response did not contain a valid payload", endpoint_family, response.status_code, None
        full_text = "".join(collected_text_parts).strip()
        if not full_text:
            return False, "Probe hi response did not contain assistant text", endpoint_family, response.status_code, None
        return True, None, endpoint_family, response.status_code, full_text[:240]
    finally:
        response.close()


class HealthProbeService:
    def __init__(self, config_store: ConfigStore, health_store: HealthStore):
        self.config_store = config_store
        self.health_store = health_store
        self._lock = threading.RLock()
        self._scheduler: BackgroundScheduler | None = None
        self._jobs_lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        with self._lock:
            if self._scheduler:
                return
            scheduler = BackgroundScheduler(timezone="UTC")
            self._configure_job(scheduler)
            scheduler.start()
            self._scheduler = scheduler

    def _configure_job(self, scheduler: BackgroundScheduler) -> None:
        app_settings = self.config_store.get_app_settings()
        interval = int(app_settings.get("probe_interval_seconds", 300))
        if bool(app_settings.get("scheduled_probe_all_enabled", False)):
            scheduler.add_job(
                self.probe_all,
                "interval",
                seconds=max(interval, 30),
                id=PROBE_ALL_JOB_ID,
                replace_existing=True,
            )
        elif scheduler.get_job(PROBE_ALL_JOB_ID):
            scheduler.remove_job(PROBE_ALL_JOB_ID)
        scheduler.add_job(
            self.probe_timed_out_upstreams,
            "interval",
            seconds=60,
            id=TIMEOUT_RECOVERY_JOB_ID,
            replace_existing=True,
        )

    def stop(self) -> None:
        with self._lock:
            if not self._scheduler:
                return
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def reload_schedule(self) -> None:
        with self._lock:
            if not self._scheduler:
                return
            self._configure_job(self._scheduler)

    def probe_all(self) -> dict[str, Any]:
        result = {}
        for upstream in self.config_store.list_upstreams():
            if not upstream.get("enabled") or not upstream.get("probe_enabled"):
                continue
            result[upstream["id"]] = self.probe_one(upstream)
        return result

    def probe_timed_out_upstreams(self) -> dict[str, Any]:
        health = self.health_store.get_all()
        result = {}
        for upstream in sorted(self.config_store.list_upstreams(), key=lambda item: int(item.get("order", 9999))):
            if not upstream.get("enabled") or not upstream.get("probe_enabled"):
                continue
            if not health.get(upstream["id"], {}).get("timeout_failover_active"):
                continue
            result[upstream["id"]] = self.probe_one(upstream)
        return result

    def probe_one(
        self,
        upstream: dict[str, Any],
        progress_callback=None,
        probe_model_override: str | None = None,
        probe_endpoint_override: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        steps: list[dict[str, Any]] = []
        probe_model = (probe_model_override or DEFAULT_PROBE_MODEL).strip() or DEFAULT_PROBE_MODEL
        default_probe_endpoint = _default_probe_endpoint_for(upstream)
        probe_endpoint = (probe_endpoint_override or default_probe_endpoint).strip() or default_probe_endpoint
        model_ids: list[str] = []
        final_status_code = None
        success = False
        error_message = None
        hi_excerpt = None
        try:
            with httpx.Client(
                timeout=httpx.Timeout(normalize_timeout_seconds(upstream.get("timeout_ms", 10000))),
            ) as client:
                _record_step(
                    steps,
                    progress_callback,
                    "models_request",
                    "running",
                    "正在请求 /v1/models 获取可探活模型列表",
                )
                models_success = False
                try:
                    models_response = client.get(
                        build_upstream_url(upstream["base_url"], "/v1/models"),
                        headers={"Authorization": f"Bearer {upstream['api_key']}"},
                    )
                    _record_step(
                        steps,
                        progress_callback,
                        "models_request",
                        "completed",
                        f"/v1/models 返回 HTTP {models_response.status_code}",
                        status_code=models_response.status_code,
                    )
                    models_success, models_error, model_ids = _validate_models_probe_response(models_response)
                    final_status_code = models_response.status_code
                except httpx.HTTPError as exc:
                    models_error = str(exc)
                    _record_step(
                        steps,
                        progress_callback,
                        "models_request",
                        "failed",
                        models_error,
                    )

                if models_success:
                    _record_step(
                        steps,
                        progress_callback,
                        "models_validate",
                        "completed",
                        f"模型列表有效，识别到 {len(model_ids)} 个模型",
                        model_count=len(model_ids),
                    )
                else:
                    _record_step(
                        steps,
                        progress_callback,
                        "models_validate",
                        "failed",
                        models_error or "模型列表校验失败，将继续直接探活",
                    )

                if probe_endpoint not in PROBE_ENDPOINTS:
                    success = False
                    error_message = f"Unsupported probe endpoint: {probe_endpoint}"
                    _record_step(
                        steps,
                        progress_callback,
                        "probe_prepare",
                        "failed",
                        error_message,
                        endpoint=probe_endpoint,
                        model=probe_model,
                    )
                elif probe_model:
                    if models_success and probe_model not in model_ids:
                        _record_step(
                            steps,
                            progress_callback,
                            "probe_prepare",
                            "running",
                            f"探活模型 {probe_model} 不在已探测模型列表中，仍尝试直接探活",
                            endpoint=probe_endpoint,
                            model=probe_model,
                        )
                    else:
                        _record_step(
                            steps,
                            progress_callback,
                            "probe_prepare",
                            "completed",
                            f"选择 {probe_endpoint} / {probe_model} 进行流式 hi 探活",
                            endpoint=probe_endpoint,
                            model=probe_model,
                        )

                if probe_endpoint in PROBE_ENDPOINTS and probe_model:
                    _record_step(
                        steps,
                        progress_callback,
                        "probe_model",
                        "completed",
                        f"探活接口: {probe_endpoint}; 探活模型: {probe_model}",
                        endpoint=probe_endpoint,
                        model=probe_model,
                    )
                    success, hi_error, probe_endpoint, hi_status_code, hi_excerpt = _validate_probe_response(
                        client,
                        upstream,
                        probe_model,
                        probe_endpoint,
                        progress_callback=progress_callback,
                    )
                    final_status_code = hi_status_code or final_status_code
                    if not success:
                        error_message = hi_error
                        _record_step(
                            steps,
                            progress_callback,
                            "response_validate",
                            "failed",
                            hi_error or "hi 探活失败",
                            endpoint=probe_endpoint,
                            model=probe_model,
                        )
                    else:
                        _record_step(
                            steps,
                            progress_callback,
                            "response_validate",
                            "completed",
                            f"已收到正常回复: {hi_excerpt or 'assistant text'}",
                            endpoint=probe_endpoint,
                            model=probe_model,
                            excerpt=hi_excerpt,
                        )
                elif not error_message:
                    error_message = "Probe could not determine a valid probe model or endpoint"
                    _record_step(
                        steps,
                        progress_callback,
                        "probe_prepare",
                        "failed",
                        error_message,
                    )
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            payload = {
                "last_probe_success": success,
                "last_probe_status_code": final_status_code,
                "last_probe_latency_ms": latency_ms,
                "last_probe_model_count": len(model_ids),
                "last_probe_model_used": probe_model,
                "last_probe_endpoint": probe_endpoint,
                "last_probe_reply_excerpt": hi_excerpt if success else None,
                "last_probe_error": error_message,
                "last_probe_steps": steps,
                "last_probe_at": utc_now_iso(),
            }
            _record_step(
                steps,
                progress_callback,
                "finished",
                "completed" if success else "failed",
                "探活完成" if success else f"探活失败: {error_message}",
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            _record_step(
                steps,
                progress_callback,
                "exception",
                "failed",
                str(exc),
            )
            payload = {
                "last_probe_success": False,
                "last_probe_status_code": None,
                "last_probe_latency_ms": latency_ms,
                "last_probe_model_count": 0,
                "last_probe_model_used": None,
                "last_probe_endpoint": None,
                "last_probe_reply_excerpt": None,
                "last_probe_error": str(exc),
                "last_probe_steps": steps,
                "last_probe_at": utc_now_iso(),
            }
        self.health_store.update_probe(upstream["id"], payload)
        return payload

    def start_probe_job(
        self,
        upstreams: list[dict[str, Any]],
        probe_models: dict[str, str] | None = None,
        probe_endpoints: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        job_id = f"probe_{secrets.token_hex(4)}"
        probe_models = probe_models or {}
        probe_endpoints = probe_endpoints or {}
        with self._jobs_lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": utc_now_iso(),
                "started_at": None,
                "completed_at": None,
                "total": len(upstreams),
                "completed_count": 0,
                "current_upstream_id": None,
                "current_upstream_name": None,
                "probe_models": probe_models,
                "probe_endpoints": probe_endpoints,
                "items": {
                    upstream["id"]: {
                        "upstream_id": upstream["id"],
                        "upstream_name": upstream["name"],
                        "requested_probe_model": probe_models.get(upstream["id"]) or DEFAULT_PROBE_MODEL,
                        "requested_probe_endpoint": probe_endpoints.get(upstream["id"]) or _default_probe_endpoint_for(upstream),
                        "status": "pending",
                        "steps": [],
                        "result": None,
                    }
                    for upstream in upstreams
                },
            }
        thread = threading.Thread(
            target=self._run_probe_job,
            args=(job_id, upstreams, probe_models, probe_endpoints),
            daemon=True,
        )
        thread.start()
        return self.get_probe_job(job_id) or {"id": job_id, "status": "queued"}

    def get_probe_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def _run_probe_job(
        self,
        job_id: str,
        upstreams: list[dict[str, Any]],
        probe_models: dict[str, str],
        probe_endpoints: dict[str, str],
    ) -> None:
        with self._jobs_lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = utc_now_iso()

        for upstream in upstreams:
            def callback(entry: dict[str, Any], upstream_id=upstream["id"], upstream_name=upstream["name"]):
                with self._jobs_lock:
                    job = self._jobs[job_id]
                    item = job["items"][upstream_id]
                    item["steps"].append(entry)
                    item["status"] = "running" if entry["status"] == "running" else item["status"]
                    job["current_upstream_id"] = upstream_id
                    job["current_upstream_name"] = upstream_name

            result = self.probe_one(
                upstream,
                progress_callback=callback,
                probe_model_override=probe_models.get(upstream["id"]),
                probe_endpoint_override=probe_endpoints.get(upstream["id"]),
            )
            with self._jobs_lock:
                job = self._jobs[job_id]
                item = job["items"][upstream["id"]]
                item["result"] = result
                item["status"] = "completed" if result.get("last_probe_success") else "failed"
                job["completed_count"] += 1

        with self._jobs_lock:
            job = self._jobs[job_id]
            job["status"] = "completed"
            job["completed_at"] = utc_now_iso()
