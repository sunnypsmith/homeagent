from __future__ import annotations

from typing import List

from home_agent.modules.base import Module
from home_agent.modules.announcements import Announcements
from home_agent.modules.daily_briefing import DailyBriefing
from home_agent.modules.heartbeat import Heartbeat


def default_modules() -> List[Module]:
    # Central registry keeps startup predictable and debuggable.
    return [
        Announcements(),
        Heartbeat(),
        DailyBriefing(),
    ]

