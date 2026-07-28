from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from appium_tool.auth import Principal
from appium_tool.exploration.actions import (
    ELEMENT_ACTIONS,
    SUPPORTED_ACTIONS,
    SessionManager,
)
from appium_tool.safety import Risk, SafetyPolicy
from appium_tool.types import JsonObject


ACTION_DESCRIPTIONS: dict[str, str] = {
    "tap": "Tap a captured UI element.",
    "double_tap": "Double-tap a captured UI element.",
    "long_press": "Long-press a captured UI element.",
    "focus": "Move input focus to a captured UI element.",
    "type_text": "Type text into a captured UI element.",
    "replace_text": "Replace text in a captured UI element.",
    "clear_text": "Clear text from a captured UI element.",
    "set_checked": "Set the checked state of a captured control.",
    "select_option": "Select an option in a native control.",
    "submit": "Submit the current form or keyboard action.",
    "swipe": "Swipe in a direction within the current screen.",
    "scroll": "Scroll the current view.",
    "scroll_to": "Scroll until matching content is visible.",
    "fling": "Perform a high-velocity scroll gesture.",
    "drag": "Drag from an element using a relative movement.",
    "drag_and_drop": "Drag one captured element onto another.",
    "pinch_open": "Zoom in with a pinch-open gesture.",
    "pinch_close": "Zoom out with a pinch-close gesture.",
    "gesture_sequence": "Perform an explicit W3C gesture sequence.",
    "back": "Press Android Back.",
    "home": "Press Android Home.",
    "recent_apps": "Open Android recent apps.",
    "press_key": "Send an Android key code.",
    "hide_keyboard": "Hide the software keyboard.",
    "set_orientation": "Change device orientation.",
    "open_notifications": "Open the Android notification shade.",
    "close_system_panel": "Close the active Android system panel.",
    "accept_dialog": "Accept the active dialog.",
    "dismiss_dialog": "Dismiss the active dialog.",
    "choose_dialog_action": "Choose a captured dialog action.",
    "allow_permission": "Allow the active Android permission request.",
    "deny_permission": "Deny the active Android permission request.",
    "tap_outside": "Tap outside the focused surface or dialog.",
    "activate_app": "Bring the application to the foreground.",
    "background_app": "Send the application to the background.",
    "terminate_app": "Terminate the application process.",
    "restart_app": "Terminate and reactivate the application.",
    "reset_app": "Reset application state through the driver.",
    "start_activity": "Start an explicit Android activity.",
    "open_deep_link": "Open an Android deep link.",
    "return_to_start": "Return to the recorded starting state.",
    "switch_context": "Switch between native and webview contexts.",
    "switch_window": "Switch the active web window.",
    "switch_frame": "Switch the active web frame.",
    "web_select_option": "Select an option in a web control.",
    "web_submit": "Submit a captured web form.",
    "tap_point": "Tap explicit screen coordinates.",
    "long_press_point": "Long-press explicit screen coordinates.",
    "swipe_points": "Swipe between explicit screen coordinates.",
    "drag_points": "Drag between explicit screen coordinates.",
    "wait": "Wait for an explicit duration.",
    "wait_until_stable": "Wait until repeated screen observations are stable.",
    "capture_screen": "Capture and return the current screen state.",
    "assert_screen": "Assert properties of the current screen.",
    "assert_element": "Assert properties of a captured element.",
    "no_action": "Record an intentional no-op.",
    "recover": "Run the generic recovery action.",
    "mark_terminal": "Mark the current path as terminal.",
}

READ_ONLY = {
    "wait",
    "wait_until_stable",
    "capture_screen",
    "assert_screen",
    "assert_element",
    "no_action",
    "mark_terminal",
}
SYSTEM = {
    "home",
    "recent_apps",
    "press_key",
    "set_orientation",
    "open_notifications",
    "close_system_panel",
    "start_activity",
    "open_deep_link",
}
DESTRUCTIVE = {"reset_app", "terminate_app"}
CONTROLLED = {
    "deny_permission",
    "background_app",
    "restart_app",
    "recover",
    "return_to_start",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    action: str
    description: str
    risk: Risk
    input_schema: JsonObject

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "action": self.action,
            "description": self.description,
            "risk": self.risk.value,
            "input_schema": self.input_schema,
        }


def _risk(action: str) -> Risk:
    if action in READ_ONLY:
        return Risk.READ_ONLY
    if action in SYSTEM:
        return Risk.SYSTEM
    if action in DESTRUCTIVE:
        return Risk.DESTRUCTIVE
    if action in CONTROLLED:
        return Risk.CONTROLLED
    return Risk.SAFE


def _schema(action: str, risk: Risk) -> JsonObject:
    properties: JsonObject = {
        "session_id": {
            "type": "string",
            "description": "Active Appium Tool session identifier.",
        },
        "screen_id": {
            "type": "string",
            "description": "Screen identifier returned by the session.",
        },
        "target": {
            "type": "object",
            "description": "Captured target, normally containing element_id.",
            "additionalProperties": True,
        },
        "parameters": {
            "type": "object",
            "description": "Action-specific parameters.",
            "additionalProperties": True,
        },
        "completion": {
            "type": "object",
            "description": "Timeout, polling, and stability requirements.",
            "additionalProperties": True,
        },
    }
    required = ["session_id", "screen_id"]
    if action in ELEMENT_ACTIONS:
        required.append("target")
    if risk not in {Risk.READ_ONLY, Risk.SAFE}:
        properties["confirm"] = {
            "type": "boolean",
            "description": "Explicit acknowledgement of the tool risk.",
        }
        required.append("confirm")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class ToolRegistry:
    """Single source of truth for REST and MCP action tools."""

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        safety_policy: SafetyPolicy | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.safety_policy = safety_policy or SafetyPolicy()
        self._tools = {
            action: ToolSpec(
                name=action,
                action=action,
                description=ACTION_DESCRIPTIONS[action],
                risk=(risk := _risk(action)),
                input_schema=_schema(action, risk),
            )
            for action in SUPPORTED_ACTIONS
        }

    def list(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"Unknown tool '{name}'.") from error

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        principal: Principal,
    ) -> JsonObject:
        spec = self.get(name)
        values = dict(arguments)
        session_id = values.pop("session_id", "")
        screen_id = values.pop("screen_id", "")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("'session_id' must be a non-empty string.")
        if not isinstance(screen_id, str) or not screen_id:
            raise ValueError("'screen_id' must be a non-empty string.")
        confirmed = values.pop("confirm", False) is True
        self.safety_policy.authorize(
            risk=spec.risk,
            principal=principal,
            confirmed=confirmed,
        )
        allowed = {"target", "parameters", "completion"}
        unexpected = sorted(set(values) - allowed)
        if unexpected:
            raise ValueError(
                f"Unsupported tool arguments: {', '.join(unexpected)}."
            )
        for field in allowed:
            value = values.get(field, {})
            if not isinstance(value, dict):
                raise ValueError(f"'{field}' must be an object.")
        return self.session_manager.execute(
            session_id,
            {
                "action": spec.action,
                "screen_id": screen_id,
                "target": values.get("target", {}),
                "parameters": values.get("parameters", {}),
                "completion": values.get("completion", {}),
            },
        )


assert set(ACTION_DESCRIPTIONS) == set(SUPPORTED_ACTIONS)
