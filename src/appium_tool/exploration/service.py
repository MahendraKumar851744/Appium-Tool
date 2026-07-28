from __future__ import annotations

from pathlib import Path

from appium_tool.exploration.appium import AppiumExplorer
from appium_tool.exploration.models import CaptureResult
from appium_tool.exploration.storage import SessionStore


class ExplorationService:
    """Milestone 1 orchestration: start, capture one screen, persist, close."""

    def __init__(
        self,
        explorer: AppiumExplorer,
        store: SessionStore,
    ) -> None:
        self.explorer = explorer
        self.store = store

    def capture_first_screen(self, apk_path: str | Path) -> CaptureResult:
        try:
            apk, session = self.explorer.start(apk_path)
            self.store.create_run(apk, session)
            capture = self.explorer.observe()
            result = self.store.save_screen(capture)
            self.store.complete(result)
            return result
        except Exception as error:
            self.store.fail(error)
            raise
        finally:
            self.explorer.close()


def capture_first_screen(
    apk_path: str | Path,
    *,
    output_root: str | Path,
    server_url: str = "http://127.0.0.1:4723",
    device_name: str = "Android",
    udid: str | None = None,
    keep_data: bool = False,
    stability_timeout: float = 15.0,
) -> CaptureResult:
    explorer = AppiumExplorer(
        server_url=server_url,
        device_name=device_name,
        udid=udid,
        keep_data=keep_data,
        stability_timeout=stability_timeout,
    )
    store = SessionStore(output_root)
    return ExplorationService(explorer, store).capture_first_screen(apk_path)

