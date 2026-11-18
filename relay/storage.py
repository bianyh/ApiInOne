from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


class RequestLogStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _log_path_for(self, dt: datetime) -> Path:
        return self.data_dir / f"requests-{dt.strftime('%Y-%m-%d')}.jsonl"

    def _metrics_path_for(self, dt: datetime) -> Path:
        return self.data_dir / f"metrics-{dt.strftime('%Y-%m-%d')}.json"

    def append(self, record: dict[str, Any]) -> None:
        now = _utc_now()
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self._log_path_for(now).open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
            self._append_metrics(now, record)

    def _append_metrics(self, dt: datetime, record: dict[str, Any]) -> None:
        path = self._metrics_path_for(dt)
        if path.exists():
            metrics = json.loads(path.read_text(encoding="utf-8"))
        else:
            metrics = {
                "date": dt.strftime("%Y-%m-%d"),
                "total_requests": 0,
                "total_tokens": 0,
                "success_requests": 0,
                "error_requests": 0,
                "by_model": {},
                "by_upstream": {},
                "by_proxy_key": {},
                "by_status": {},
            }

        metrics["total_requests"] += 1
        metrics["total_tokens"] += int(record.get("total_tokens") or 0)
        if int(record.get("status_code") or 0) < 400:
            metrics["success_requests"] += 1
        else:
            metrics["error_requests"] += 1

        for field, key in (
            ("by_model", record.get("model") or "unknown"),
            ("by_upstream", record.get("upstream_name") or "unknown"),
            ("by_proxy_key", record.get("proxy_key_name") or "unknown"),
            ("by_status", str(record.get("status_code") or "unknown")),
        ):
            metrics[field][key] = int(metrics[field].get(key, 0)) + 1

        path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    def _iter_records(self, days: int = 30) -> list[dict[str, Any]]:
        now = _utc_now()
        records: list[dict[str, Any]] = []
        with self._lock:
            for offset in range(days):
                dt = now - timedelta(days=offset)
                path = self._log_path_for(dt)
                if not path.exists():
                    continue
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    records.append(json.loads(line))
        records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return records

    def query(
        self,
        page: int = 1,
        page_size: int = 50,
        proxy_key_id: str | None = None,
        upstream_id: str | None = None,
        model: str | None = None,
        status_group: str | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        records = self._iter_records(days=days)
        filtered = []
        for item in records:
            if proxy_key_id and item.get("proxy_key_id") != proxy_key_id:
                continue
            if upstream_id and item.get("upstream_id") != upstream_id:
                continue
            if model and item.get("model") != model:
                continue
            if status_group == "success" and int(item.get("status_code") or 0) >= 400:
                continue
            if status_group == "error" and int(item.get("status_code") or 0) < 400:
                continue
            filtered.append(item)

        total = len(filtered)
        start = max(page - 1, 0) * page_size
        end = start + page_size
        return {"items": filtered[start:end], "total": total}

    def build_summary(self, days: int = 7) -> dict[str, Any]:
        records = self._iter_records(days=days)
        total_requests = len(records)
        success = sum(1 for item in records if int(item.get("status_code") or 0) < 400)
        error = total_requests - success
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in records)
        avg_latency_ms = round(
            sum(float(item.get("duration_ms") or 0) for item in records) / total_requests,
            2,
        ) if total_requests else 0
        avg_ttfb_ms = round(
            sum(float(item.get("ttfb_ms") or 0) for item in records if item.get("ttfb_ms") is not None)
            / max(sum(1 for item in records if item.get("ttfb_ms") is not None), 1),
            2,
        ) if total_requests else 0

        by_hour: dict[str, int] = defaultdict(int)
        latency_by_upstream: dict[str, list[float]] = defaultdict(list)
        by_model: Counter[str] = Counter()
        by_upstream: Counter[str] = Counter()
        by_proxy_key: Counter[str] = Counter()
        by_status: Counter[str] = Counter()

        for item in records:
            created_at = item.get("created_at")
            if created_at:
                by_hour[created_at[:13] + ":00"] += 1
            by_model[item.get("model") or "unknown"] += 1
            by_upstream[item.get("upstream_name") or "unknown"] += 1
            by_proxy_key[item.get("proxy_key_name") or "unknown"] += 1
            by_status[str(item.get("status_code") or "unknown")] += 1
            if item.get("upstream_name"):
                latency_by_upstream[item["upstream_name"]].append(float(item.get("ttfb_ms") or 0))

        latency_summary = {
            name: round(sum(values) / len(values), 2) if values else 0
            for name, values in latency_by_upstream.items()
        }

        return {
            "summary": {
                "total_requests": total_requests,
                "success_requests": success,
                "error_requests": error,
                "total_tokens": total_tokens,
                "avg_latency_ms": avg_latency_ms,
                "avg_ttfb_ms": avg_ttfb_ms,
            },
            "timeseries": [
                {"time": key, "count": by_hour[key]}
                for key in sorted(by_hour.keys())
            ],
            "top_models": [{"name": key, "value": value} for key, value in by_model.most_common(8)],
            "top_upstreams": [{"name": key, "value": value} for key, value in by_upstream.most_common(8)],
            "top_proxy_keys": [{"name": key, "value": value} for key, value in by_proxy_key.most_common(8)],
            "status_breakdown": [{"name": key, "value": value} for key, value in by_status.items()],
            "latency_by_upstream": [
                {"name": key, "value": value}
                for key, value in sorted(latency_summary.items(), key=lambda item: item[0])
            ],
        }


class ObjectIndexStore:
    def __init__(self, data_dir: str | Path):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "object-index.sqlite3"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                connection.execute(
                    """
                    CREATE TABLE object_index (
                        object_id TEXT PRIMARY KEY NOT NULL,
                        upstream_id TEXT NOT NULL,
                        object_type TEXT,
                        updated_at TEXT
                    )
                    """
                )
                self._import_legacy(connection, directory / "object-index.json")
                connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.row_factory = sqlite3.Row
            yield connection

    @staticmethod
    def _import_legacy(connection: sqlite3.Connection, path: Path) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raw = data[:exc.start].decode("utf-8")
        try:
            entries = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            entries = _recover_object_index(raw)
            logger.warning("Recovered %d complete entries from damaged object index %s", len(entries), path)
        if not isinstance(entries, dict):
            logger.warning("Expected an object in legacy object index %s", path)
            return

        connection.executemany(
            "INSERT INTO object_index VALUES (?, ?, ?, ?)",
            (
                (
                    object_id,
                    entry["upstream_id"],
                    entry.get("object_type") if isinstance(entry.get("object_type"), str) else None,
                    entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else None,
                )
                for object_id, entry in entries.items()
                if object_id and isinstance(entry, dict)
                and isinstance(entry.get("upstream_id"), str) and entry["upstream_id"]
            ),
        )

    def get(self, object_id: str) -> dict[str, Any] | None:
        return self.match_from_segments([object_id])

    def upsert(self, object_id: str, upstream_id: str, object_type: str | None = None) -> None:
        self.upsert_many([(object_id, object_type)], upstream_id)

    def upsert_many(self, objects: Iterable[tuple[str, str | None]], upstream_id: str) -> None:
        if not upstream_id:
            return
        now = _utc_now_iso()
        rows = [
            (object_id, upstream_id, object_type or None, now)
            for object_id, object_type in objects
            if object_id
        ]
        if rows:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO object_index VALUES (?, ?, ?, ?)
                    ON CONFLICT(object_id) DO UPDATE SET
                        upstream_id = excluded.upstream_id,
                        object_type = COALESCE(excluded.object_type, object_index.object_type),
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )

    def match_from_segments(self, segments: list[str]) -> dict[str, Any] | None:
        with self._connect() as connection:
            for segment in segments:
                row = connection.execute(
                    "SELECT * FROM object_index WHERE object_id = ?", (segment,)
                ).fetchone()
                if row is not None:
                    return dict(row)
        return None


def _recover_object_index(raw: str) -> dict[str, Any]:
    # The old writer saved a single JSON object. Decode complete entries before the damaged tail.
    decoder = json.JSONDecoder()
    whitespace = re.compile(r"\s*")
    position = whitespace.match(raw).end()
    entries = {}
    if raw[position:position + 1] != "{":
        return entries

    while True:
        try:
            key, position = decoder.raw_decode(raw, whitespace.match(raw, position + 1).end())
            position = whitespace.match(raw, position).end()
            if not isinstance(key, str) or raw[position:position + 1] != ":":
                break
            value, position = decoder.raw_decode(raw, whitespace.match(raw, position + 1).end())
        except json.JSONDecodeError:
            break
        entries[key] = value
        position = whitespace.match(raw, position).end()
        if raw[position:position + 1] != ",":
            break
    return entries


class HealthStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "health.json"
        self._lock = threading.RLock()
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def update_probe(self, upstream_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(upstream_id, {})
            data[upstream_id].update(payload)
            if payload.get("last_probe_success") is True:
                data[upstream_id]["timeout_failover_active"] = False
                data[upstream_id]["timeout_failover_reason"] = None
                data[upstream_id]["timeout_failover_since"] = None
            data[upstream_id]["updated_at"] = _utc_now_iso()
            self._save(data)

    def mark_timeout_failover(self, upstream_id: str, reason: str) -> None:
        with self._lock:
            data = self._load()
            entry = data.setdefault(upstream_id, {})
            entry["timeout_failover_active"] = True
            entry["timeout_failover_reason"] = reason
            entry["timeout_failover_since"] = _utc_now_iso()
            entry["updated_at"] = _utc_now_iso()
            self._save(data)

    def record_passive_result(self, upstream_id: str, success: bool, ttfb_ms: float | None) -> None:
        with self._lock:
            data = self._load()
            entry = data.setdefault(upstream_id, {})
            entry.setdefault("passive_total", 0)
            entry.setdefault("passive_success", 0)
            entry.setdefault("passive_error", 0)
            entry.setdefault("passive_ttfb_samples", [])
            entry["passive_total"] += 1
            if success:
                entry["passive_success"] += 1
            else:
                entry["passive_error"] += 1
            if ttfb_ms is not None:
                samples = entry["passive_ttfb_samples"]
                samples.append(round(float(ttfb_ms), 2))
                entry["passive_ttfb_samples"] = samples[-50:]
                entry["passive_avg_ttfb_ms"] = round(
                    sum(entry["passive_ttfb_samples"]) / len(entry["passive_ttfb_samples"]),
                    2,
                )
            entry["updated_at"] = _utc_now_iso()
            self._save(data)
