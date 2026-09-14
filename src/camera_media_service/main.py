from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping

from camera_media_service.app import WebSocketServer, create_app
from camera_media_service.settings import Settings
from camera_media_service.telemetry import configure_logging


async def run(
    environ: Mapping[str, str] | None = None,
    app_factory: Callable[[Settings], WebSocketServer] = create_app,
) -> None:
    settings = Settings.from_env(os.environ if environ is None else environ)
    configure_logging(settings.log_format)
    app = app_factory(settings)
    async with app.serve() as server:
        await server.server.serve_forever()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
