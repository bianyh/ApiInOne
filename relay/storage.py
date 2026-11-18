from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


@contextmanager
def _sqlite_connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, timeout=10)) as connection, connection:
        connection.row_factory = sqlite3.Row
        yield connection


class RequestLogStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "requests.sqlite3"
        with _sqlite_connection(self.path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                connection.execute(
                    """
                    CREATE TABLE requests (
                        id INTEGER PRIMARY KEY,
                        log_day TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        proxy_key_id TEXT,
                        upstream_id TEXT,
                        model TEXT,
                        proxy_key_name TEXT,
                        upstream_name TEXT,
                        status_code INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        duration_ms REAL NOT NULL,
                        ttfb_ms REAL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                connection.executemany(self._insert, self._legacy_rows())
                connection.execute("CREATE INDEX requests_day ON requests(log_day)")
                connection.execute("PRAGMA user_version = 1")

    _insert = """
        INSERT INTO requests (
            log_day, created_at, proxy_key_id, upstream_id, model, proxy_key_name,
            upstream_name, status_code, total_tokens, duration_ms, ttfb_ms, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    @staticmethod
    def _row(day: str, record: dict[str, Any], payload: str) -> tuple:
        return (
            day, record.get("created_at") or "", record.get("proxy_key_id"),
            record.get("upstream_id"), record.get("model"), record.get("proxy_key_name"),
            record.get("upstream_name"), int(record.get("status_code") or 0),
            int(record.get("total_tokens") or 0), float(record.get("duration_ms") or 0),
            record.get("ttfb_ms"), payload,
        )

    def _legacy_rows(self) -> Iterator[tuple]:
        for path in sorted(self.data_dir.glob("requests-????-??-??.jsonl"), reverse=True):
            day = path.stem.removeprefix("requests-")
            skipped = 0
            with path.open("rb") as handle:
                for raw_line in handle:
                    if not raw_line.strip():
                        continue
                    try:
                        line = raw_line.decode("utf-8")
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("expected a log record")
                        row = self._row(day, record, line)
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    yield row
            if skipped:
                logger.warning("Skipped %d damaged request log entries in %s", skipped, path)

    def append(self, record: dict[str, Any]) -> None:
        day = _utc_now().date().isoformat()
        with _sqlite_connection(self.path) as connection:
            connection.execute(self._insert, self._row(day, record, json.dumps(record, ensure_ascii=False)))

    @staticmethod
    def _date_window(days: int) -> tuple[str, str]:
        today = _utc_now().date()
        return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()

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
        clauses = ["log_day BETWEEN ? AND ?"]
        parameters = list(self._date_window(days))
        for column, value in (("proxy_key_id", proxy_key_id), ("upstream_id", upstream_id), ("model", model)):
            if value:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if status_group in {"success", "error"}:
            clauses.append("status_code < 400" if status_group == "success" else "status_code >= 400")
        where = " AND ".join(clauses)
        with _sqlite_connection(self.path) as connection:
            connection.execute("BEGIN")
            total = connection.execute(f"SELECT count(*) FROM requests WHERE {where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"""SELECT payload FROM requests WHERE {where}
                    ORDER BY created_at DESC, id LIMIT ? OFFSET ?""",
                [*parameters, page_size, max(page - 1, 0) * page_size],
            )
            return {"items": [json.loads(row["payload"]) for row in rows], "total": total}

    def build_summary(self, days: int = 7) -> dict[str, Any]:
        window = self._date_window(days)
        with _sqlite_connection(self.path) as connection:
            connection.execute("BEGIN")
            summary = dict(connection.execute(
                """
                SELECT count(*) AS total_requests,
                       count(CASE WHEN status_code < 400 THEN 1 END) AS success_requests,
                       count(CASE WHEN status_code >= 400 THEN 1 END) AS error_requests,
                       coalesce(sum(total_tokens), 0) AS total_tokens,
                       coalesce(round(avg(duration_ms), 2), 0) AS avg_latency_ms,
                       coalesce(round(avg(ttfb_ms), 2), 0) AS avg_ttfb_ms
                FROM requests WHERE log_day BETWEEN ? AND ?
                """,
                window,
            ).fetchone())
            series = connection.execute(
                """
                SELECT substr(created_at, 1, 13) || ':00' AS time, count(*) AS count
                FROM requests WHERE log_day BETWEEN ? AND ? AND created_at <> ''
                GROUP BY time ORDER BY time
                """,
                window,
            )
            latency = connection.execute(
                """
                SELECT upstream_name AS name, round(avg(ttfb_ms), 2) AS value
                FROM requests WHERE log_day BETWEEN ? AND ? AND upstream_name <> '' AND ttfb_ms IS NOT NULL
                GROUP BY name ORDER BY name
                """,
                window,
            )
            result = {
                "summary": summary,
                "timeseries": [dict(row) for row in series],
                "latency_by_upstream": [dict(row) for row in latency],
            }
            for name, column in (
                ("top_models", "model"),
                ("top_upstreams", "upstream_name"),
                ("top_proxy_keys", "proxy_key_name"),
                ("status_breakdown", "cast(nullif(status_code, 0) as text)"),
            ):
                limit = "" if name == "status_breakdown" else "LIMIT 8"
                rows = connection.execute(
                    f"""
                    SELECT coalesce(nullif({column}, ''), 'unknown') AS name, count(*) AS value
                    FROM requests WHERE log_day BETWEEN ? AND ?
                    GROUP BY name ORDER BY value DESC, name {limit}
                    """,
                    window,
                )
                result[name] = [dict(row) for row in rows]
            return result


class ObjectIndexStore:
    def __init__(self, data_dir: str | Path):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "object-index.sqlite3"
        with _sqlite_connection(self.path) as connection:
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
            with _sqlite_connection(self.path) as connection:
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
        with _sqlite_connection(self.path) as connection:
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

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            if not isinstance(payload, dict) or any(not isinstance(item, dict) for item in payload.values()):
                raise ValueError("expected an object of health records")
            return payload
        except FileNotFoundError:
            return {}
        except ValueError:
            backup = self.path.with_name(f"health.corrupt-{_utc_now():%Y%m%d%H%M%S%f}.json")
            self.path.replace(backup)
            logger.warning("Preserved damaged health records at %s", backup)
            return {}

    def _save(self, payload: dict[str, Any]) -> None:
        temp_path = None
        try:
            with NamedTemporaryFile(dir=self.data_dir, mode="w", encoding="utf-8", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            temp_path.replace(self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

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
