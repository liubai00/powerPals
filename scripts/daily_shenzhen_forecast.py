# -*- coding: utf-8 -*-
"""每日深圳未来三天气象卡片 -> 发到指定飞书群(多群)。由 cron 调用。"""
import asyncio
import urllib.parse
from datetime import date
from services.weather_bot import main as m

CHAT_TARGETS = [
    ("国峰运营-AI 实验群", "oc_8a6645e28915e2eefe7768e41773ec08"),
    ("小可爱电力社区 Power Pals", "oc_fe8abbef9959e5439c4797c237ad5df8"),
]
PUBLIC_BASE = "http://38.76.196.233:8001"


async def go() -> None:
    settings = m.Settings()
    service = m.ForecastService(settings=settings)
    today = date.today().isoformat()
    req = m.ForecastRequest(region="深圳", target_date=today, days=3, granularity="1h")
    collected, errors = await m.collect_forecasts_with_errors(service, req)
    if not collected:
        print("ERR 无预测结果:", errors)
        return
    region = collected[0].region
    report_url = "%s/reports/weather?region=%s&target_date=%s&days=3" % (
        PUBLIC_BASE, urllib.parse.quote(region), today,
    )
    card = m.build_feishu_card(collected[0], report_url=report_url, chart_submissions=collected, show_task_id=False)
    legacy = m._legacy_feishu_account(settings, None)
    acct = m._role_feishu_account(settings, m.FEISHU_WEATHER_BOT, legacy)
    client = m.FeishuClient(settings, acct)
    for chat_name, chat_id in CHAT_TARGETS:
        try:
            msg_id = await client.send_interactive_card(chat_id, card)
            print("SENT ok chat=%s msg_id=%s region=%s days=%d errors=%s" % (chat_name, msg_id, region, len(collected), errors))
        except Exception as exc:  # noqa: BLE001 - 单群失败不影响其它群
            print("SEND FAIL chat=%s err=%r" % (chat_name, exc))


if __name__ == "__main__":
    asyncio.run(go())
