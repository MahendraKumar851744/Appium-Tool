from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from appium_tool.auth import current_principal
from appium_tool.exploration.actions import SessionManager
from appium_tool.exploration.context_export import ScreenContextExporter
from appium_tool.registry import ToolRegistry
from appium_tool.runtime import RuntimeManager
from appium_tool.types import JsonObject


def create_mcp_server(
    registry: ToolRegistry,
    *,
    sessions: SessionManager,
    contexts: ScreenContextExporter,
    runtime: RuntimeManager,
    allowed_hosts: tuple[str, ...],
) -> FastMCP:
    """Generate MCP tools from the same registry used by the REST interface."""

    mcp = FastMCP(
        "Appium Tool",
        instructions=(
            "Open an installed package to obtain a session_id and screen_id. "
            "Pass both to each action. Action results contain stabilized "
            "before/after evidence and the next screen_id."
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(allowed_hosts),
        ),
    )

    @mcp.tool(
        name="open_session",
        description="Open an installed Android package and capture its first screen.",
    )
    def open_session(
        package_id: str,
        device_id: str | None = None,
    ) -> JsonObject:
        payload: JsonObject = {"package_id": package_id}
        if device_id:
            payload["device_id"] = device_id
        return sessions.launch(payload)

    @mcp.tool(
        name="close_session",
        description="Close an active Appium session and release its driver.",
    )
    def close_session(session_id: str) -> JsonObject:
        return {
            "session_id": session_id,
            "closed": sessions.close(session_id),
        }

    @mcp.tool(
        name="get_screen_context",
        description="Return compact LLM-ready Markdown for a captured screen.",
    )
    def get_screen_context(
        session_id: str,
        screen_id: str,
        options: dict[str, Any] | None = None,
    ) -> JsonObject:
        return contexts.export(
            {
                "screen_ref": {
                    "session_id": session_id,
                    "screen_id": screen_id,
                },
                "options": options or {},
            }
        )

    @mcp.tool(
        name="runtime_status",
        description=(
            "Return Android SDK, emulator, Appium server, connected device, "
            "and managed runtime status. Requires the admin token."
        ),
    )
    def runtime_status() -> JsonObject:
        _require_admin_principal()
        return runtime.status()

    @mcp.tool(
        name="start_runtime",
        description=(
            "Start the managed Android runtime. By default this starts the "
            "configured emulator and local Appium server. Returns a job_id; "
            "poll get_runtime_job until status is succeeded or failed. "
            "Requires the admin token."
        ),
    )
    def start_runtime(
        start_emulator: bool = True,
        device_id: str | None = None,
    ) -> JsonObject:
        _require_admin_principal()
        payload: JsonObject = {"start_emulator": start_emulator}
        if device_id:
            payload["device_id"] = device_id
        job, reused = runtime.start(payload)
        return {**job, "reused": reused}

    @mcp.tool(
        name="start_emulator",
        description=(
            "Ensure the configured Android emulator and local Appium server "
            "are ready in one call. This checks runtime_status, starts the "
            "emulator/Appium runtime when needed, polls the startup job, and "
            "returns the final runtime status. Use this before open_session. "
            "Requires the admin token."
        ),
    )
    def start_emulator(
        timeout_seconds: int = 300,
        poll_interval_seconds: int = 2,
    ) -> JsonObject:
        _require_admin_principal()
        return _ensure_runtime_ready(
            runtime,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    @mcp.tool(
        name="get_runtime_job",
        description=(
            "Return status, output, result, or error for a runtime job returned "
            "by start_runtime or start_emulator. Requires the admin token."
        ),
    )
    def get_runtime_job(job_id: str) -> JsonObject:
        _require_admin_principal()
        return runtime.get_job(job_id)

    @mcp.tool(
        name="stop_runtime",
        description=(
            "Stop managed Appium and/or emulator processes started by the "
            "runtime manager. Requires the admin token."
        ),
    )
    def stop_runtime(
        stop_appium: bool = True,
        stop_emulator: bool = True,
    ) -> JsonObject:
        _require_admin_principal()
        return runtime.stop(
            {
                "stop_appium": stop_appium,
                "stop_emulator": stop_emulator,
            }
        )

    for spec in registry.list():
        mcp.add_tool(
            _action_tool(registry, spec.name),
            name=spec.name,
            description=f"{spec.description} Safety risk: {spec.risk.value}.",
        )
    return mcp


def _action_tool(registry: ToolRegistry, tool_name: str):
    def invoke(
        session_id: str,
        screen_id: str,
        target: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        completion: dict[str, Any] | None = None,
        confirm: bool = False,
    ) -> JsonObject:
        principal = current_principal.get()
        if principal is None:
            raise PermissionError("Authenticated MCP request required.")
        return registry.invoke(
            tool_name,
            {
                "session_id": session_id,
                "screen_id": screen_id,
                "target": target or {},
                "parameters": parameters or {},
                "completion": completion or {},
                "confirm": confirm,
            },
            principal=principal,
        )

    invoke.__name__ = f"appium_{tool_name}"
    return invoke


def _require_admin_principal() -> None:
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("Authenticated MCP request required.")
    if not principal.has("admin"):
        raise PermissionError("This MCP tool requires the admin token.")


def _ensure_runtime_ready(
    runtime: RuntimeManager,
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> JsonObject:
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("'timeout_seconds' must be a positive integer.")
    if not isinstance(poll_interval_seconds, int) or poll_interval_seconds < 1:
        raise ValueError("'poll_interval_seconds' must be a positive integer.")

    initial = runtime.status()
    if initial.get("ready"):
        return {
            "contract": "appium.runtime_ready_result",
            "schema_version": 1,
            "status": "ready",
            "started": False,
            "reused_job": False,
            "job": None,
            "runtime": initial,
        }

    job, reused = runtime.start({"start_emulator": True})
    deadline = time.monotonic() + timeout_seconds
    latest_job = job

    while time.monotonic() < deadline:
        latest_job = runtime.get_job(str(job["job_id"]))
        status = latest_job.get("status")
        if status == "succeeded":
            final = runtime.status()
            if not final.get("ready"):
                raise RuntimeError(
                    "Runtime startup job succeeded, but no ready compatible "
                    "Android device is available."
                )
            return {
                "contract": "appium.runtime_ready_result",
                "schema_version": 1,
                "status": "ready",
                "started": True,
                "reused_job": reused,
                "job": latest_job,
                "runtime": final,
            }
        if status == "failed":
            return {
                "contract": "appium.runtime_ready_result",
                "schema_version": 1,
                "status": "failed",
                "started": True,
                "reused_job": reused,
                "job": latest_job,
                "runtime": runtime.status(),
                "error": latest_job.get("error"),
            }
        time.sleep(poll_interval_seconds)

    return {
        "contract": "appium.runtime_ready_result",
        "schema_version": 1,
        "status": "timeout",
        "started": True,
        "reused_job": reused,
        "job": latest_job,
        "runtime": runtime.status(),
        "error": (
            "Timed out waiting for the Android emulator and Appium runtime "
            f"to become ready after {timeout_seconds} seconds."
        ),
    }
