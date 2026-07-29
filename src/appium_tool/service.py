from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from appium_tool.apps import AndroidAppManager
from appium_tool.auth import (
    BearerAuthMiddleware,
    Principal,
    TokenAuthenticator,
)
from appium_tool.config import Settings
from appium_tool.device_selection import AndroidDeviceCoordinator
from appium_tool.exploration.actions import SessionManager
from appium_tool.exploration.context_export import ScreenContextExporter
from appium_tool.mcp_server import create_mcp_server
from appium_tool.registry import ToolRegistry
from appium_tool.runtime import RuntimeManager
from appium_tool.safety import SafetyViolation


def _principal(request: Request) -> Principal:
    return request.state.principal


def _require_admin(request: Request) -> None:
    if not _principal(request).has("admin"):
        raise SafetyViolation("This operation requires the admin token.")


async def _payload(request: Request) -> dict[str, Any]:
    value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("The JSON request body must be an object.")
    return value


async def _forbidden(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    return _error_response(error, 403)


async def _bad_request(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    return _error_response(error, 400)


async def _not_found(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    return _error_response(error, 404)


async def _internal_error(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    return _error_response(error, 500)


def create_app(
    settings: Settings | None = None,
    *,
    session_manager: SessionManager | None = None,
    runtime_manager: RuntimeManager | None = None,
    app_manager: AndroidAppManager | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    runtime = runtime_manager or RuntimeManager(settings.project_root)
    sessions = session_manager or SessionManager(
        settings.artifact_root,
        ttl_seconds=settings.session_ttl_seconds,
    )
    apps = app_manager or AndroidAppManager(
        allowed_apk_roots=list(settings.apk_roots),
        runtime_manager=runtime,
        action_manager=sessions,
        device_coordinator=AndroidDeviceCoordinator(
            project_root=settings.project_root,
            runtime_manager=runtime,
        ),
    )
    registry = ToolRegistry(sessions)
    contexts = ScreenContextExporter(settings.artifact_root)

    async def index(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "appium-tool",
                "version": "1.0.0",
                "health": "/health",
                "rest": "/api/v1",
                "mcp": "/mcp",
            }
        )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def list_tools(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"tools": [spec.to_dict() for spec in registry.list()]}
        )

    async def invoke_tool(request: Request) -> JSONResponse:
        result = registry.invoke(
            request.path_params["tool_name"],
            await _payload(request),
            principal=_principal(request),
        )
        return JSONResponse(result)

    async def open_session(request: Request) -> JSONResponse:
        return JSONResponse(sessions.launch(await _payload(request)), status_code=201)

    async def get_session(request: Request) -> JSONResponse:
        return JSONResponse(
            sessions.session_status(request.path_params["session_id"])
        )

    async def close_session(request: Request) -> JSONResponse:
        closed = sessions.close(request.path_params["session_id"])
        return JSONResponse(
            {
                "session_id": request.path_params["session_id"],
                "status": "closed" if closed else "not_active",
            }
        )

    async def execute_action(request: Request) -> JSONResponse:
        payload = await _payload(request)
        action = payload.pop("action", None)
        if not isinstance(action, str) or not action:
            raise ValueError("'action' must be a non-empty string.")
        return JSONResponse(
            registry.invoke(
                action,
                {
                    **payload,
                    "session_id": request.path_params["session_id"],
                },
                principal=_principal(request),
            )
        )

    async def screen_context(request: Request) -> JSONResponse:
        payload = await _payload(request)
        screen_id = payload.pop("screen_id", None)
        if not isinstance(screen_id, str) or not screen_id:
            raise ValueError("'screen_id' must be a non-empty string.")
        unexpected = sorted(set(payload) - {"options"})
        if unexpected:
            raise ValueError(
                f"Unsupported context fields: {', '.join(unexpected)}."
            )
        return JSONResponse(
            contexts.export(
                {
                    "screen_ref": {
                        "session_id": request.path_params["session_id"],
                        "screen_id": screen_id,
                    },
                    "options": payload.get("options", {}),
                }
            )
        )

    async def runtime_status(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(runtime.status())

    async def runtime_provision(request: Request) -> JSONResponse:
        _require_admin(request)
        result, created = runtime.provision(await _payload(request))
        return JSONResponse(result, status_code=202 if created else 200)

    async def runtime_start(request: Request) -> JSONResponse:
        _require_admin(request)
        result, created = runtime.start(await _payload(request))
        return JSONResponse(result, status_code=202 if created else 200)

    async def runtime_stop(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(runtime.stop(await _payload(request)))

    async def runtime_job(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(runtime.get_job(request.path_params["job_id"]))

    async def app_preflight(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(apps.preflight(await _payload(request)))

    async def app_prepare(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(apps.prepare_device(await _payload(request)))

    async def app_install(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(apps.install(await _payload(request)))

    async def app_get(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(
            apps.get(
                request.path_params["package_id"],
                device_id=request.query_params.get("device_id"),
            )
        )

    async def app_uninstall(request: Request) -> JSONResponse:
        _require_admin(request)
        return JSONResponse(
            apps.uninstall(
                request.path_params["package_id"],
                await _payload(request),
            )
        )

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/api/v1/tools", list_tools),
        Route("/api/v1/tools/{tool_name:str}/invoke", invoke_tool, methods=["POST"]),
        Route("/api/v1/sessions", open_session, methods=["POST"]),
        Route("/api/v1/sessions/{session_id:str}", get_session),
        Route(
            "/api/v1/sessions/{session_id:str}",
            close_session,
            methods=["DELETE"],
        ),
        Route(
            "/api/v1/sessions/{session_id:str}/actions",
            execute_action,
            methods=["POST"],
        ),
        Route(
            "/api/v1/sessions/{session_id:str}/context",
            screen_context,
            methods=["POST"],
        ),
        Route("/api/v1/runtime", runtime_status),
        Route("/api/v1/runtime/provision", runtime_provision, methods=["POST"]),
        Route("/api/v1/runtime/start", runtime_start, methods=["POST"]),
        Route("/api/v1/runtime/stop", runtime_stop, methods=["POST"]),
        Route("/api/v1/runtime/jobs/{job_id:str}", runtime_job),
        Route("/api/v1/apps/preflight", app_preflight, methods=["POST"]),
        Route("/api/v1/apps/prepare-device", app_prepare, methods=["POST"]),
        Route("/api/v1/apps/install", app_install, methods=["POST"]),
        Route("/api/v1/apps/{package_id:str}", app_get),
        Route(
            "/api/v1/apps/{package_id:str}",
            app_uninstall,
            methods=["DELETE"],
        ),
    ]

    mcp_server = create_mcp_server(
        registry,
        sessions=sessions,
        contexts=contexts,
        runtime=runtime,
        allowed_hosts=settings.allowed_hosts,
    )
    mcp_app = mcp_server.streamable_http_app()
    routes.extend(mcp_app.routes)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async def reap_idle_sessions() -> None:
            interval = max(5, min(30, settings.session_ttl_seconds // 2))
            while True:
                await asyncio.sleep(interval)
                sessions.expire_idle()

        reaper = asyncio.create_task(reap_idle_sessions())
        async with mcp_server.session_manager.run():
            try:
                yield
            finally:
                reaper.cancel()
                with suppress(asyncio.CancelledError):
                    await reaper
                sessions.close_all()

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={
            SafetyViolation: _forbidden,
            PermissionError: _forbidden,
            ValueError: _bad_request,
            RuntimeError: _bad_request,
            KeyError: _not_found,
            FileNotFoundError: _not_found,
            Exception: _internal_error,
        },
    )
    app.add_middleware(
        BearerAuthMiddleware,
        authenticator=TokenAuthenticator(
            settings.service_token,
            settings.admin_token,
        ),
    )
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.runtime = runtime
    app.state.apps = apps
    app.state.registry = registry
    app.state.contexts = contexts
    app.state.mcp_server = mcp_server

    return app


def _error_response(error: Exception, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": type(error).__name__,
                "message": str(error),
            }
        },
        status_code=status_code,
    )
