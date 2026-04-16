from __future__ import annotations

from io import BytesIO

from tests.conftest import provision_relay


def test_models_endpoint_returns_merged_models(client, app, upstream_factory):
    first = upstream_factory("first", ["gpt-4.1", "text-embedding-3-large"])
    second = upstream_factory("second", ["gpt-4.1-mini"])
    proxy_key = provision_relay(app, [first, second])

    response = client.get("/v1/models", headers={"Authorization": f"Bearer {proxy_key}"})
    assert response.status_code == 200
    model_ids = {item["id"] for item in response.get_json()["data"]}
    assert model_ids == {"gpt-4.1", "text-embedding-3-large"}


def test_proxy_json_request_and_usage_logging(client, app, upstream_factory):
    upstream = upstream_factory("primary", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["id"] == "resp_primary"

    stats = client.get("/admin/api/session")
    assert stats.status_code == 200

    records = app.extensions["relay_state"].request_store.query(page=1, page_size=10)
    assert records["total"] == 1
    item = records["items"][0]
    assert item["input_tokens"] == 20
    assert item["cached_tokens"] == 4
    assert item["reasoning_tokens"] == 3


def test_proxy_failover_before_first_byte(client, app, upstream_factory):
    failing = upstream_factory("failing", ["gpt-4.1"], fail_responses=1)
    healthy = upstream_factory("healthy", ["gpt-4.1"])
    proxy_key = provision_relay(app, [failing, healthy])

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 200
    assert response.get_json()["id"] == "resp_healthy"

    records = app.extensions["relay_state"].request_store.query(page=1, page_size=10)
    assert records["total"] == 1
    assert records["items"][0]["upstream_name"] == "healthy"


def test_streaming_proxy(client, app, upstream_factory):
    upstream = upstream_factory("streamer", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello", "stream": True},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["Content-Type"]
    body = response.get_data(as_text=True)
    assert "response.completed" in body

    records = app.extensions["relay_state"].request_store.query(page=1, page_size=10)
    assert records["items"][0]["ttfb_ms"] is not None


def test_multipart_file_upload_and_index_lookup(client, app, upstream_factory):
    upstream = upstream_factory("files", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])

    create_response = client.post(
        "/v1/files",
        data={"purpose": "assistants", "file": (BytesIO(b"hello"), "hello.txt")},
        headers={"Authorization": f"Bearer {proxy_key}"},
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 200
    file_id = create_response.get_json()["id"]

    get_response = client.get(
        f"/v1/files/{file_id}",
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert get_response.status_code == 200
    assert get_response.get_json()["id"] == file_id


def test_timeout_failover_skips_timed_out_higher_priority_upstream(client, app, upstream_factory):
    timed_out = upstream_factory(
        "timed_out",
        ["gpt-4.1"],
        response_delay_seconds=0.25,
    )
    healthy = upstream_factory("healthy", ["gpt-4.1"])
    state = app.extensions["relay_state"]
    state.config_store.save_upstreams(
        [
            {
                "id": "timed_out_id",
                "name": "timed_out",
                "base_url": timed_out.base_url,
                "api_key": "up-timed",
                "enabled": True,
                "order": 1,
                "allowed_models": ["gpt-4.1"],
                "endpoint_capabilities": ["*"],
                "timeout_ms": 50,
                "probe_enabled": True,
            },
            {
                "id": "healthy_id",
                "name": "healthy",
                "base_url": healthy.base_url,
                "api_key": "up-healthy",
                "enabled": True,
                "order": 2,
                "allowed_models": ["gpt-4.1"],
                "endpoint_capabilities": ["*"],
                "timeout_ms": 5000,
                "probe_enabled": True,
            },
        ]
    )
    _, proxy_key = state.config_store.create_proxy_key("pytest")

    first = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert first.status_code == 200
    assert first.get_json()["id"] == "resp_healthy"
    assert timed_out.responses_calls == 1
    assert healthy.responses_calls == 1
    health = state.health_store.get_all()["timed_out_id"]
    assert health["timeout_failover_active"] is True

    second = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "again"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert second.status_code == 200
    assert second.get_json()["id"] == "resp_healthy"
    assert timed_out.responses_calls == 1
    assert healthy.responses_calls == 2


def test_recovery_probe_restores_higher_priority_upstream_after_timeout(client, app, upstream_factory):
    recovering = upstream_factory(
        "recovering",
        ["gpt-4.1", "gpt-5.4"],
        response_delay_seconds=0.25,
        chat_delay_seconds=0.0,
    )
    healthy = upstream_factory("healthy", ["gpt-4.1", "gpt-5.4"])
    state = app.extensions["relay_state"]
    state.config_store.save_upstreams(
        [
            {
                "id": "recovering_id",
                "name": "recovering",
                "base_url": recovering.base_url,
                "api_key": "up-recovering",
                "enabled": True,
                "order": 1,
                "allowed_models": ["gpt-4.1", "gpt-5.4"],
                "endpoint_capabilities": ["chat.completions", "responses"],
                "timeout_ms": 50,
                "probe_enabled": True,
            },
            {
                "id": "healthy_id",
                "name": "healthy",
                "base_url": healthy.base_url,
                "api_key": "up-healthy",
                "enabled": True,
                "order": 2,
                "allowed_models": ["gpt-4.1", "gpt-5.4"],
                "endpoint_capabilities": ["chat.completions", "responses"],
                "timeout_ms": 5000,
                "probe_enabled": True,
            },
        ]
    )
    _, proxy_key = state.config_store.create_proxy_key("pytest")

    first = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert first.status_code == 200
    assert first.get_json()["id"] == "resp_healthy"
    assert state.health_store.get_all()["recovering_id"]["timeout_failover_active"] is True

    recovery = state.health_service.probe_timed_out_upstreams()
    assert recovery["recovering_id"]["last_probe_success"] is True
    assert recovery["recovering_id"]["last_probe_model_used"] == "gpt-5.4"
    assert state.health_store.get_all()["recovering_id"]["timeout_failover_active"] is False

    recovering.response_delay_seconds = 0.0
    second = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "again"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert second.status_code == 200
    assert second.get_json()["id"] == "resp_recovering"
