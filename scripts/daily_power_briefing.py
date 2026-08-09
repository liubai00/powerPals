# -*- coding: utf-8 -*-
"""电力气象晨报定时入口：预计算与固定群发送显式分离。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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
from services.weather_bot.send_policy import AdminSendPolicy


@dataclass(frozen=True)
class ApprovedBriefingTarget:
    name: str
    chat_id: str


def _approved_chat_targets(settings: Any) -> list[ApprovedBriefingTarget]:
    """Load reviewed targets from configuration; malformed input fails closed."""
    raw = str(getattr(settings, "power_briefing_targets_json", "") or "").strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []

    targets: list[ApprovedBriefingTarget] = []
    seen_chat_ids: set[str] = set()
    for index, item in enumerate(decoded, start=1):
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("chat_id") or "").strip()
        if not chat_id or chat_id in seen_chat_ids:
            continue
        name = str(item.get("name") or f"approved-target-{index}").strip()
        targets.append(ApprovedBriefingTarget(name=name, chat_id=chat_id))
        seen_chat_ids.add(chat_id)
    return targets

__all__ = [
    "ApprovedBriefingTarget",
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
    weather_memory.save_briefing_context(
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
    source_registry = m.SourceRegistry.from_json(
        settings.weather_source_policies_json,
        environment=settings.app_env,
    )
    service = m.ForecastService(settings=settings)
    qweather_host = (settings.qweather_api_host or "devapi.qweather.com").strip().rstrip("/")
    typhoon_policy = source_registry.resolve(
        m.QWEATHER_TYPHOON_PROVIDER,
        f"https://{qweather_host}/v7/tropical/storm-list",
    )
    typhoon_client = m.TyphoonClient(
        settings.qweather_api_key,
        qweather_host,
        source_registry=source_registry,
        source_policy=typhoon_policy,
    )
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
        release_slot="09:00",
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
    targets = _approved_chat_targets(settings)
    send_policy = AdminSendPolicy(
        global_send_enabled=settings.global_feishu_send_enabled,
        dry_run=settings.dry_run or dry_run,
    )
    schedule_send_enabled = bool(settings.power_briefing_allow_send)
    initial_decision = send_policy.scheduled_card_send_decision(
        targets[0].chat_id if targets else None,
        schedule_send_enabled=schedule_send_enabled,
    )
    if not initial_decision.allowed:
        print("NO-SEND mode=send reason=%s" % initial_decision.reason)
        return

    legacy = m._legacy_feishu_account(settings, None)
    account = m._role_feishu_account(settings, m.FEISHU_WEATHER_BOT, legacy)
    feishu = m.FeishuClient(settings, account)
    report_date = str(snapshot.get("report_date") or start_date)
    declared_slot = str(snapshot.get("release_slot") or "09:00")
    release_slot = f"{report_date}|{declared_slot}"
    for target in targets:
        decision = send_policy.scheduled_card_send_decision(
            target.chat_id,
            schedule_send_enabled=schedule_send_enabled,
        )
        if not decision.allowed:
            print("NO-SEND target=%s reason=%s" % (target.name, decision.reason))
            continue
        owner_token = uuid4().hex
        send_uuid = str(
            uuid5(
                NAMESPACE_URL,
                f"power-briefing:{release_slot}:{target.chat_id}",
            )
        )
        if not cache.claim_scheduled_delivery(
            release_slot,
            target.chat_id,
            owner_token,
            send_uuid,
        ):
            print("NO-SEND target=%s reason=already_sent_or_in_progress" % target.name)
            continue
        try:
            message_id = await feishu.send_interactive_card(
                target.chat_id,
                snapshot["summary_card"],
                idempotency_key=send_uuid,
            )
            cache.complete_scheduled_delivery(
                release_slot,
                target.chat_id,
                owner_token,
                message_id,
            )
            try:
                _remember_scheduled_briefing_thread(
                    target.chat_id,
                    message_id,
                    str(snapshot["cache_key"]),
                    str(snapshot.get("generated_at") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - sent card remains successful
                print("POINTER FAIL target=%s err=%s" % (target.name, type(exc).__name__))
            print("SENT ok target=%s msg_id=%s" % (target.name, message_id))
        except Exception as exc:  # noqa: BLE001
            try:
                cache.release_failed_scheduled_delivery(
                    release_slot,
                    target.chat_id,
                    owner_token,
                )
            except Exception as ledger_exc:  # noqa: BLE001 - preserve the send failure
                print(
                    "LEDGER RELEASE FAIL target=%s err=%s"
                    % (target.name, type(ledger_exc).__name__)
                )
            print("SEND FAIL target=%s err=%s" % (target.name, type(exc).__name__))


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
