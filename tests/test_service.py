from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from appium_tool.config import Settings
from appium_tool.service import create_app


class FakeSessions:
    def execute(self, session_id, payload):
        return {
            "session_id": session_id,
            "request": payload,
            "status": "completed",
        }

    def launch(self, payload):
        return {
            "session_id": "session_1",
            "screen_id": "screen_1",
            "package_id": payload["package_id"],
        }

    def session_status(self, session_id):
        return {"session_id": session_id, "status": "active"}

    def close(self, session_id):
        return True


class FakeRuntime:
    def status(self):
        return {"status": "ready"}


class FakeApps:
    pass


def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        apk_roots=(tmp_path,),
        service_token="service-secret",
        admin_token="admin-secret",
        session_ttl_seconds=60,
        host="127.0.0.1",
        port=8000,
    )


def app(tmp_path):
    return create_app(
        settings(tmp_path),
        session_manager=FakeSessions(),
        runtime_manager=FakeRuntime(),
        app_manager=FakeApps(),
    )


def client(tmp_path):
    return TestClient(app(tmp_path), raise_server_exceptions=False)


def test_protected_routes_require_bearer_token(tmp_path):
    test_client = client(tmp_path)

    assert test_client.get("/health").status_code == 200
    assert test_client.get("/api/v1/tools").status_code == 401
    assert test_client.get(
        "/api/v1/tools",
        headers={"Authorization": "Bearer service-secret"},
    ).status_code == 200


def test_rest_tool_invocation_uses_registry_policy(tmp_path):
    test_client = client(tmp_path)
    headers = {"Authorization": "Bearer service-secret"}

    result = test_client.post(
        "/api/v1/tools/tap/invoke",
        headers=headers,
        json={
            "session_id": "session_1",
            "screen_id": "screen_1",
            "target": {"element_id": "element_1"},
        },
    )
    denied = test_client.post(
        "/api/v1/tools/home/invoke",
        headers=headers,
        json={
            "session_id": "session_1",
            "screen_id": "screen_1",
            "confirm": True,
        },
    )

    assert result.status_code == 200
    assert result.json()["request"]["action"] == "tap"
    assert denied.status_code == 403


def test_runtime_administration_requires_admin_scope(tmp_path):
    test_client = client(tmp_path)

    service = test_client.get(
        "/api/v1/runtime",
        headers={"Authorization": "Bearer service-secret"},
    )
    admin = test_client.get(
        "/api/v1/runtime",
        headers={"Authorization": "Bearer admin-secret"},
    )

    assert service.status_code == 403
    assert admin.status_code == 200
