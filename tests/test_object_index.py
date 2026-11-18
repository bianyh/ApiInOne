from concurrent.futures import ThreadPoolExecutor

from relay.routing import resolve_bound_upstream
from relay.storage import ObjectIndexStore


def test_migrates_complete_entries_from_truncated_index(app):
    directory = app.extensions["relay_state"].config_store.data_dir / "legacy"
    directory.mkdir()
    old_index = directory / "object-index.json"
    raw = (
        '{"file_existing": {"object_id": "file_existing", "upstream_id": "first",'
        '"object_type": "file", "updated_at": "2026-11-18T00:00:00+00:00"}, "resp_cut'
    )
    old_index.write_text(raw, encoding="utf-8")

    store = ObjectIndexStore(directory)
    assert resolve_bound_upstream("/v1/files/file_existing", store) == ("first", False)
    assert resolve_bound_upstream("/v1/files/resp_cut", store) == (None, True)
    assert resolve_bound_upstream("/v1/chat/completions", store) == (None, False)
    assert resolve_bound_upstream("/v1/responses/compact", store) == (None, False)
    assert old_index.read_text(encoding="utf-8") == raw

    store.upsert("file_existing", "second")
    reopened = ObjectIndexStore(directory)
    assert reopened.get("file_existing")["upstream_id"] == "second"
    assert reopened.get("file_existing")["object_type"] == "file"


def test_different_store_instances_keep_concurrent_upserts(app):
    state = app.extensions["relay_state"]
    stores = [state.object_store, ObjectIndexStore(state.config_store.data_dir)]

    def write(number):
        stores[number % 2].upsert(f"file_{number}", "upstream", "file")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    assert all(stores[0].get(f"file_{number}") for number in range(40))
