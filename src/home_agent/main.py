from __future__ import annotations

import asyncio

from home_agent.app import HomeAgentApp
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging


def main() -> int:
    settings = AppSettings()
    configure_logging(settings.log_level)
    app = HomeAgentApp(settings)
    asyncio.run(app.run())
    return 0

