from __future__ import annotations

import copy
import hashlib
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from relay.settings import (
    ADMIN_PASSWORD,
    ADMIN_SECRET_KEY,
    ADMIN_USERNAME,
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_DIRNAME,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def hash_proxy_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    config = _deep_merge(DEFAULT_CONFIG, raw)
    # Discard legacy deadlines on load and save, including existing deployments.
    config["app"].pop("request_timeout_ms", None)
    for upstream in config["upstreams"]:
        upstream.pop("timeout_ms", None)
    return config


def normalize_model_mappings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for source, target in value.items():
        source_model = str(source or "").strip()
        target_model = str(target or "").strip()
        if not source_model or not target_model or source_model == target_model:
            continue
        normalized[source_model] = target_model
    return normalized


class ConfigStore:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.data_dir = self.config_path.parent / DEFAULT_DATA_DIRNAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.config_path.exists():
            self._write_yaml(DEFAULT_CONFIG)
        self._config = self._load_yaml()

    @staticmethod
    def _file_signature(stat: os.stat_result) -> tuple[int, int, int, int]:
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _load_yaml(self) -> dict[str, Any]:
        with self.config_path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
            config = _normalize_config(raw)
            self._signature = self._file_signature(os.fstat(handle.fileno()))
        return config

    def _current_config(self) -> dict[str, Any]:
        if self._signature != self._file_signature(self.config_path.stat()):
            self._config = self._load_yaml()
        return self._config

    def _write_yaml(self, payload: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with NamedTemporaryFile(dir=self.config_path.parent, mode="w", encoding="utf-8", delete=False) as handle:
                temp_path = Path(handle.name)
                yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                signature = self._file_signature(os.fstat(handle.fileno()))
            temp_path.replace(self.config_path)
            self._signature = signature
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @property
    def admin_settings(self) -> dict[str, Any]:
        return {
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "secret_key": ADMIN_SECRET_KEY,
        }

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._current_config())

    def save_config(self, new_config: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = _normalize_config(new_config)
            self._write_yaml(merged)
            self._config = merged
            return copy.deepcopy(self._config)

    def get_app_settings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._current_config()["app"])

    def update_app_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        patch = dict(patch)
        if "model_mappings" in patch:
            patch["model_mappings"] = normalize_model_mappings(patch["model_mappings"])
        with self._lock:
            config = self.get_config()
            config["app"].update(patch)
            return self.save_config(config)["app"]

    def list_upstreams(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._current_config()["upstreams"])

    def save_upstreams(self, upstreams: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            config = self.get_config()
            existing = {item["id"]: item for item in config["upstreams"] if item.get("id")}
            normalized: list[dict[str, Any]] = []
            for order, upstream in enumerate(upstreams, start=1):
                current = existing.get(upstream.get("id", ""))
                normalized.append(
                    {
                        "id": upstream.get("id") or f"upstream_{secrets.token_hex(4)}",
                        "name": upstream.get("name", "").strip() or f"Upstream {order}",
                        "base_url": upstream.get("base_url", "").strip().rstrip("/"),
                        "api_key": upstream.get("api_key", "") or (current or {}).get("api_key", ""),
                        "enabled": bool(upstream.get("enabled", True)),
                        "order": int(upstream.get("order") or order),
                        "allowed_models": [
                            item.strip()
                            for item in upstream.get("allowed_models", [])
                            if str(item).strip()
                        ],
                        "probe_enabled": bool(upstream.get("probe_enabled", True)),
                        "endpoint_capabilities": upstream.get(
                            "endpoint_capabilities",
                            (current or {}).get("endpoint_capabilities", ["*"]),
                        )
                        or ["*"],
                        "models_detected_at": upstream.get(
                            "models_detected_at",
                            (current or {}).get("models_detected_at"),
                        ),
                        "updated_at": _utc_now_iso(),
                        "created_at": (current or {}).get("created_at", _utc_now_iso()),
                    }
                )
            config["upstreams"] = sorted(normalized, key=lambda item: item["order"])
            return self.save_config(config)["upstreams"]

    def list_proxy_keys(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._current_config()["proxy_keys"])

    def create_proxy_key(self, name: str, note: str = "") -> tuple[dict[str, Any], str]:
        raw_key = f"sk-relay-{secrets.token_urlsafe(24)}"
        key_id = f"key_{secrets.token_hex(4)}"
        metadata = {
            "id": key_id,
            "name": name.strip() or "未命名 Key",
            "note": note.strip(),
            "enabled": True,
            "key_prefix": f"{raw_key[:14]}...",
            "key_hash": hash_proxy_key(raw_key),
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "last_used_at": None,
        }
        with self._lock:
            config = self.get_config()
            config["proxy_keys"].append(metadata)
            self.save_config(config)
        return metadata, raw_key

    def update_proxy_key(self, key_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            config = self.get_config()
            updated = None
            for item in config["proxy_keys"]:
                if item["id"] != key_id:
                    continue
                item["name"] = patch.get("name", item["name"]).strip() or item["name"]
                item["note"] = patch.get("note", item["note"]).strip()
                if "enabled" in patch:
                    item["enabled"] = bool(patch["enabled"])
                item["updated_at"] = _utc_now_iso()
                updated = item
                break
            if not updated:
                return None
            self.save_config(config)
            return updated

    def get_proxy_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._current_config()["proxy_keys"]:
                if item.get("key_hash") == key_hash:
                    return copy.deepcopy(item)
        return None

    def mark_proxy_key_used(self, key_id: str) -> None:
        with self._lock:
            now = datetime.now(timezone.utc)
            current = self._current_config()
            for index, item in enumerate(current["proxy_keys"]):
                if item["id"] != key_id:
                    continue
                last_used = item.get("last_used_at")
                if isinstance(last_used, str):
                    try:
                        previous = datetime.fromisoformat(last_used)
                        if previous.tzinfo and now - previous < timedelta(minutes=1):
                            return
                    except ValueError:
                        pass
                config = copy.deepcopy(current)
                config["proxy_keys"][index]["last_used_at"] = now.isoformat()
                self.save_config(config)
                return

    def masked_upstreams(self) -> list[dict[str, Any]]:
        masked = []
        for item in self.list_upstreams():
            copied = copy.deepcopy(item)
            copied["api_key"] = ""
            copied["api_key_masked"] = f"{item['api_key'][:8]}..." if item.get("api_key") else ""
            masked.append(copied)
        return masked

    def sanitized_proxy_keys(self) -> list[dict[str, Any]]:
        items = []
        for item in self.list_proxy_keys():
            copied = copy.deepcopy(item)
            copied.pop("key_hash", None)
            items.append(copied)
        return items
