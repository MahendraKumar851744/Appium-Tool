from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ApkMetadata:

    path: Path | None

    sha256: str

    size_bytes: int

    package: str | None = None

    launch_activity: str | None = None

    label: str | None = None

    version_name: str | None = None

    version_code: str | None = None

    min_sdk: str | None = None

    target_sdk: str | None = None

    native_abis: tuple[str, ...] = ()

    required_features: tuple[str, ...] = ()

    supports_16kb_page_size: bool | None = None

    inspector: str | None = None

    warnings: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:

        data = asdict(self)

        data["path"] = str(self.path) if self.path is not None else None

        data["native_abis"] = list(self.native_abis)

        data["required_features"] = list(self.required_features)

        data["warnings"] = list(self.warnings)

        return data


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    server_url: str
    device_name: str
    udid: str | None
    capabilities: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class ScreenCapture:
    observation: JsonObject
    hierarchy_xml: str
    screenshot_png: bytes

    @property
    def screen_id(self) -> str:
        return str(self.observation["screen_id"])


@dataclass(frozen=True)
class CaptureResult:
    session_id: str
    screen_id: str
    artifact_root: Path
    screen_root: Path
    document_path: Path
    viewer_path: Path
    element_count: int
    status: str = "completed"

    def to_dict(self) -> JsonObject:
        return {
            "session_id": self.session_id,
            "screen_id": self.screen_id,
            "status": self.status,
            "artifact_root": str(self.artifact_root),
            "screen_root": str(self.screen_root),
            "document_path": str(self.document_path),
            "viewer_path": str(self.viewer_path),
            "element_count": self.element_count,
        }
