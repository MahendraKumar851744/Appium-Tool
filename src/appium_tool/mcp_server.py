from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from appium_tool.auth import current_principal
from appium_tool.exploration.actions import SessionManager
from appium_tool.exploration.context_export import ScreenContextExporter
from appium_tool.registry import ToolRegistry
from appium_tool.types import JsonObject


def create_mcp_server(
    registry: ToolRegistry,
    *,
    sessions: SessionManager,
    contexts: ScreenContextExporter,
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
