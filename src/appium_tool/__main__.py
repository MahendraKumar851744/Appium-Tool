from __future__ import annotations

import uvicorn

from appium_tool.config import Settings
from appium_tool.service import create_app


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
