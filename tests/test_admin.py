from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from tests.conftest import login_admin


def test_admin_dashboard_renders(client):
    response = client.get("/admin")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Private Relay Console" in body
    assert "自动探查模型" in body
    assert "三步添加节点" in body
    assert "拖拽调整使用顺序" in body
    assert "复制模型列表" in body
    assert "搜索模型名" in body
    assert "Detected Models" in body


def test_admin_login_and_settings_roundtrip(client):
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    response = client.get("/admin/api/session")
    assert response.status_code == 200
    assert response.get_json()["authenticated"] is True

    default_settings_response = client.get("/admin/api/settings")
    assert default_settings_response.status_code == 200
    assert default_settings_response.get_json()["item"]["scheduled_probe_all_enabled"] is False

    settings_response = client.put(
        "/admin/api/settings",
        json={
            "title": "私人中转站",
            "route_mode": "fastest_first",
            "auto_failover_enabled": False,
            "model_mappings": {"gpt-5.5": "gpt-5.4", "same": "same", "": "ignored"},
            "scheduled_probe_all_enabled": True,
            "probe_interval_seconds": 60,
            "request_timeout_ms": 45000,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert settings_response.status_code == 200
    assert settings_response.get_json()["item"]["route_mode"] == "fastest_first"
    assert settings_response.get_json()["item"]["auto_failover_enabled"] is False
    assert settings_response.get_json()["item"]["scheduled_probe_all_enabled"] is True
    assert settings_response.get_json()["item"]["model_mappings"] == {"gpt-5.5": "gpt-5.4"}


def test_scheduled_probe_all_job_is_disabled_by_default(app):
    state = app.extensions["relay_state"]
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start(paused=True)
    try:
        state.health_service._configure_job(scheduler)
        assert scheduler.get_job("probe_all") is None
        assert scheduler.get_job("probe_timeout_recovery") is not None

        state.config_store.update_app_settings({"scheduled_probe_all_enabled": True})
        state.health_service._configure_job(scheduler)
        assert scheduler.get_job("probe_all") is not None
        assert scheduler.get_job("probe_timeout_recovery") is not None

        state.config_store.update_app_settings({"scheduled_probe_all_enabled": False})
        state.health_service._configure_job(scheduler)
        assert scheduler.get_job("probe_all") is None
        assert scheduler.get_job("probe_timeout_recovery") is not None
    finally:
        scheduler.shutdown(wait=False)


def test_admin_upstreams_and_proxy_keys(client, app, upstream_factory):
    upstream = upstream_factory("primary", ["gpt-4.1"])
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    upstream_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "name": "主节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": ["gpt-4.1"],
                    "endpoint_capabilities": ["*"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert upstream_response.status_code == 200

    get_upstreams = client.get("/admin/api/upstreams")
    payload = get_upstreams.get_json()
    assert payload["items"][0]["api_key"] == ""
    assert payload["items"][0]["api_key_masked"].startswith("secret-u")

    key_response = client.post(
        "/admin/api/proxy-keys",
        json={"name": "办公机", "note": "main device"},
        headers={"X-CSRF-Token": csrf},
    )
    assert key_response.status_code == 201
    key_payload = key_response.get_json()
    assert key_payload["plaintext_key"].startswith("sk-relay-")

    key_id = key_payload["item"]["id"]
    disable_response = client.patch(
        f"/admin/api/proxy-keys/{key_id}",
        json={"enabled": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert disable_response.status_code == 200
    assert disable_response.get_json()["item"]["enabled"] is False


def test_upstream_model_discovery_and_auto_fill(client, upstream_factory):
    upstream = upstream_factory("discoverable", ["gpt-4.1", "gpt-4.1-mini"])
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    discovery_response = client.post(
        "/admin/api/upstreams/discover",
        json={
            "name": "自动探测节点",
            "base_url": upstream.base_url,
            "api_key": "secret-up-key",
            "timeout_ms": 10000,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert discovery_response.status_code == 200
    discovery_payload = discovery_response.get_json()["item"]
    assert discovery_payload["model_count"] == 2
    assert discovery_payload["models"] == ["gpt-4.1", "gpt-4.1-mini"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "name": "自动探测节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": [],
                    "endpoint_capabilities": ["*"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                    "auto_discover_models": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert save_response.status_code == 200

    list_response = client.get("/admin/api/upstreams")
    item = list_response.get_json()["items"][0]
    assert item["allowed_models"] == ["gpt-4.1", "gpt-4.1-mini"]
    assert item["models_detected_at"] is not None


def test_upstream_can_be_saved_without_model_discovery(client, upstream_factory):
    upstream = upstream_factory("no-models-required", ["gpt-4.1"], models_mode="invalid_json")
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "id": "direct_add_id",
                    "name": "直接添加节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": [],
                    "endpoint_capabilities": ["*"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert save_response.status_code == 200
    assert upstream.models_calls == 0
    item = client.get("/admin/api/upstreams").get_json()["items"][0]
    assert item["allowed_models"] == []


def test_health_probe_can_continue_when_model_list_is_invalid(client, upstream_factory):
    upstream = upstream_factory("invalid-probe", ["gpt-4.1"], models_mode="invalid_json")
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "id": "invalid_probe_id",
                    "name": "无效探活节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": ["gpt-4.1"],
                    "endpoint_capabilities": ["*"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert save_response.status_code == 200

    probe_response = client.post(
        "/admin/api/health/probe",
        json={"upstream_id": "invalid_probe_id"},
        headers={"X-CSRF-Token": csrf},
    )
    assert probe_response.status_code == 200
    payload = probe_response.get_json()["item"]
    assert payload["last_probe_success"] is True
    assert payload["last_probe_status_code"] == 200
    assert payload["last_probe_model_count"] == 0
    assert payload["last_probe_endpoint"] == "chat.completions"
    assert payload["last_probe_reply_excerpt"] == "Hello!"
    assert payload["last_probe_error"] is None


def test_health_probe_marks_valid_model_list_healthy(client, upstream_factory):
    upstream = upstream_factory("valid-probe", ["gpt-4.1", "gpt-4.1-mini"])
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "id": "valid_probe_id",
                    "name": "有效探活节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": ["gpt-4.1", "gpt-4.1-mini"],
                    "endpoint_capabilities": ["*"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert save_response.status_code == 200

    probe_response = client.post(
        "/admin/api/health/probe",
        json={"upstream_id": "valid_probe_id"},
        headers={"X-CSRF-Token": csrf},
    )
    assert probe_response.status_code == 200
    payload = probe_response.get_json()["item"]
    assert payload["last_probe_success"] is True
    assert payload["last_probe_status_code"] == 200
    assert payload["last_probe_model_count"] == 2
    assert payload["last_probe_model_used"] == "gpt-5.4"
    assert payload["last_probe_endpoint"] == "chat.completions"
    assert payload["last_probe_reply_excerpt"] == "Hello!"
    assert payload["last_probe_error"] is None


def test_health_probe_requires_valid_hi_completion_response(client, upstream_factory):
    upstream = upstream_factory("no-text-probe", ["gpt-4.1"], completion_mode="no_text")
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "id": "no_text_probe_id",
                    "name": "无内容探活节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": ["gpt-4.1"],
                    "endpoint_capabilities": ["chat.completions"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert save_response.status_code == 200

    probe_response = client.post(
        "/admin/api/health/probe",
        json={"upstream_id": "no_text_probe_id"},
        headers={"X-CSRF-Token": csrf},
    )
    assert probe_response.status_code == 200
    payload = probe_response.get_json()["item"]
    assert payload["last_probe_success"] is False
    assert payload["last_probe_status_code"] == 200
    assert payload["last_probe_model_count"] == 1
    assert payload["last_probe_model_used"] == "gpt-5.4"
    assert payload["last_probe_endpoint"] == "chat.completions"
    assert "assistant text" in payload["last_probe_error"]


def test_health_probe_can_use_selected_probe_model(client, upstream_factory):
    upstream = upstream_factory("selected-model-probe", ["gpt-4.1", "gpt-5.4"])
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "id": "selected_model_probe_id",
                    "name": "自选探活模型节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": ["gpt-4.1", "gpt-5.4"],
                    "endpoint_capabilities": ["chat.completions"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert save_response.status_code == 200

    probe_response = client.post(
        "/admin/api/health/probe",
        json={"upstream_id": "selected_model_probe_id", "probe_model": "gpt-4.1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert probe_response.status_code == 200
    payload = probe_response.get_json()["item"]
    assert payload["last_probe_success"] is True
    assert payload["last_probe_model_used"] == "gpt-4.1"
    assert payload["last_probe_reply_excerpt"] == "Hello!"


def test_health_probe_can_use_responses_endpoint(client, upstream_factory):
    upstream = upstream_factory("responses-probe", ["gpt-4.1"])
    session_payload = login_admin(client)
    csrf = session_payload["csrf_token"]

    save_response = client.put(
        "/admin/api/upstreams",
        json={
            "upstreams": [
                {
                    "id": "responses_probe_id",
                    "name": "Responses 探活节点",
                    "base_url": upstream.base_url,
                    "api_key": "secret-up-key",
                    "enabled": True,
                    "order": 1,
                    "allowed_models": ["gpt-4.1"],
                    "endpoint_capabilities": ["responses"],
                    "timeout_ms": 10000,
                    "probe_enabled": True,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert save_response.status_code == 200

    probe_response = client.post(
        "/admin/api/health/probe",
        json={
            "upstream_id": "responses_probe_id",
            "probe_model": "gpt-4.1",
            "probe_endpoint": "responses",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert probe_response.status_code == 200
    payload = probe_response.get_json()["item"]
    assert payload["last_probe_success"] is True
    assert payload["last_probe_model_used"] == "gpt-4.1"
    assert payload["last_probe_endpoint"] == "responses"
    assert payload["last_probe_reply_excerpt"] == "Hello!"
    assert upstream.responses_calls == 1
    assert upstream.chat_calls == 0
