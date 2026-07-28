from __future__ import annotations

import atexit
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from appium_tool.exploration.appium import AppiumExplorer
from appium_tool.exploration.models import JsonObject, ScreenCapture
from appium_tool.exploration.storage import SessionStore


SUPPORTED_ACTIONS = (
    "tap",
    "double_tap",
    "long_press",
    "focus",
    "type_text",
    "replace_text",
    "clear_text",
    "set_checked",
    "select_option",
    "submit",
    "swipe",
    "scroll",
    "scroll_to",
    "fling",
    "drag",
    "drag_and_drop",
    "pinch_open",
    "pinch_close",
    "gesture_sequence",
    "back",
    "home",
    "recent_apps",
    "press_key",
    "hide_keyboard",
    "set_orientation",
    "open_notifications",
    "close_system_panel",
    "accept_dialog",
    "dismiss_dialog",
    "choose_dialog_action",
    "allow_permission",
    "deny_permission",
    "tap_outside",
    "activate_app",
    "background_app",
    "terminate_app",
    "restart_app",
    "reset_app",
    "start_activity",
    "open_deep_link",
    "return_to_start",
    "switch_context",
    "switch_window",
    "switch_frame",
    "web_select_option",
    "web_submit",
    "tap_point",
    "long_press_point",
    "swipe_points",
    "drag_points",
    "wait",
    "wait_until_stable",
    "capture_screen",
    "assert_screen",
    "assert_element",
    "no_action",
    "recover",
    "mark_terminal",
)

ELEMENT_ACTIONS = {
    "tap",
    "double_tap",
    "long_press",
    "focus",
    "type_text",
    "replace_text",
    "clear_text",
    "set_checked",
    "select_option",
    "choose_dialog_action",
    "web_select_option",
    "web_submit",
    "assert_element",
}

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PACKAGE_ID_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


class ActionError(RuntimeError):
    pass


class ActionValidationError(ActionError):
    pass


class AppLaunchError(ActionError):
    pass


class SessionNotFoundError(ActionError):
    pass


class ScreenNotFoundError(ActionError):
    pass


class StaleScreenError(ActionError):
    def __init__(self, message: str, *, details: JsonObject) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class ActionRequest:
    action: str
    screen_id: str
    target: JsonObject = field(default_factory=dict)
    parameters: JsonObject = field(default_factory=dict)
    completion: JsonObject = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: JsonObject) -> "ActionRequest":
        action = str(payload.get("action") or "").strip()
        screen_id = str(payload.get("screen_id") or "").strip()
        if action not in SUPPORTED_ACTIONS:
            raise ActionValidationError(
                f"Unsupported action '{action}'.",
            )
        if not screen_id or not IDENTIFIER_PATTERN.fullmatch(screen_id):
            raise ActionValidationError(
                "'screen_id' must be a captured screen identifier."
            )
        target = _object(payload.get("target", {}), "target")
        parameters = _object(payload.get("parameters", {}), "parameters")
        completion = _object(payload.get("completion", {}), "completion")
        if action in ELEMENT_ACTIONS and not target.get("element_id"):
            raise ActionValidationError(
                f"Action '{action}' requires target.element_id."
            )
        return cls(
            action=action,
            screen_id=screen_id,
            target=target,
            parameters=parameters,
            completion=completion,
        )

    @property
    def timeout_seconds(self) -> float:
        value = _number(self.completion.get("timeout_ms"), 15_000)
        return min(60.0, max(0.5, value / 1000))

    @property
    def interval_seconds(self) -> float:
        value = _number(self.completion.get("interval_ms"), 400)
        return min(2.0, max(0.05, value / 1000))

    @property
    def stable_samples(self) -> int:
        value = int(_number(self.completion.get("stable_samples"), 3))
        return min(10, max(1, value))

    def to_dict(self) -> JsonObject:
        return {
            "action": self.action,
            "screen_id": self.screen_id,
            "target": self.target,
            "parameters": self.parameters,
            "completion": {
                **self.completion,
                "timeout_ms": round(self.timeout_seconds * 1000),
                "interval_ms": round(self.interval_seconds * 1000),
                "stable_samples": self.stable_samples,
            },
        }


@dataclass
class ResolvedTarget:
    captured: JsonObject
    live: Any | None
    locator: JsonObject | None

    @property
    def remote_id(self) -> str | None:
        value = getattr(self.live, "id", None)
        return str(value) if value else None

    def to_dict(self) -> JsonObject:
        return {
            "element_id": self.captured.get("id"),
            "resource_id": self.captured.get("resource_id"),
            "text": self.captured.get("text"),
            "content_description": self.captured.get("content_description"),
            "bounds": self.captured.get("bounds"),
            "locator_used": self.locator,
            "appium_element_id": self.remote_id,
        }


@dataclass
class ManagedSession:
    explorer: AppiumExplorer
    store: SessionStore
    lock: threading.Lock = field(default_factory=threading.Lock)
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)


class SessionManager:
    """TTL-bound Appium sessions behind one generic action contract."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        explorer_factory: Callable[[JsonObject], AppiumExplorer] | None = None,
        ttl_seconds: int = 1800,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 30:
            raise ValueError("Session TTL must be at least 30 seconds.")
        self.output_root = Path(output_root).expanduser().resolve()
        self.explorer_factory = explorer_factory or self._default_explorer
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._sessions: dict[str, ManagedSession] = {}
        self._manager_lock = threading.Lock()
        atexit.register(self.close_all)

    def execute(self, session_id: str, payload: JsonObject) -> JsonObject:
        self.expire_idle()
        request = ActionRequest.from_dict(payload)
        expected = self._load_screen(session_id, request.screen_id)
        managed = self._managed_session(session_id, expected)
        with managed.lock:
            managed.last_used_at = self.clock()
            before_capture = managed.explorer.observe()
            try:
                self._validate_live_screen(expected, before_capture.observation)
            except StaleScreenError as error:
                if not self._target_remains_resolvable(
                    request,
                    expected,
                    before_capture.observation,
                ):
                    live_result = managed.store.save_screen(before_capture)
                    error.details.update(
                        {
                            "actual_screen_id": live_result.screen_id,
                            "actual_appium_result": str(live_result.document_path),
                        }
                    )
                    raise
            executor = ActionExecutor(managed.explorer)
            result, after_capture = executor.execute(
                session_id,
                request,
                expected,
                before_capture,
            )
            after_result = managed.store.save_screen(after_capture)
            result["after"] = {
                **result["after"],
                "screen_id": after_result.screen_id,
                "fingerprint": after_capture.observation.get("fingerprint"),
                "appium_result": str(after_result.document_path),
                "viewer": str(after_result.viewer_path),
            }
            result["transition_id"] = f"transition_{result['action_id']}"
            result_path = (
                managed.store.run_root
                / "transitions"
                / result["transition_id"]
                / "result.json"
            )
            result["artifact"] = str(result_path)
            managed.store.save_action_result(result)
            return result

    def launch(self, payload: JsonObject) -> JsonObject:
        package_id = payload.get("package_id")
        if not isinstance(package_id, str) or not PACKAGE_ID_PATTERN.fullmatch(
            package_id
        ):
            raise ActionValidationError(
                "'package_id' must be a valid Android application ID, "
                "for example 'com.example.app'."
            )
        device_id = payload.get("device_id")

        if device_id is not None and (

            not isinstance(device_id, str)

            or not device_id.strip()

        ):

            raise ActionValidationError(

                "'device_id' must be a non-empty string."

            )

        unexpected = sorted(

            set(payload) - {"package_id", "device_id"}

        )
        if unexpected:
            raise ActionValidationError(
                f"Unsupported launch fields: {', '.join(unexpected)}."
            )

        explorer = self.explorer_factory(

            {

                "input": {

                    "udid": device_id,

                    "device_name": "Android",

                }

            }

        )
        store = SessionStore(self.output_root)
        try:
            apk, session = explorer.start_package(package_id)
            session_id = store.create_run(apk, session)
            capture = explorer.observe()
            captured = store.save_screen(capture)
            now = self.clock()
            managed = ManagedSession(
                explorer=explorer,
                store=store,
                created_at=now,
                last_used_at=now,
            )
            with self._manager_lock:
                self._sessions[session_id] = managed
            return {
                "contract": "appium.session",
                "schema_version": 1,
                "status": "opened",
                "package_id": package_id,
                "device_id": session.udid,
                "session_id": session_id,
                "screen_id": captured.screen_id,
                "screen_ref": {
                    "session_id": session_id,
                    "screen_id": captured.screen_id,
                },
                "screen": {
                    "fingerprint": capture.observation.get("fingerprint"),
                    "package": capture.observation.get("screen", {}).get(
                        "package"
                    ),
                    "activity": capture.observation.get("screen", {}).get(
                        "activity"
                    ),
                    "stable": capture.observation.get("stability", {}).get(
                        "stable"
                    ),
                    "element_count": captured.element_count,
                    "appium_result": str(captured.document_path),
                    "viewer": str(captured.viewer_path),
                },
                "actions_endpoint": f"/api/v1/sessions/{session_id}/actions",
                "expires_in_seconds": self.ttl_seconds,
            }
        except ActionValidationError:
            raise
        except Exception as error:
            store.fail(error)
            explorer.close()
            raise AppLaunchError(
                f"Could not open installed package '{package_id}': {error}"
            ) from error

    def supported_actions(self) -> list[str]:
        return list(SUPPORTED_ACTIONS)

    def close_all(self) -> None:
        with self._manager_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for managed in sessions:
            managed.explorer.close()

    def close(self, session_id: str) -> bool:
        self._validate_session_id(session_id)
        with self._manager_lock:
            managed = self._sessions.pop(session_id, None)
        if managed is None:
            return False
        with managed.lock:
            managed.explorer.close()
        return True

    def expire_idle(self) -> list[str]:
        now = self.clock()
        with self._manager_lock:
            expired = [
                (session_id, managed)
                for session_id, managed in self._sessions.items()
                if now - managed.last_used_at >= self.ttl_seconds
            ]
            for session_id, _managed in expired:
                self._sessions.pop(session_id, None)
        for _session_id, managed in expired:
            with managed.lock:
                managed.explorer.close()
        return [session_id for session_id, _managed in expired]

    def session_status(self, session_id: str) -> JsonObject:
        self.expire_idle()
        self._validate_session_id(session_id)
        with self._manager_lock:
            managed = self._sessions.get(session_id)
        if managed is None:
            raise SessionNotFoundError(
                f"Session '{session_id}' is not active."
            )
        now = self.clock()
        idle_seconds = max(0, round(now - managed.last_used_at, 3))
        managed.last_used_at = now
        return {
            "session_id": session_id,
            "status": "active",
            "idle_seconds": idle_seconds,
            "expires_in_seconds": self.ttl_seconds,
        }

    def close_package_sessions(self, package_id: str) -> list[str]:
        """Close and forget every live session for one installed package."""
        with self._manager_lock:
            matches = [
                (session_id, managed)
                for session_id, managed in self._sessions.items()
                if managed.explorer.apk is not None
                and managed.explorer.apk.package == package_id
            ]
            for session_id, _managed in matches:
                self._sessions.pop(session_id, None)
        for _session_id, managed in matches:
            with managed.lock:
                managed.explorer.close()
        return [session_id for session_id, _managed in matches]

    def _managed_session(
        self,
        session_id: str,
        expected: JsonObject,
    ) -> ManagedSession:
        self._validate_session_id(session_id)
        with self._manager_lock:
            managed = self._sessions.get(session_id)
            if managed is not None:
                managed.last_used_at = self.clock()
                return managed
            store = SessionStore(self.output_root)
            try:
                store.attach_run(session_id)
            except FileNotFoundError as error:
                raise SessionNotFoundError(str(error)) from error
            explorer = self.explorer_factory(expected)
            capture_input = expected.get("input", {})
            if capture_input.get("launch_source") == "installed_package":
                explorer.start_package(
                    str(capture_input.get("package_id") or "")
                )
            else:
                explorer.start(str(expected.get("apk", {}).get("path") or ""))
            now = self.clock()
            managed = ManagedSession(
                explorer=explorer,
                store=store,
                created_at=now,
                last_used_at=now,
            )
            self._sessions[session_id] = managed
            return managed

    def _load_screen(self, session_id: str, screen_id: str) -> JsonObject:
        self._validate_session_id(session_id)
        if not IDENTIFIER_PATTERN.fullmatch(screen_id):
            raise ScreenNotFoundError("Invalid screen identifier.")
        path = (
            self.output_root
            / session_id
            / "screens"
            / screen_id
            / "appium-result.json"
        )
        if not path.is_file():
            raise ScreenNotFoundError(
                f"Screen '{screen_id}' does not exist in session "
                f"'{session_id}'."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not IDENTIFIER_PATTERN.fullmatch(session_id):
            raise SessionNotFoundError("Invalid session identifier.")

    @staticmethod
    def _validate_live_screen(
        expected: JsonObject,
        live: JsonObject,
    ) -> None:
        expected_key = _semantic_screen_key(expected)
        live_key = _semantic_screen_key(live)
        if expected_key != live_key:
            raise StaleScreenError(
                "The live application is not on the requested captured screen.",
                details={
                    "expected": expected_key,
                    "actual": live_key,
                },
            )

    @staticmethod
    def _target_remains_resolvable(
        request: ActionRequest,
        expected: JsonObject,
        live: JsonObject,
    ) -> bool:
        """Allow volatile content changes when the chosen target stays unique."""
        if request.action not in ELEMENT_ACTIONS:
            return False
        element_id = request.target.get("element_id")
        captured = next(
            (
                item
                for item in expected.get("elements", [])
                if item.get("id") == element_id
            ),
            None,
        )
        if not isinstance(captured, dict):
            return False
        if not captured.get("enabled") or not captured.get("displayed"):
            return False

        locator_field = next(
            (
                field
                for field in ("resource_id", "content_description")
                if captured.get(field)
            ),
            None,
        )
        if locator_field is None:
            return False
        locator_value = captured[locator_field]
        matches = [
            item
            for item in live.get("elements", [])
            if isinstance(item, dict)
            and item.get(locator_field) == locator_value
            and item.get("package") == captured.get("package")
            and item.get("interaction") == captured.get("interaction")
            and item.get("enabled")
            and item.get("displayed")
        ]
        return len(matches) == 1

    @staticmethod
    def _default_explorer(document: JsonObject) -> AppiumExplorer:
        capture_input = document.get("input", {})
        return AppiumExplorer(
            server_url=str(
                capture_input.get("server_url") or "http://127.0.0.1:4723"
            ),
            device_name=str(capture_input.get("device_name") or "Android"),
            udid=capture_input.get("udid"),
            keep_data=True,
            stability_timeout=15.0,
            stability_interval=0.4,
        )


class ActionExecutor:
    """Dispatch one action and report its observable, stabilized outcome."""

    def __init__(self, explorer: AppiumExplorer) -> None:
        self.explorer = explorer

    @property
    def driver(self) -> Any:
        if self.explorer.driver is None:
            raise ActionError("The Appium session is not active.")
        return self.explorer.driver

    def execute(
        self,
        session_id: str,
        request: ActionRequest,
        expected: JsonObject,
        before_capture: ScreenCapture,
    ) -> tuple[JsonObject, ScreenCapture]:
        started_monotonic = time.monotonic()
        started_epoch = time.time()
        action_id = f"action_{uuid4().hex}"
        timings: JsonObject = {}
        errors: list[JsonObject] = []
        target: ResolvedTarget | None = None

        resolution_started = time.monotonic()
        delivery: JsonObject = {
            "status": "not_started",
            "target_resolved": False,
            "dispatched": False,
            "acknowledged": False,
            "error": None,
        }
        try:
            if request.target.get("element_id"):
                target = self._resolve_target(expected, request.target)
                delivery["target_resolved"] = True
                delivery["resolved_target"] = target.to_dict()
            timings["target_resolution_ms"] = _elapsed_ms(resolution_started)

            dispatch_started = time.monotonic()
            delivery["status"] = "dispatched"
            delivery["dispatched"] = True
            dispatch_result = self._dispatch(request, target, expected)
            delivery["status"] = "acknowledged"
            delivery["acknowledged"] = True
            delivery["result"] = _json_safe(dispatch_result)
            timings["dispatch_ms"] = _elapsed_ms(dispatch_started)
        except Exception as error:
            timings.setdefault(
                "target_resolution_ms",
                _elapsed_ms(resolution_started),
            )
            delivery["status"] = (
                "target_not_found"
                if isinstance(error, ActionValidationError)
                and request.target.get("element_id")
                and not delivery.get("target_resolved")
                else "action_rejected"
                if isinstance(error, ActionValidationError)
                else "driver_failed"
            )
            delivery["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            errors.append({"phase": "dispatch", **delivery["error"]})

        monitor_started = time.monotonic()
        monitoring = self._monitor(request, before_capture.observation)
        timings["monitoring_ms"] = _elapsed_ms(monitor_started)

        capture_started = time.monotonic()
        try:
            after_capture = self.explorer.observe()
        except Exception as error:
            errors.append(
                {
                    "phase": "after_capture",
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
            after_capture = before_capture
            monitoring["session_unresponsive"] = True
        timings["after_capture_ms"] = _elapsed_ms(capture_started)

        effect = self._effect(before_capture.observation, after_capture.observation)
        health = self._health(
            expected,
            after_capture.observation,
            since_epoch=started_epoch,
            errors=errors,
        )
        classification = self._classification(
            delivery,
            monitoring,
            effect,
            health,
            request,
            after_capture.observation,
        )
        status = (
            "failed"
            if classification.startswith(("failed_", "app_", "timed_out_"))
            else "completed"
        )
        timings["total_ms"] = _elapsed_ms(started_monotonic)
        result: JsonObject = {
            "contract": "appium.action_result",
            "schema_version": 1,
            "action_id": action_id,
            "transition_id": None,
            "session_id": session_id,
            "status": status,
            "classification": classification,
            "request": request.to_dict(),
            "before": {
                "screen_id": request.screen_id,
                "fingerprint": expected.get("fingerprint"),
                "live_screen_id": before_capture.observation.get("screen_id"),
                "live_fingerprint": before_capture.observation.get("fingerprint"),
            },
            "delivery": delivery,
            "effect": effect,
            "app_health": health,
            "stability": monitoring,
            "timings": timings,
            "after": {
                "screen_id": after_capture.observation.get("screen_id"),
                "fingerprint": after_capture.observation.get("fingerprint"),
            },
            "errors": errors,
        }
        return result, after_capture

    def _resolve_target(
        self,
        document: JsonObject,
        target: JsonObject,
    ) -> ResolvedTarget:
        element_id = str(target.get("element_id"))
        captured = next(
            (
                item
                for item in document.get("elements", [])
                if item.get("id") == element_id
            ),
            None,
        )
        if captured is None:
            raise ActionValidationError(
                f"Captured element does not exist: {element_id}"
            )
        locators: list[tuple[str, str]] = []
        if captured.get("resource_id"):
            locators.append(("id", str(captured["resource_id"])))
        if captured.get("content_description"):
            locators.append(
                ("accessibility id", str(captured["content_description"]))
            )
        if captured.get("xpath"):
            locators.append(("xpath", str(captured["xpath"])))
        errors: list[str] = []
        for strategy, value in locators:
            try:
                live = self.driver.find_element(strategy, value)
                return ResolvedTarget(
                    captured=captured,
                    live=live,
                    locator={"strategy": strategy, "value": value},
                )
            except Exception as error:
                errors.append(f"{strategy}: {error}")
        if captured.get("bounds"):
            return ResolvedTarget(
                captured=captured,
                live=None,
                locator={"strategy": "coordinates", "value": captured["bounds"]},
            )
        raise ActionValidationError(
            f"Unable to resolve element {element_id}: {'; '.join(errors)}"
        )

    def _dispatch(
        self,
        request: ActionRequest,
        target: ResolvedTarget | None,
        document: JsonObject,
    ) -> Any:
        action = request.action
        params = request.parameters
        live = target.live if target else None
        remote_id = target.remote_id if target else None

        if action in {"tap", "focus", "select_option", "choose_dialog_action"}:
            return self._click(target)
        if action == "double_tap":
            return self._gesture("doubleClickGesture", target, params)
        if action == "long_press":
            return self._gesture("longClickGesture", target, params)
        if action == "type_text":
            return self._require_live(live, action).send_keys(
                _required_text(params)
            )
        if action == "replace_text":
            text = _required_text(params)
            if remote_id:
                return self.driver.execute_script(
                    "mobile: replaceElementValue",
                    {"elementId": remote_id, "text": text},
                )
            element = self._require_live(live, action)
            element.clear()
            return element.send_keys(text)
        if action == "clear_text":
            return self._require_live(live, action).clear()
        if action == "set_checked":
            desired = _required_bool(params, "checked")
            element = self._require_live(live, action)
            current = str(element.get_attribute("checked")).lower() == "true"
            return {"changed": False} if current == desired else element.click()
        if action == "submit":
            editor_action = str(params.get("editor_action") or "done")
            return self.driver.execute_script(
                "mobile: performEditorAction",
                {"action": editor_action},
            )
        if action in {"swipe", "scroll", "fling", "pinch_open", "pinch_close"}:
            method = {
                "swipe": "swipeGesture",
                "scroll": "scrollGesture",
                "fling": "flingGesture",
                "pinch_open": "pinchOpenGesture",
                "pinch_close": "pinchCloseGesture",
            }[action]
            return self._gesture(method, target, params)
        if action == "scroll_to":
            selector = str(params.get("selector") or "")
            strategy = str(params.get("strategy") or "text")
            if not selector:
                raise ActionValidationError(
                    "scroll_to requires parameters.selector."
                )
            return self.driver.execute_script(
                "mobile: scroll",
                {"strategy": strategy, "selector": selector},
            )
        if action in {"drag", "drag_and_drop"}:
            end = self._destination(params, document)
            arguments = {"endX": end["x"], "endY": end["y"]}
            if remote_id:
                arguments["elementId"] = remote_id
            else:
                arguments.update(self._point(target, params))
            if params.get("speed") is not None:
                arguments["speed"] = params["speed"]
            return self.driver.execute_script("mobile: dragGesture", arguments)
        if action == "gesture_sequence":
            actions = params.get("actions")
            if not isinstance(actions, list) or not actions:
                raise ActionValidationError(
                    "gesture_sequence requires parameters.actions."
                )
            return self.driver.perform_actions(actions)
        if action == "back":
            return self.driver.back()
        if action in {"home", "recent_apps", "press_key"}:
            keycode = {
                "home": 3,
                "recent_apps": 187,
            }.get(action, params.get("keycode"))
            if keycode is None:
                raise ActionValidationError(
                    "press_key requires parameters.keycode."
                )
            arguments = {"keycode": int(keycode)}
            for key in ("metastate", "flags", "isLongPress", "source"):
                if key in params:
                    arguments[key] = params[key]
            return self.driver.execute_script("mobile: pressKey", arguments)
        if action == "hide_keyboard":
            try:
                return self.driver.execute_script("mobile: hideKeyboard", {})
            except Exception:
                return self.driver.hide_keyboard()
        if action == "set_orientation":
            orientation = str(params.get("orientation") or "").upper()
            if orientation not in {"PORTRAIT", "LANDSCAPE"}:
                raise ActionValidationError(
                    "set_orientation requires PORTRAIT or LANDSCAPE."
                )
            self.driver.orientation = orientation
            return orientation
        if action in {"open_notifications", "close_system_panel"}:
            command = (
                "expandNotifications"
                if action == "open_notifications"
                else "collapse"
            )
            return self.driver.execute_script(
                "mobile: statusBar",
                {"command": command},
            )
        if action in {"accept_dialog", "allow_permission"} and target is None:
            return self.driver.switch_to.alert.accept()
        if action in {"dismiss_dialog", "deny_permission"} and target is None:
            return self.driver.switch_to.alert.dismiss()
        if action in {
            "accept_dialog",
            "dismiss_dialog",
            "allow_permission",
            "deny_permission",
        }:
            return self._click(target)
        if action in {"tap_outside", "tap_point"}:
            return self.driver.execute_script(
                "mobile: clickGesture",
                self._point(target, params),
            )
        if action == "long_press_point":
            return self.driver.execute_script(
                "mobile: longClickGesture",
                self._point(target, params),
            )
        if action in {"swipe_points", "drag_points"}:
            start = _required_point(params, "start")
            end = _required_point(params, "end")
            duration = int(_number(params.get("duration_ms"), 600))
            return self._pointer_swipe(start, end, duration)
        if action == "activate_app":
            return self.driver.activate_app(self._package(document, params))
        if action == "background_app":
            return self.driver.background_app(
                int(_number(params.get("seconds"), 1))
            )
        if action == "terminate_app":
            return self.driver.terminate_app(self._package(document, params))
        if action in {"restart_app", "return_to_start"}:
            package = self._package(document, params)
            self.driver.terminate_app(package)
            return self.driver.activate_app(package)
        if action == "reset_app":
            if params.get("allow_destructive") is not True:
                raise ActionValidationError(
                    "reset_app requires parameters.allow_destructive=true."
                )
            return self.driver.execute_script(
                "mobile: clearApp",
                {"appId": self._package(document, params)},
            )
        if action == "start_activity":
            intent = str(params.get("intent") or "")
            if not intent:
                raise ActionValidationError(
                    "start_activity requires parameters.intent."
                )
            return self.driver.execute_script(
                "mobile: startActivity",
                {"intent": intent},
            )
        if action == "open_deep_link":
            url = str(params.get("url") or "")
            if not url:
                raise ActionValidationError(
                    "open_deep_link requires parameters.url."
                )
            return self.driver.execute_script(
                "mobile: deepLink",
                {"url": url, "package": self._package(document, params)},
            )
        if action == "switch_context":
            return self.driver.switch_to.context(
                str(params.get("context") or "NATIVE_APP")
            )
        if action == "switch_window":
            handle = str(params.get("handle") or "")
            if not handle:
                raise ActionValidationError(
                    "switch_window requires parameters.handle."
                )
            return self.driver.switch_to.window(handle)
        if action == "switch_frame":
            frame = params.get("frame")
            return (
                self.driver.switch_to.default_content()
                if frame is None
                else self.driver.switch_to.frame(frame)
            )
        if action == "web_select_option":
            return self._click(target)
        if action == "web_submit":
            return self._require_live(live, action).submit()
        if action == "wait":
            seconds = min(30.0, max(0.0, _number(params.get("seconds"), 1)))
            time.sleep(seconds)
            return {"waited_ms": round(seconds * 1000)}
        if action in {
            "wait_until_stable",
            "capture_screen",
            "no_action",
            "mark_terminal",
            "assert_element",
        }:
            return {"performed": action}
        if action == "assert_screen":
            expected_package = params.get("package")
            if (
                expected_package
                and self.driver.current_package != expected_package
            ):
                raise ActionValidationError(
                    f"Expected package '{expected_package}'."
                )
            return {"asserted": True}
        if action == "recover":
            return self.driver.back()
        raise ActionValidationError(f"Action is not implemented: {action}")

    def _monitor(
        self,
        request: ActionRequest,
        before: JsonObject,
    ) -> JsonObject:
        started = time.monotonic()
        deadline = started + request.timeout_seconds
        previous_semantic: tuple[Any, ...] | None = None
        stable_count = 0
        sample_count = 0
        first_change_ms: int | None = None
        before_semantic = _observation_semantic_signature(before)
        before_screenshot = before.get("screenshot", {}).get("sha256")
        screenshot_changed = False
        latest: JsonObject = {}
        errors: list[JsonObject] = []

        while True:
            sample_count += 1
            try:
                latest = self._sample()
                semantic = tuple(latest["semantic_signature"])
                if semantic != before_semantic or (
                    latest.get("screenshot_sha256") != before_screenshot
                ):
                    screenshot_changed = screenshot_changed or (
                        latest.get("screenshot_sha256") != before_screenshot
                    )
                    if first_change_ms is None:
                        first_change_ms = _elapsed_ms(started)
                stable_count = (
                    stable_count + 1 if semantic == previous_semantic else 1
                )
                previous_semantic = semantic
                elapsed = time.monotonic() - started
                no_change_grace_met = elapsed >= min(
                    1.2,
                    request.timeout_seconds,
                )
                expected_conditions = _completion_conditions(request.completion)
                condition_met = (
                    _sample_conditions_met(
                        expected_conditions,
                        latest,
                        before,
                    )
                    if expected_conditions
                    else None
                )
                may_finish = (
                    condition_met
                    if expected_conditions
                    else first_change_ms is not None or no_change_grace_met
                )
                if stable_count >= request.stable_samples and may_finish:
                    latest.pop("hierarchy_xml", None)
                    return {
                        "stable": True,
                        "samples": sample_count,
                        "stable_samples": stable_count,
                        "duration_ms": _elapsed_ms(started),
                        "first_change_after_ms": first_change_ms,
                        "screenshot_changed": screenshot_changed,
                        "session_unresponsive": False,
                        "completion_condition_met": condition_met,
                        "latest": latest,
                        "errors": errors,
                    }
            except Exception as error:
                errors.append(
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                )
                if len(errors) >= 3:
                    return {
                        "stable": False,
                        "samples": sample_count,
                        "duration_ms": _elapsed_ms(started),
                        "first_change_after_ms": first_change_ms,
                        "screenshot_changed": screenshot_changed,
                        "session_unresponsive": True,
                        "latest": latest,
                        "errors": errors,
                    }
            if time.monotonic() >= deadline:
                latest.pop("hierarchy_xml", None)
                return {
                    "stable": False,
                    "reason": "timeout",
                    "samples": sample_count,
                    "stable_samples": stable_count,
                    "duration_ms": _elapsed_ms(started),
                    "first_change_after_ms": first_change_ms,
                    "screenshot_changed": screenshot_changed,
                    "session_unresponsive": False,
                    "completion_condition_met": False,
                    "latest": latest,
                    "errors": errors,
                }
            time.sleep(request.interval_seconds)

    def _sample(self) -> JsonObject:
        hierarchy = str(self.driver.page_source)
        screenshot = bytes(self.driver.get_screenshot_as_png())
        package = self.driver.current_package
        activity = self.driver.current_activity
        try:
            keyboard = bool(self.driver.is_keyboard_shown())
        except Exception:
            keyboard = None
        hierarchy_sha = _sha(hierarchy.encode("utf-8"))
        screenshot_sha = _sha(screenshot)
        return {
            "package": package,
            "activity": activity,
            "keyboard_visible": keyboard,
            "hierarchy_sha256": hierarchy_sha,
            "screenshot_sha256": screenshot_sha,
            "hierarchy_xml": hierarchy,
            "semantic_signature": [
                package,
                activity,
                keyboard,
                hierarchy_sha,
            ],
        }

    def _effect(
        self,
        before: JsonObject,
        after: JsonObject,
    ) -> JsonObject:
        before_screen = before.get("screen", {})
        after_screen = after.get("screen", {})
        hierarchy_changed = (
            before.get("hierarchy", {}).get("sha256")
            != after.get("hierarchy", {}).get("sha256")
        )
        screenshot_changed = (
            before.get("screenshot", {}).get("sha256")
            != after.get("screenshot", {}).get("sha256")
        )
        package_changed = (
            before_screen.get("package") != after_screen.get("package")
        )
        activity_changed = (
            before_screen.get("activity") != after_screen.get("activity")
        )
        dialog_appeared = (
            not before.get("system", {}).get("dialog", {}).get("present")
            and bool(after.get("system", {}).get("dialog", {}).get("present"))
        )
        permission_appeared = (
            not before.get("system", {})
            .get("permission_prompt", {})
            .get("present")
            and bool(
                after.get("system", {})
                .get("permission_prompt", {})
                .get("present")
            )
        )
        keyboard_changed = (
            before.get("system", {}).get("keyboard_visible")
            != after.get("system", {}).get("keyboard_visible")
        )
        observable_change = any(
            (
                hierarchy_changed,
                screenshot_changed,
                package_changed,
                activity_changed,
                dialog_appeared,
                permission_appeared,
                keyboard_changed,
            )
        )
        return {
            "status": (
                "screen_changed"
                if observable_change
                else "no_observable_change"
            ),
            "observable_change": observable_change,
            "hierarchy_changed": hierarchy_changed,
            "screenshot_changed": screenshot_changed,
            "package_changed": package_changed,
            "activity_changed": activity_changed,
            "dialog_appeared": dialog_appeared,
            "permission_prompt_appeared": permission_appeared,
            "keyboard_changed": keyboard_changed,
        }

    def _health(
        self,
        expected: JsonObject,
        after: JsonObject,
        *,
        since_epoch: float,
        errors: list[JsonObject],
    ) -> JsonObject:
        package = str(expected.get("screen", {}).get("package") or "")
        current_package = after.get("screen", {}).get("package")
        foreground = current_package == package
        adb_health: JsonObject = {}
        udid = (
            self.explorer.session.udid
            if self.explorer.session is not None
            else self.explorer.udid
        )
        if udid and package:
            try:
                adb_health = self.explorer.system_probe.collect_action_health(
                    str(udid),
                    package,
                    since_epoch=since_epoch,
                )
            except Exception as error:
                errors.append(
                    {
                        "phase": "health",
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                )
        crash = adb_health.get("crash_detected")
        anr = adb_health.get("anr_detected")
        process_alive = adb_health.get("process_alive")
        if crash:
            status = "crashed"
        elif anr:
            status = "anr"
        elif process_alive is False:
            status = "process_terminated"
        elif not foreground:
            status = "external_app_foreground"
        else:
            status = "healthy"
        return {
            "status": status,
            "expected_package": package,
            "current_package": current_package,
            "foreground": foreground,
            "process_alive": process_alive,
            "process_ids": adb_health.get("process_ids", []),
            "crash_detected": crash,
            "anr_detected": anr,
            "recent_error_log": adb_health.get("recent_error_log", ""),
        }

    @staticmethod
    def _classification(
        delivery: JsonObject,
        monitoring: JsonObject,
        effect: JsonObject,
        health: JsonObject,
        request: ActionRequest,
        after: JsonObject,
    ) -> str:
        if not delivery.get("acknowledged"):
            if delivery.get("status") == "action_rejected":
                return "failed_precondition"
            return (
                "failed_target_missing"
                if delivery.get("status") == "target_not_found"
                else "failed_appium_command"
            )
        if health.get("crash_detected"):
            return "app_crashed"
        if health.get("anr_detected"):
            return "app_anr"
        if (
            health.get("process_alive") is False
            and request.action in {"terminate_app", "reset_app"}
        ):
            return "succeeded_expected_condition"
        if (
            health.get("process_alive") is False
            and request.action not in {"terminate_app", "reset_app"}
        ):
            return "app_terminated"
        if monitoring.get("session_unresponsive"):
            return "failed_session_lost"
        if not monitoring.get("stable"):
            return "timed_out_unstable"
        expected_conditions = _completion_conditions(request.completion)
        if expected_conditions:
            if any(
                _condition_met(condition, effect, health, after)
                for condition in expected_conditions
            ):
                return "succeeded_expected_condition"
            return "timed_out_no_change"
        if effect.get("observable_change"):
            return (
                "succeeded_external_navigation"
                if effect.get("package_changed")
                else "succeeded_screen_changed"
            )
        return "acknowledged_no_observable_change"

    def _click(self, target: ResolvedTarget | None) -> Any:
        if target is None:
            raise ActionValidationError("This action requires a target.")
        if target.live is not None:
            return target.live.click()
        return self.driver.execute_script(
            "mobile: clickGesture",
            self._point(target, {}),
        )

    def _gesture(
        self,
        method: str,
        target: ResolvedTarget | None,
        params: JsonObject,
    ) -> Any:
        arguments = dict(params)
        if target and target.remote_id:
            arguments["elementId"] = target.remote_id
        elif target:
            arguments.update(self._rect(target))
        return self.driver.execute_script(f"mobile: {method}", arguments)

    def _point(
        self,
        target: ResolvedTarget | None,
        params: JsonObject,
    ) -> JsonObject:
        if isinstance(params.get("point"), dict):
            return _required_point(params, "point")
        if params.get("x") is not None and params.get("y") is not None:
            return {"x": int(params["x"]), "y": int(params["y"])}
        if target and target.captured.get("bounds", {}).get("center"):
            center = target.captured["bounds"]["center"]
            return {"x": int(center["x"]), "y": int(center["y"])}
        size = self.driver.get_window_size()
        return {"x": int(size["width"] / 2), "y": int(size["height"] / 2)}

    @staticmethod
    def _rect(target: ResolvedTarget) -> JsonObject:
        bounds = target.captured.get("bounds") or {}
        return {
            "left": int(bounds.get("left", 0)),
            "top": int(bounds.get("top", 0)),
            "width": int(bounds.get("width", 0)),
            "height": int(bounds.get("height", 0)),
        }

    def _destination(
        self,
        params: JsonObject,
        document: JsonObject,
    ) -> JsonObject:
        destination_id = params.get("destination_element_id")
        if destination_id:
            destination = next(
                (
                    item
                    for item in document.get("elements", [])
                    if item.get("id") == destination_id
                ),
                None,
            )
            if not destination or not destination.get("bounds", {}).get("center"):
                raise ActionValidationError(
                    "Destination element has no usable bounds."
                )
            return {
                "x": int(destination["bounds"]["center"]["x"]),
                "y": int(destination["bounds"]["center"]["y"]),
            }
        return _required_point(params, "end")

    def _pointer_swipe(
        self,
        start: JsonObject,
        end: JsonObject,
        duration_ms: int,
    ) -> Any:
        actions = [
            {
                "type": "pointer",
                "id": "finger1",
                "parameters": {"pointerType": "touch"},
                "actions": [
                    {
                        "type": "pointerMove",
                        "duration": 0,
                        "x": start["x"],
                        "y": start["y"],
                    },
                    {"type": "pointerDown", "button": 0},
                    {
                        "type": "pointerMove",
                        "duration": duration_ms,
                        "x": end["x"],
                        "y": end["y"],
                    },
                    {"type": "pointerUp", "button": 0},
                ],
            }
        ]
        return self.driver.perform_actions(actions)

    @staticmethod
    def _require_live(element: Any | None, action: str) -> Any:
        if element is None:
            raise ActionValidationError(
                f"Action '{action}' requires a live Appium element."
            )
        return element

    @staticmethod
    def _package(document: JsonObject, params: JsonObject) -> str:
        package = str(
            params.get("package")
            or document.get("screen", {}).get("package")
            or ""
        )
        if not package:
            raise ActionValidationError("No application package is available.")
        return package


def _semantic_screen_key(document: JsonObject) -> JsonObject:
    return {
        "package": document.get("screen", {}).get("package"),
        "activity": document.get("screen", {}).get("activity"),
        "orientation": document.get("screen", {}).get("orientation"),
        "elements_semantic_sha256": _elements_semantic_hash(
            document.get("elements", [])
        ),
    }


def _observation_semantic_signature(document: JsonObject) -> tuple[Any, ...]:
    return (
        document.get("screen", {}).get("package"),
        document.get("screen", {}).get("activity"),
        document.get("system", {}).get("keyboard_visible"),
        document.get("hierarchy", {}).get("sha256"),
    )


def _condition_met(
    condition: JsonObject,
    effect: JsonObject,
    health: JsonObject,
    after: JsonObject,
) -> bool:
    name = condition.get("condition")
    if name == "screen_changed":
        return bool(effect.get("observable_change"))
    if name == "activity_changed":
        return bool(effect.get("activity_changed"))
    if name == "dialog_present":
        return bool(after.get("system", {}).get("dialog", {}).get("present"))
    if name == "permission_prompt_present":
        return bool(
            after.get("system", {})
            .get("permission_prompt", {})
            .get("present")
        )
    if name == "keyboard_visible":
        return bool(after.get("system", {}).get("keyboard_visible")) == bool(
            condition.get("value", True)
        )
    if name == "app_foreground":
        return bool(health.get("foreground")) == bool(
            condition.get("value", True)
        )
    if name == "package":
        return (
            after.get("screen", {}).get("package") == condition.get("value")
        )
    if name == "element_present":
        element_id = condition.get("element_id")
        resource_id = condition.get("resource_id")
        text = condition.get("text")
        return any(
            (element_id and item.get("id") == element_id)
            or (resource_id and item.get("resource_id") == resource_id)
            or (text and item.get("text") == text)
            for item in after.get("elements", [])
        )
    return False


def _completion_conditions(completion: JsonObject) -> list[JsonObject]:
    any_of = completion.get("any_of")
    if isinstance(any_of, list):
        return [item for item in any_of if isinstance(item, dict)]
    if completion.get("condition"):
        return [completion]
    return []


def _sample_conditions_met(
    conditions: list[JsonObject],
    sample: JsonObject,
    before: JsonObject,
) -> bool:
    hierarchy = str(sample.get("hierarchy_xml") or "")
    before_screen = before.get("screen", {})
    before_semantic = _observation_semantic_signature(before)
    sample_semantic = tuple(sample.get("semantic_signature") or ())
    for condition in conditions:
        name = condition.get("condition")
        if name == "screen_changed" and sample_semantic != before_semantic:
            return True
        if (
            name == "activity_changed"
            and sample.get("activity") != before_screen.get("activity")
        ):
            return True
        if name == "package" and sample.get("package") == condition.get("value"):
            return True
        if name == "keyboard_visible" and bool(
            sample.get("keyboard_visible")
        ) == bool(condition.get("value", True)):
            return True
        if name == "app_foreground" and (
            sample.get("package") == before_screen.get("package")
        ) == bool(condition.get("value", True)):
            return True
        if name == "dialog_present" and (
            "dialog" in hierarchy.lower() or "alert" in hierarchy.lower()
        ):
            return True
        if name == "permission_prompt_present" and (
            "permissioncontroller" in hierarchy.lower()
        ):
            return True
        if name == "element_present":
            expected_element = next(
                (
                    item
                    for item in before.get("elements", [])
                    if item.get("id") == condition.get("element_id")
                ),
                None,
            )
            selectors = [
                str(condition.get("resource_id") or ""),
                str(condition.get("text") or ""),
            ]
            if expected_element:
                selectors.extend(
                    [
                        str(expected_element.get("resource_id") or ""),
                        str(expected_element.get("text") or ""),
                        str(expected_element.get("content_description") or ""),
                    ]
                )
            if any(value and value in hierarchy for value in selectors):
                return True
    return False


def _required_text(params: JsonObject) -> str:
    if "text" not in params:
        raise ActionValidationError("This action requires parameters.text.")
    return str(params["text"])


def _required_bool(params: JsonObject, name: str) -> bool:
    value = params.get(name)
    if not isinstance(value, bool):
        raise ActionValidationError(
            f"This action requires boolean parameters.{name}."
        )
    return value


def _required_point(params: JsonObject, name: str) -> JsonObject:
    value = params.get(name)
    if not isinstance(value, dict) or "x" not in value or "y" not in value:
        raise ActionValidationError(
            f"This action requires parameters.{name}.x and .y."
        )
    return {"x": int(value["x"]), "y": int(value["y"])}


def _object(value: Any, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ActionValidationError(f"'{name}' must be an object.")
    return value


def _number(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ActionValidationError("Expected a numeric value.") from error


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _elements_semantic_hash(elements: Any) -> str:
    if not isinstance(elements, list):
        elements = []
    stable_fields = (
        "depth",
        "class",
        "package",
        "source",
        "resource_id",
        "text",
        "content_description",
        "bounds_raw",
        "clickable",
        "long_clickable",
        "checkable",
        "checked",
        "enabled",
        "focusable",
        "focused",
        "scrollable",
        "selected",
        "password",
        "displayed",
        "editable",
        "interaction",
    )
    payload = [
        {field: item.get(field) for field in stable_fields}
        for item in elements
        if isinstance(item, dict)
    ]
    return _sha(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)


