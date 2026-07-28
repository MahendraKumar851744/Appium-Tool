from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    code = "platform_error"
    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class RequestValidationError(PlatformError):
    code = "validation_error"
    status_code = 400


class ResourceNotFoundError(PlatformError):
    code = "not_found"
    status_code = 404


class ResourceConflictError(PlatformError):
    code = "conflict"
    status_code = 409


class AccessDeniedError(PlatformError):
    code = "access_denied"
    status_code = 403


class AuthenticationError(PlatformError):
    code = "authentication_required"
    status_code = 401


class SafetyPolicyError(PlatformError):
    code = "safety_policy_denied"
    status_code = 403


class ModuleExecutionError(PlatformError):
    code = "module_execution_failed"
    status_code = 422


class ExternalServiceError(PlatformError):
    code = "external_service_failed"
    status_code = 502


class UnsupportedScreenContractError(PlatformError):
    code = "unsupported_screen_contract"
    status_code = 422


class AutomationContractPendingError(PlatformError):
    code = "automation_contract_pending"
    status_code = 501
