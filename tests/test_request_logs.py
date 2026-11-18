import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from relay.storage import RequestLogStore


def test_migrates_legacy_logs_and_paginates(app):
    directory = app.extensions["relay_state"].config_store.data_dir / "archive"
    directory.mkdir()
    now = datetime.now(timezone.utc)
    entries = [
        {"created_at": now.replace(hour=13).isoformat(), "model": "other", "status_code": 404},
        {"created_at": now.replace(hour=7).isoformat(), "model": "test", "status_code": 200, "ttfb_ms": 28},
        {"created_at": now.replace(hour=9).isoformat(), "model": "test", "status_code": 200, "ttfb_ms": 32},
    ]
    archive = directory / f"requests-{now:%Y-%m-%d}.jsonl"
    source = "\n".join(json.dumps(entry) for entry in entries) + '\n{"truncated"'
    archive.write_text(source, encoding="utf-8")

    store = RequestLogStore(directory)
    assert store.query(page=2, page_size=1, model="test") == {"items": [entries[1]], "total": 2}
    assert store.query(status_group="error")["items"] == [entries[0]]
    assert store.build_summary()["summary"]["avg_ttfb_ms"] == 30
    assert RequestLogStore(directory).query()["total"] == 3
    assert archive.read_text(encoding="utf-8") == source


def test_concurrent_config_changes_keep_proxy_keys(app):
    config = app.extensions["relay_state"].config_store

    def create(index):
        item, _ = config.create_proxy_key(f"key-{index}")
        config.mark_proxy_key_used(item["id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(create, range(12)))

    keys = config.list_proxy_keys()
    assert len(keys) == 12
    assert all(key["last_used_at"] for key in keys)
