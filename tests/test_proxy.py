from __future__ import annotations

import gzip
import json
import socket
from io import BytesIO

import httpx
import pytest
import yaml
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request as WsgiRequest

from tests.conftest import provision_relay


def _unused_local_base_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_models_endpoint_returns_merged_models(client, app, upstream_factory):
    first = upstream_factory("first", ["gpt-4.1", "text-embedding-3-large"])
    second = upstream_factory("second", ["gpt-4.1-mini"])
    proxy_key = provision_relay(app, [first, second])

    response = client.get("/v1/models", headers={"Authorization": f"Bearer {proxy_key}"})
    assert response.status_code == 200
    model_ids = {item["id"] for item in response.get_json()["data"]}
    assert model_ids == {"gpt-4.1", "text-embedding-3-large", "gpt-4.1-mini"}


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


@pytest.mark.parametrize("phase", ["headers", "json_body", "stream"])
def test_slow_upstream_ignores_legacy_deadlines(client, app, upstream_factory, phase):
    upstream = upstream_factory(
        "slow",
        ["gpt-4.1"],
        chat_delay_seconds=0.2 if phase == "headers" else 0.0,
        response_delay_seconds=0.2 if phase == "json_body" else 0.0,
        stream_delay_seconds=0.2 if phase == "stream" else 0.0,
    )
    proxy_key = provision_relay(app, [upstream])
    state = app.extensions["relay_state"]
    # Simulate an existing deployment, including an on-disk configuration reload.
    config = state.config_store.get_config()
    config["app"]["request_timeout_ms"] = 50
    config["upstreams"][0]["timeout_ms"] = 50
    state.config_store.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    response = client.post(
        "/v1/chat/completions" if phase == "headers" else "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello", "stream": phase == "stream"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 200
    if phase == "stream":
        assert "response.completed" in response.get_data(as_text=True)
    else:
        assert response.get_json()["id"] in {"chat_slow", "resp_slow"}
    records = state.request_store.query(page=1, page_size=10)["items"]
    assert records[0]["status_code"] == 200
    assert records[0]["duration_ms"] >= 150


def test_forwarding_disables_every_http_timeout(client, app, upstream_factory):
    upstream = upstream_factory("deadlines", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    state = app.extensions["relay_state"]
    observed = []

    def respond(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json={"id": "resp_no_deadline"})

    state.upstream_client.close()
    # Even a replacement client with finite defaults must forward without deadlines.
    state.upstream_client = httpx.Client(timeout=0.05, transport=httpx.MockTransport(respond))
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 200
    assert observed == [{"connect": None, "read": None, "write": None, "pool": None}]


def test_proxy_rebuilds_forward_headers_as_local_request(client, app, upstream_factory):
    upstream = upstream_factory("headers", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={
            "Authorization": f"Bearer {proxy_key}",
            "User-Agent": "external-client",
            "X-Forwarded-For": "203.0.113.9",
            "X-Real-IP": "203.0.113.9",
            "Forwarded": "for=203.0.113.9",
            "Via": "relay",
            "Origin": "https://relay.example",
            "OpenAI-Beta": "assistants=v2",
            "Idempotency-Key": "req_123",
        },
    )

    assert response.status_code == 200
    received = {key.lower(): value for key, value in upstream.last_response_headers.items()}
    assert received["authorization"] == "Bearer up-headers"
    assert received["openai-beta"] == "assistants=v2"
    assert received["idempotency-key"] == "req_123"
    assert received["user-agent"] != "external-client"
    assert "x-forwarded-for" not in received
    assert "x-real-ip" not in received
    assert "forwarded" not in received
    assert "via" not in received
    assert "origin" not in received


def test_empty_allowed_models_means_no_model_restriction(client, app, upstream_factory):
    upstream = upstream_factory("unrestricted", ["gpt-4.1"])
    state = app.extensions["relay_state"]
    state.config_store.save_upstreams(
        [
            {
                "id": "unrestricted_id",
                "name": "unrestricted",
                "base_url": upstream.base_url,
                "api_key": "up-unrestricted",
                "enabled": True,
                "order": 1,
                "allowed_models": [],
                "endpoint_capabilities": ["*"],
                "timeout_ms": 5000,
                "probe_enabled": True,
            }
        ]
    )
    _, proxy_key = state.config_store.create_proxy_key("pytest")

    response = client.post(
        "/v1/responses",
        json={"model": "not-in-local-list", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )

    assert response.status_code == 200
    assert response.get_json()["id"] == "resp_unrestricted"


def test_model_mapping_routes_and_rewrites_json_request(client, app, upstream_factory):
    upstream = upstream_factory("mapped", ["gpt-5.4"])
    state = app.extensions["relay_state"]
    state.config_store.update_app_settings({"model_mappings": {"gpt-5.5": "gpt-5.4"}})
    state.config_store.save_upstreams(
        [
            {
                "id": "mapped_id",
                "name": "mapped",
                "base_url": upstream.base_url,
                "api_key": "up-mapped",
                "enabled": True,
                "order": 1,
                "allowed_models": ["gpt-5.4"],
                "endpoint_capabilities": ["*"],
                "timeout_ms": 5000,
                "probe_enabled": True,
            }
        ]
    )
    _, proxy_key = state.config_store.create_proxy_key("pytest")

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.5", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )

    assert response.status_code == 200
    assert upstream.last_response_json["model"] == "gpt-5.4"
    records = state.request_store.query(page=1, page_size=10)
    item = records["items"][0]
    assert item["model"] == "gpt-5.5"
    assert item["requested_model"] == "gpt-5.5"
    assert item["upstream_model"] == "gpt-5.4"
    assert item["model_mapped"] is True


def test_model_mapping_rewrites_urlencoded_request(client, app, upstream_factory):
    upstream = upstream_factory("form-mapped", ["gpt-5.4"])
    state = app.extensions["relay_state"]
    state.config_store.update_app_settings({"model_mappings": {"gpt-5.5": "gpt-5.4"}})
    state.config_store.save_upstreams(
        [
            {
                "id": "form_mapped_id",
                "name": "form-mapped",
                "base_url": upstream.base_url,
                "api_key": "up-form-mapped",
                "enabled": True,
                "order": 1,
                "allowed_models": ["gpt-5.4"],
                "endpoint_capabilities": ["*"],
                "timeout_ms": 5000,
                "probe_enabled": True,
            }
        ]
    )
    _, proxy_key = state.config_store.create_proxy_key("pytest")

    response = client.post(
        "/v1/responses",
        data={"model": "gpt-5.5", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 200
    assert upstream.last_response_form["model"] == "gpt-5.4"


def test_models_endpoint_includes_model_mapping_sources(client, app, upstream_factory):
    upstream = upstream_factory("models", ["gpt-5.4"])
    proxy_key = provision_relay(app, [upstream])
    app.extensions["relay_state"].config_store.update_app_settings(
        {"model_mappings": {"gpt-5.5": "gpt-5.4"}}
    )

    response = client.get("/v1/models", headers={"Authorization": f"Bearer {proxy_key}"})

    assert response.status_code == 200
    model_ids = {item["id"] for item in response.get_json()["data"]}
    assert "gpt-5.4" in model_ids
    assert "gpt-5.5" in model_ids


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
    health = app.extensions["relay_state"].health_store.get_all()["failing_id"]
    assert health["passive_error"] == 1
    assert health["passive_success"] == 0


def test_auto_failover_can_be_disabled(client, app, upstream_factory):
    failing = upstream_factory("failing", ["gpt-4.1"], fail_responses=1)
    healthy = upstream_factory("healthy", ["gpt-4.1"])
    state = app.extensions["relay_state"]
    proxy_key = provision_relay(app, [failing, healthy])
    state.config_store.update_app_settings({"auto_failover_enabled": False})

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )

    assert response.status_code == 500
    assert failing.responses_calls == 1
    assert healthy.responses_calls == 0


def test_proxy_tries_suppressed_fallback_after_network_error(client, app, upstream_factory):
    healthy = upstream_factory("healthy", ["gpt-4.1"])
    state = app.extensions["relay_state"]
    state.config_store.save_upstreams(
        [
            {
                "id": "broken_id",
                "name": "broken",
                "base_url": _unused_local_base_url(),
                "api_key": "up-broken",
                "enabled": True,
                "order": 1,
                "allowed_models": ["gpt-4.1"],
                "endpoint_capabilities": ["*"],
                "timeout_ms": 500,
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
    state.health_store.mark_timeout_failover("healthy_id", "previous timeout")
    _, proxy_key = state.config_store.create_proxy_key("pytest")

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )

    assert response.status_code == 200
    assert response.get_json()["id"] == "resp_healthy"
    assert healthy.responses_calls == 1
    health = state.health_store.get_all()
    assert health["broken_id"]["timeout_failover_active"] is True
    assert health["broken_id"]["passive_error"] == 1


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


def test_streaming_proxy_decodes_compressed_sse(client, app, upstream_factory, monkeypatch):
    upstream = upstream_factory("compressed", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    event = b'data: {"type":"response.completed","response":{"id":"resp_compressed"}}\n\n'

    def respond(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream", "Content-Encoding": "gzip"},
            stream=httpx.ByteStream(gzip.compress(event)),
        )

    state = app.extensions["relay_state"]
    state.upstream_client.close()
    monkeypatch.setattr(state, "upstream_client", httpx.Client(transport=httpx.MockTransport(respond)))
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello", "stream": True},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.get_data() == event
    assert "Content-Encoding" not in response.headers
    assert app.extensions["relay_state"].object_store.get("resp_compressed")["upstream_id"] == "compressed_id"
    state.upstream_client.close()


def test_large_download_streams_without_reading_ahead(client, app, upstream_factory):
    upstream = upstream_factory("downloads", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    state = app.extensions["relay_state"]
    state.object_store.upsert("file_large", "downloads_id", "file")
    consumed = []

    class Download(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"first", b"second", b"third"):
                consumed.append(chunk)
                yield chunk

    def respond(request):
        assert request.headers["Range"] == "bytes=5-20"
        return httpx.Response(206, stream=Download(), headers={
            "Content-Type": "application/octet-stream", "Content-Length": "16", "Content-Range": "bytes 5-20/35",
        })

    state.upstream_client.close()
    state.upstream_client = httpx.Client(transport=httpx.MockTransport(respond))
    response = client.get("/v1/files/file_large/content", headers={
        "Authorization": f"Bearer {proxy_key}", "Range": "bytes=5-20",
    })
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 5-20/35"
    assert response.headers["Content-Length"] == "16"
    assert consumed == [b"first"]
    assert response.get_data() == b"firstsecondthird"


@pytest.mark.parametrize("stream", [False, True])
def test_large_responses_keep_object_routing_and_usage(client, app, upstream_factory, monkeypatch, stream):
    upstream = upstream_factory("large", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    monkeypatch.setattr("relay.proxy._MAX_CAPTURE_BYTES", 128)
    state = app.extensions["relay_state"]
    payload = {
        "output": [{"text": "x" * 1000, "id": "output_large"}],
        "id": "resp_large", "object": "response",
        "usage": {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
    }
    if stream:
        event = {"type": "response.completed", "response": payload}
        wire = b"data: " + json.dumps(event).encode() + b"\n\n"
        content_type = "text/event-stream"
    else:
        wire = json.dumps(payload).encode()
        content_type = "application/json"

    state.upstream_client.close()
    state.upstream_client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=wire, headers={"Content-Type": content_type})
    ))
    response = client.post(
        "/v1/responses", json={"model": "gpt-4.1", "input": "hello", "stream": stream},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.get_data() == wire
    assert state.object_store.get("resp_large")["upstream_id"] == "large_id"
    assert state.object_store.get("output_large")["upstream_id"] == "large_id"
    assert state.request_store.query()["items"][0]["total_tokens"] == 60


@pytest.mark.parametrize("model", ["gpt-4.1", "alias"])
def test_large_multipart_upload_keeps_file_and_form(client, app, upstream_factory, monkeypatch, model):
    upstream = upstream_factory("upload", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    state = app.extensions["relay_state"]
    state.config_store.update_app_settings({"model_mappings": {"alias": "gpt-4.1"}})
    monkeypatch.setattr("relay.proxy._MAX_CAPTURE_BYTES", 128)
    file_data = b"x" * 1024

    def respond(request):
        builder = EnvironBuilder(method="POST", data=request.read(), content_type=request.headers["Content-Type"])
        with WsgiRequest(builder.get_environ()) as forwarded:
            assert forwarded.form["model"] == "gpt-4.1"
            assert forwarded.form["purpose"] == "assistants"
            assert forwarded.files["file"].read() == file_data
        return httpx.Response(200, json={"id": "file_large", "object": "file"})

    state.upstream_client.close()
    state.upstream_client = httpx.Client(transport=httpx.MockTransport(respond))
    response = client.post(
        "/v1/files", data={"model": model, "purpose": "assistants", "file": (BytesIO(file_data), "upload.txt")},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 200
    assert state.object_store.get("file_large")["upstream_id"] == "upload_id"


def test_relay_rejects_oversized_requests(client, app, upstream_factory):
    upstream = upstream_factory("limits", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    app.config["MAX_CONTENT_LENGTH"] = 64
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "x" * 100},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.status_code == 413
    assert response.get_json()["error"]["type"] == "request_too_large"
    assert upstream.responses_calls == 0


def test_stream_error_is_reported_in_request_logs(client, app, upstream_factory):
    upstream = upstream_factory("stream-error", ["gpt-4.1"])
    proxy_key = provision_relay(app, [upstream])
    state = app.extensions["relay_state"]
    state.upstream_client.close()
    event = b'data: {"type":"response.failed","response":null}\n\n'
    state.upstream_client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=event, headers={"Content-Type": "text/event-stream"})
    ))
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "hello", "stream": True},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert response.get_data() == event
    record = state.request_store.query()["items"][0]
    assert record["status_code"] == 502
    assert record["upstream_error"] == "Upstream sent a stream error event"


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


def test_upstream_timeout_status_failover_skips_higher_priority_upstream(client, app, upstream_factory):
    timed_out = upstream_factory(
        "timed_out",
        ["gpt-4.1"],
        fail_responses=1,
        failure_status_code=504,
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


def test_recovery_probe_restores_upstream_after_timeout_status(client, app, upstream_factory):
    recovering = upstream_factory(
        "recovering",
        ["gpt-4.1", "gpt-5.4"],
        fail_responses=1,
        failure_status_code=504,
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

    second = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "again"},
        headers={"Authorization": f"Bearer {proxy_key}"},
    )
    assert second.status_code == 200
    assert second.get_json()["id"] == "resp_recovering"
