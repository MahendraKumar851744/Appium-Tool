from __future__ import annotations

import pytest

from appium_tool.auth import Principal
from appium_tool.exploration.actions import SUPPORTED_ACTIONS
from appium_tool.registry import ToolRegistry
from appium_tool.safety import SafetyViolation


class FakeSessions:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, session_id, payload):
        self.calls.append((session_id, payload))
        return {"status": "completed", "request": payload}


SERVICE = Principal("service", frozenset({"tools"}))
ADMIN = Principal("admin", frozenset({"tools", "admin"}))


def test_registry_covers_every_generic_executor_action_once():
    registry = ToolRegistry(FakeSessions())
    specs = registry.list()

    assert len(specs) == len(SUPPORTED_ACTIONS)
    assert {spec.name for spec in specs} == set(SUPPORTED_ACTIONS)
    assert all(spec.input_schema["additionalProperties"] is False for spec in specs)


def test_safe_tool_maps_to_generic_action_executor_contract():
    sessions = FakeSessions()
    registry = ToolRegistry(sessions)

    result = registry.invoke(
        "tap",
        {
            "session_id": "session_1",
            "screen_id": "screen_1",
            "target": {"element_id": "element_1"},
        },
        principal=SERVICE,
    )

    assert result["status"] == "completed"
    assert sessions.calls[0][0] == "session_1"
    assert sessions.calls[0][1]["action"] == "tap"
    assert sessions.calls[0][1]["target"] == {"element_id": "element_1"}


def test_system_tool_requires_confirmation_and_admin_token():
    registry = ToolRegistry(FakeSessions())
    arguments = {
        "session_id": "session_1",
        "screen_id": "screen_1",
        "confirm": True,
    }

    with pytest.raises(SafetyViolation, match="admin token"):
        registry.invoke("home", arguments, principal=SERVICE)

    result = registry.invoke("home", arguments, principal=ADMIN)
    assert result["request"]["action"] == "home"


def test_controlled_tool_requires_explicit_confirmation():
    registry = ToolRegistry(FakeSessions())

    with pytest.raises(SafetyViolation, match="confirm=true"):
        registry.invoke(
            "restart_app",
            {"session_id": "session_1", "screen_id": "screen_1"},
            principal=ADMIN,
        )
