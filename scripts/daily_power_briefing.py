# -*- coding: utf-8 -*-
"""定时发送入口；晨报分析与卡片生成位于服务模块供聊天指令复用。"""

from __future__ import annotations

import asyncio
import sys

from services.weather_bot.power_briefing import (
    CHAT_TARGETS,
    MARKET_POINTS,
    PROVINCES,
    MarketInsight,
    _confidence_label,
    _continuous_windows,
    build_briefing_card,
    go,
)

__all__ = [
    "CHAT_TARGETS",
    "MARKET_POINTS",
    "PROVINCES",
    "MarketInsight",
    "_confidence_label",
    "_continuous_windows",
    "build_briefing_card",
    "go",
]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(go())
