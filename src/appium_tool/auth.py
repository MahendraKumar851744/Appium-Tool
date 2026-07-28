from __future__ import annotations

import hmac
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


current_principal: ContextVar[Principal | None] = ContextVar(
    "appium_tool_principal",
    default=None,
)


class TokenAuthenticator:
    def __init__(self, service_token: str, admin_token: str) -> None:
        self.service_token = service_token
        self.admin_token = admin_token

    def authenticate(self, authorization: str | None) -> Principal | None:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            return None
        token = authorization[len(prefix) :]
        if hmac.compare_digest(token, self.admin_token):
            return Principal("admin", frozenset({"tools", "admin"}))
        if hmac.compare_digest(token, self.service_token):
            return Principal("service", frozenset({"tools"}))
        return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, authenticator: TokenAuthenticator) -> None:
        super().__init__(app)
        self.authenticator = authenticator

    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/", "/health"}:
            return await call_next(request)
        principal = self.authenticator.authenticate(
            request.headers.get("authorization")
        )
        if principal is None:
            return JSONResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "A valid Bearer token is required.",
                    }
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.principal = principal
        token = current_principal.set(principal)
        try:
            return await call_next(request)
        finally:
            current_principal.reset(token)
