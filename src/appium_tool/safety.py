from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from appium_tool.auth import Principal


class Risk(str, Enum):
    READ_ONLY = "read_only"
    SAFE = "safe"
    CONTROLLED = "controlled"
    DESTRUCTIVE = "destructive"
    SYSTEM = "system"


class SafetyViolation(PermissionError):
    pass


@dataclass(frozen=True)
class SafetyPolicy:
    """Deterministic authorization rules applied before tool dispatch."""

    def authorize(
        self,
        *,
        risk: Risk,
        principal: Principal,
        confirmed: bool,
    ) -> None:
        if not principal.has("tools"):
            raise SafetyViolation("The principal cannot invoke tools.")
        if risk in {Risk.READ_ONLY, Risk.SAFE}:
            return
        if not confirmed:
            raise SafetyViolation(
                f"Tool risk '{risk}' requires confirm=true."
            )
        if risk in {Risk.DESTRUCTIVE, Risk.SYSTEM} and not principal.has(
            "admin"
        ):
            raise SafetyViolation(
                f"Tool risk '{risk}' requires the admin token."
            )
