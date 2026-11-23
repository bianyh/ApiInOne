from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

import pytest
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

from relay import create_app
from relay.settings import ADMIN_PASSWORD, ADMIN_USERNAME


class MockUpstreamServer:
    def __init__(
        self,
        name: str,
        models: list[str],
        fail_responses: int = 0,
        failure_status_code: int = 500,
        stream_delay_seconds: float = 0.0,
        models_delay_seconds: float = 0.0,
        models_mode: str = "valid",
        completion_mode: str = "valid",
        response_delay_seconds: float = 0.0,
        chat_delay_seconds: float = 0.0,
    ) -> None:
        self.name = name
        self.models = models
        self.fail_responses = fail_responses
        self.failure_status_code = failure_status_code
        self.stream_delay_seconds = stream_delay_seconds
        self.models_delay_seconds = models_delay_seconds
        self.models_mode = models_mode
        self.completion_mode = completion_mode
        self.response_delay_seconds = response_delay_seconds
        self.chat_delay_seconds = chat_delay_seconds
        self.models_calls = 0
        self.responses_calls = 0
        self.chat_calls = 0
        self.last_response_headers: dict[str, str] = {}
        self.last_response_json: dict = {}
        self.last_response_form: dict = {}
        self.files: dict[str, dict] = {}
        self._app = Flask(f"mock_{name}")
        self._server = None
        self._thread = None
        self.base_url = ""
        self._register_routes()

    def _register_routes(self) -> None:
        @self._app.get("/v1/models")
        def models():
            self.models_calls += 1
            time.sleep(self.models_delay_seconds)
            if self.models_mode == "invalid_json":
                return Response("not-json", mimetype="text/plain")
            if self.models_mode == "wrong_shape":
                return jsonify({"ok": True})
            if self.models_mode == "empty":
                return jsonify({"object": "list", "data": []})
            return jsonify(
                {
                    "object": "list",
                    "data": [{"id": item, "object": "model", "owned_by": self.name} for item in self.models],
                }
            )

        @self._app.post("/v1/responses")
        def responses():
            self.responses_calls += 1
            self.last_response_headers = dict(request.headers)
            payload = request.get_json(silent=True) or {}
            self.last_response_json = payload
            self.last_response_form = request.form.to_dict(flat=True)
            if self.fail_responses > 0:
                self.fail_responses -= 1
                return jsonify({"error": {"message": "temporary failure"}}), self.failure_status_code

            if payload.get("stream"):
                response_id = f"resp_{self.name}"

                def generate():
                    yield f'data: {{"type":"response.created","response":{{"id":"{response_id}","object":"response"}}}}\n\n'
                    time.sleep(self.stream_delay_seconds)
                    yield 'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
                    yield 'data: {"type":"response.output_text.delta","delta":"!"}\n\n'
                    yield (
                        'data: {"type":"response.completed","response":{"id":"%s","object":"response",'
                        '"output":[{"id":"out_1","type":"message","content":[{"type":"output_text","text":"Hello!"}]}],'
                        '"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18,'
                        '"input_tokens_details":{"cached_tokens":3},"output_tokens_details":{"reasoning_tokens":2}}}}\n\n'
                    ) % response_id
                    yield "data: [DONE]\n\n"

                return Response(generate(), mimetype="text/event-stream")

            response_payload = {
                "id": f"resp_{self.name}",
                "object": "response",
                "model": payload.get("model"),
                "output": [{"type": "output_text", "text": f"hello from {self.name}"}],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 12,
                    "total_tokens": 32,
                    "input_tokens_details": {"cached_tokens": 4},
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            }
            if self.response_delay_seconds > 0:
                def delayed_body():
                    yield " "
                    time.sleep(self.response_delay_seconds)
                    yield json.dumps(response_payload)

                return Response(delayed_body(), mimetype="application/json")
            return jsonify(response_payload)

        @self._app.post("/v1/chat/completions")
        def chat_completions():
            self.chat_calls += 1
            if self.chat_delay_seconds > 0:
                time.sleep(self.chat_delay_seconds)
            payload = request.get_json(silent=True) or {}
            if self.completion_mode == "invalid_json":
                return Response("not-json", mimetype="text/plain")
            if self.completion_mode == "wrong_shape":
                return jsonify({"ok": True})
            if self.completion_mode == "error":
                return jsonify({"error": {"message": "probe failure"}}), 500

            if payload.get("stream"):
                response_id = f"chat_{self.name}"

                def generate():
                    if self.completion_mode == "no_text":
                        yield (
                            'data: {"id":"%s","object":"chat.completion.chunk","choices":[{"index":0,'
                            '"delta":{"role":"assistant","content":null},"finish_reason":"stop"}]}\n\n'
                        ) % response_id
                    else:
                        yield (
                            'data: {"id":"%s","object":"chat.completion.chunk","choices":[{"index":0,'
                            '"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
                        ) % response_id
                        yield (
                            'data: {"id":"%s","object":"chat.completion.chunk","choices":[{"index":0,'
                            '"delta":{"role":"assistant","content":"!"},"finish_reason":null}]}\n\n'
                        ) % response_id
                    yield (
                        'data: {"id":"%s","object":"chat.completion.chunk","choices":[{"index":0,'
                        '"delta":{"role":null,"content":null},"finish_reason":"stop"}]}\n\n'
                    ) % response_id
                    yield "data: [DONE]\n\n"

                return Response(generate(), mimetype="text/event-stream")

            if self.completion_mode == "no_text":
                return jsonify(
                    {
                        "id": f"chat_{self.name}",
                        "object": "chat.completion",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}}],
                    }
                )

            return jsonify(
                {
                    "id": f"chat_{self.name}",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"}}],
                }
            )

        @self._app.post("/v1/files")
        def create_file():
            uploaded = request.files.get("file")
            filename = uploaded.filename if uploaded else "unknown.txt"
            file_id = f"file_{self.name}_{len(self.files) + 1}"
            payload = {"id": file_id, "object": "file", "filename": filename}
            self.files[file_id] = payload
            return jsonify(payload)

        @self._app.get("/v1/files/<file_id>")
        def get_file(file_id: str):
            payload = self.files.get(file_id)
            if not payload:
                return jsonify({"error": {"message": "not found"}}), 404
            return jsonify(payload)

    def start(self) -> "MockUpstreamServer":
        self._server = make_server("127.0.0.1", 0, self._app, threaded=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self) -> "MockUpstreamServer":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


@pytest.fixture()
def app():
    base_dir = Path.cwd() / "test_runtime"
    base_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = base_dir / f"relay_{uuid.uuid4().hex}"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_dir / "config.yaml"
    application = create_app(
        {
            "CONFIG_PATH": str(config_path),
            "SECRET_KEY": "test-secret",
            "DISABLE_SCHEDULER": True,
        }
    )
    application.config.update(TESTING=True)
    yield application
    application.extensions["relay_state"].upstream_client.close()
    shutil.rmtree(runtime_dir, ignore_errors=True)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def upstream_factory():
    servers: list[MockUpstreamServer] = []

    def factory(
        name: str,
        models: list[str],
        fail_responses: int = 0,
        failure_status_code: int = 500,
        stream_delay_seconds: float = 0.0,
        models_delay_seconds: float = 0.0,
        models_mode: str = "valid",
        completion_mode: str = "valid",
        response_delay_seconds: float = 0.0,
        chat_delay_seconds: float = 0.0,
    ):
        server = MockUpstreamServer(
            name=name,
            models=models,
            fail_responses=fail_responses,
            failure_status_code=failure_status_code,
            stream_delay_seconds=stream_delay_seconds,
            models_delay_seconds=models_delay_seconds,
            models_mode=models_mode,
            completion_mode=completion_mode,
            response_delay_seconds=response_delay_seconds,
            chat_delay_seconds=chat_delay_seconds,
        )
        servers.append(server.start())
        return server

    yield factory

    for server in servers:
        server.stop()


def login_admin(client):
    response = client.post(
        "/admin/api/session",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return response.get_json()


def provision_relay(app, upstreams: list[MockUpstreamServer], route_mode: str = "ordered_failover"):
    state = app.extensions["relay_state"]
    state.config_store.update_app_settings({"route_mode": route_mode})
    state.config_store.save_upstreams(
        [
            {
                "id": f"{item.name}_id",
                "name": item.name,
                "base_url": item.base_url,
                "api_key": f"up-{item.name}",
                "enabled": True,
                "order": index + 1,
                "allowed_models": item.models,
                "endpoint_capabilities": ["*"],
                "timeout_ms": 15000,
                "probe_enabled": True,
            }
            for index, item in enumerate(upstreams)
        ]
    )
    _, proxy_key = state.config_store.create_proxy_key("pytest")
    return proxy_key
