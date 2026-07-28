from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _paths(value: str | None, default: Path) -> tuple[Path, ...]:
    raw = value or str(default)
    return tuple(
        Path(item).expanduser().resolve()
        for item in raw.split(os.pathsep)
        if item.strip()
    )


@dataclass(frozen=True)
class Settings:
    project_root: Path
    artifact_root: Path
    apk_roots: tuple[Path, ...]
    service_token: str
    admin_token: str
    session_ttl_seconds: int
    host: str
    port: int
    allowed_hosts: tuple[str, ...] = (
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(
            os.environ.get(
                "APPIUM_TOOL_ROOT",
                Path(__file__).resolve().parents[2],
            )
        ).expanduser().resolve()
        service_token = os.environ.get("APPIUM_TOOL_SERVICE_TOKEN", "")
        admin_token = os.environ.get("APPIUM_TOOL_ADMIN_TOKEN", "")
        if not service_token or not admin_token:
            raise RuntimeError(
                "Set APPIUM_TOOL_SERVICE_TOKEN and "
                "APPIUM_TOOL_ADMIN_TOKEN before starting the service."
            )
        return cls(
            project_root=root,
            artifact_root=Path(
                os.environ.get(
                    "APPIUM_TOOL_ARTIFACT_ROOT",
                    root / "artifacts" / "sessions",
                )
            ).expanduser().resolve(),
            apk_roots=_paths(
                os.environ.get("APPIUM_TOOL_APK_ROOTS"),
                root / "assets" / "apps",
            ),
            service_token=service_token,
            admin_token=admin_token,
            session_ttl_seconds=int(
                os.environ.get("APPIUM_TOOL_SESSION_TTL_SECONDS", "1800")
            ),
            host=os.environ.get("APPIUM_TOOL_HOST", "127.0.0.1"),
            port=int(os.environ.get("APPIUM_TOOL_PORT", "8000")),
            allowed_hosts=tuple(
                item.strip()
                for item in os.environ.get(
                    "APPIUM_TOOL_ALLOWED_HOSTS",
                    "127.0.0.1,127.0.0.1:*,localhost,localhost:*,[::1],[::1]:*",
                ).split(",")
                if item.strip()
            ),
        )
