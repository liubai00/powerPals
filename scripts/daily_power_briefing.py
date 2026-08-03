# -*- coding: utf-8 -*-
"""电力气象晨报定时入口：预计算与固定群发送显式分离。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

from services.weather_bot.briefing_cache import BriefingCache
from services.weather_bot.power_briefing import (
    MARKET_POINTS,
    PROVINCES,
    SHANGHAI_TZ,
    MarketInsight,
    _confidence_label,
    _continuous_windows,
    briefing_cache_key,
    build_briefing_card,
    get_or_generate_briefing,
)


CHAT_TARGETS = [
    ("国峰运营-AI 实验群", "oc_8a6645e28915e2eefe7768e41773ec08"),
    ("小可爱电力社区 Power Pals", "oc_fe8abbef9959e5439c4797c237ad5df8"),
]

__all__ = [
    "CHAT_TARGETS",
    "MARKET_POINTS",
    "PROVINCES",
    "MarketInsight",
    "_confidence_label",
    "_continuous_windows",
    "briefing_cache_key",
    "build_briefing_card",
    "go",
]


def _remember_scheduled_briefing_thread(
    chat_id: str,
    message_id: str,
    cache_key: str,
    generated_at: str | None,
) -> None:
    """Allow replies to a scheduled group card to read that exact cached snapshot."""
    if not chat_id or not message_id or not cache_key:
        return
    from services.weather_bot import memory as weather_memory
    from services.weather_bot.main import (
        WEATHER_FORECAST_BOT_ROLE,
        _conversation_key,
    )

    state_key = _conversation_key(
        WEATHER_FORECAST_BOT_ROLE,
        chat_id,
        message_id,
        "*",
        "group",
    )
    weather_memory.save_conversation_state(
        state_key,
        {
            "state_version": 1,
            "last_power_briefing_cache_key": cache_key,
            "last_power_briefing_generated_at": generated_at,
            "source": "scheduled_briefing",
        },
    )


async def go(mode: str | None = None) -> None:
    from services.weather_bot import main as m

    settings = m.Settings()
    service = m.ForecastService(settings=settings)
    typhoon_client = m.TyphoonClient(settings.qweather_api_key, settings.qweather_api_host)
    cache = BriefingCache(
        settings.power_briefing_cache_db,
        ttl_seconds=settings.power_briefing_cache_ttl_seconds,
    )
    start_date = datetime.now(SHANGHAI_TZ).date().isoformat()
    snapshot, cache_hit = await get_or_generate_briefing(
        service,
        typhoon_client,
        start_date,
        cache=cache,
    )
    coverage = snapshot["coverage"]
    statistics = snapshot["statistics"]
    print(
        "BRIEFING cache=%s date=%s areas=%d/%d markets=%d/%d points=%d/%d classified=%d/%d"
        % (
            "hit" if cache_hit else "generated",
            start_date,
            coverage["provincial_areas"]["covered"],
            coverage["provincial_areas"]["total"],
            coverage["markets"]["covered"],
            coverage["markets"]["total"],
            coverage["points"]["covered"],
            coverage["points"]["total"],
            statistics["classified_markets"],
            statistics["configured_markets"],
        )
    )

    selected_mode = (mode or os.getenv("POWER_BRIEFING_MODE") or "precompute").strip().lower()
    dry_run = os.getenv("DRY_RUN") == "1"
    if selected_mode == "precompute" or dry_run:
        card = snapshot["summary_card"]
        print(
            "NO-SEND mode=%s dry_run=%s title=%s"
            % (
                selected_mode,
                dry_run,
                card["card"]["header"]["title"]["content"],
            )
        )
        for element in card["card"]["elements"]:
            if element.get("tag") == "div":
                print("---")
                print(element["text"]["content"])
        return
    if selected_mode != "send":
        raise ValueError(f"unsupported POWER_BRIEFING_MODE: {selected_mode}")
    if os.getenv("POWER_BRIEFING_ALLOW_SEND") != "1":
        print("NO-SEND mode=send reason=POWER_BRIEFING_ALLOW_SEND_disabled")
        return

    legacy = m._legacy_feishu_account(settings, None)
    account = m._role_feishu_account(settings, m.FEISHU_WEATHER_BOT, legacy)
    feishu = m.FeishuClient(settings, account)
    for chat_name, chat_id in CHAT_TARGETS:
        try:
            message_id = await feishu.send_interactive_card(chat_id, snapshot["summary_card"])
            try:
                _remember_scheduled_briefing_thread(
                    chat_id,
                    message_id,
                    str(snapshot["cache_key"]),
                    str(snapshot.get("generated_at") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - sent card remains successful
                print("POINTER FAIL chat=%s err=%r" % (chat_name, exc))
            print("SENT ok chat=%s msg_id=%s" % (chat_name, message_id))
        except Exception as exc:  # noqa: BLE001
            print("SEND FAIL chat=%s err=%r" % (chat_name, exc))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("precompute", "send"),
        default=os.getenv("POWER_BRIEFING_MODE") or "precompute",
    )
    return parser.parse_args()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(go(_parse_args().mode))
