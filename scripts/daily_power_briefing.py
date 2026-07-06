# -*- coding: utf-8 -*-
"""每日《电力气象晨报》: 扫描重点电力省份的明日气象, 异常驱动播报到飞书群。cron 09:00 调用。"""
import asyncio
import os
from datetime import date, timedelta
from services.weather_bot import main as m
from services.weather_bot.cards import _weather_emoji, _fmt_metric
from services.weather_bot.typhoon import TyphoonClient, format_active_for_briefing

CHAT_TARGETS = [
    ("国峰运营-AI 实验群", "oc_8a6645e28915e2eefe7768e41773ec08"),
    ("小可爱电力社区 Power Pals", "oc_fe8abbef9959e5439c4797c237ad5df8"),
]
PROVINCES = [
    ("山东", "济南"), ("山西", "太原"), ("甘肃", "兰州"), ("蒙西", "呼和浩特"),
    ("广东", "广州"), ("广东", "深圳"),
    ("浙江", "杭州"), ("江苏", "南京"), ("安徽", "合肥"),
    ("四川", "成都"), ("云南", "昆明"),
]
_SEM = asyncio.Semaphore(3)


async def _fetch(service, province, city, target):
    async with _SEM:
        try:
            req = m.ForecastRequest(region=city, target_date=target, days=1, granularity="1h")
            collected, _err = await m.collect_forecasts_with_errors(service, req)
            if collected:
                return {"province": province, "city": city, "summary": collected[0].aggregated_forecast.summary}
        except Exception as exc:  # noqa: BLE001
            print("FETCH FAIL %s/%s %r" % (province, city, exc))
    return {"province": province, "city": city, "summary": None}


def _focus_signal(row):
    s = row["summary"]
    if s is None:
        return None
    temp = s.max_temperature or 0
    wind = s.wind_speed or 0
    rain = s.rain_probability or 0
    cloud = s.cloud_cover or 0
    p = row["province"]
    if rain >= 70 and wind >= 10:
        return (4, p, f"⛈️ {p} 强对流（降水{rain:.0f}%+风{wind:.0f}m/s），电网风险，光伏受限")
    if temp >= 35:
        return (3, p, f"🔥 {p} 高温 {temp:.0f}℃，制冷负荷高企，晚峰供需偏紧")
    if wind >= 10:
        return (2, p, f"💨 {p} 风速 {wind:.0f}m/s，风电出力偏强，现货或承压")
    if rain >= 60 or cloud >= 85:
        return (1, p, f"🌧️ {p} 阴雨，光伏出力受限，午间电价有支撑")
    return None


def _row_line(row):
    s = row["summary"]
    label = f"**{row['province']}**·{row['city']}"
    if s is None:
        return f"{label}　数据暂缺"
    emoji = _weather_emoji(getattr(s, "main_weather", None))
    temp = f"{_fmt_metric(s.max_temperature)}/{_fmt_metric(s.min_temperature)}°"
    wind = s.wind_speed or 0
    rain = s.rain_probability or 0
    sig = []
    if (s.max_temperature or 0) >= 35:
        sig.append("负荷↑")
    if wind >= 10:
        sig.append("风电↑")
    elif wind <= 3:
        sig.append("风电↓")
    if rain >= 60 or (s.cloud_cover or 0) >= 85:
        sig.append("光伏↓")
    return f"{label}　{emoji} {temp}　💨{wind:.0f}　💧{rain:.0f}%　｜ {'、'.join(sig) or '平稳'}"


def build_briefing_card(rows, target, typhoon_block=None):
    signals = [x for x in (_focus_signal(r) for r in rows) if x]
    signals.sort(key=lambda x: -x[0])
    seen, focus_lines = set(), []
    for sev, prov, line in signals:
        if prov in seen:
            continue
        seen.add(prov)
        focus_lines.append(line)
    top_sev = signals[0][0] if signals else 0
    template = {4: "red", 3: "orange", 2: "blue", 1: "grey"}.get(top_sev, "wathet")
    # 有活跃台风时抬升表头警示色(至少 orange), 台风是全社区级信号
    if typhoon_block and template in ("grey", "wathet", "blue"):
        template = "orange"
    if not focus_lines:
        focus_lines = ["😌 各主要市场气象面平稳，供需扰动有限"]
    md = target[5:].replace("-", "/")
    elements = []
    if typhoon_block:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": typhoon_block}})
        elements.append({"tag": "hr"})
    elements += [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**🎯 今日焦点**\n" + "\n".join(focus_lines[:4])}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**📍 分省速览（明日）**\n" + "\n".join(_row_line(r) for r in rows)}},
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "气象侧推演 · 仅供参考，不构成投资建议 · 数据 Open-Meteo / 和风加权 · 单城详情 @云云 查询"}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": f"⚡ 电力气象晨报 · {md}（明日）"},
            },
            "elements": elements,
        },
    }


async def go() -> None:
    settings = m.Settings()
    service = m.ForecastService(settings=settings)
    target = (date.today() + timedelta(days=1)).isoformat()
    rows = list(await asyncio.gather(*[_fetch(service, p, c, target) for p, c in PROVINCES]))
    ok = sum(1 for r in rows if r["summary"] is not None)
    print("PROVINCES ok=%d/%d target=%s" % (ok, len(rows), target))
    if ok == 0:
        print("ERR 全部省份无数据, 放弃发送")
        return
    typhoon_block = None
    try:
        tc = TyphoonClient(settings.qweather_api_key, settings.qweather_api_host)
        active = await tc.active_storms()
        typhoon_block = format_active_for_briefing(active)
        print("ACTIVE typhoons=%d" % len(active))
    except Exception as exc:  # noqa: BLE001 - 台风段失败不影响晨报主体
        print("TYPHOON FETCH FAIL %r" % exc)
    card = build_briefing_card(rows, target, typhoon_block)
    if os.getenv("DRY_RUN") == "1":
        print("DRY-RUN(不发送) 头部:", card["card"]["header"]["template"], card["card"]["header"]["title"]["content"])
        for element in card["card"]["elements"]:
            if element.get("tag") == "div":
                print("---")
                print(element["text"]["content"])
        return
    legacy = m._legacy_feishu_account(settings, None)
    acct = m._role_feishu_account(settings, m.FEISHU_WEATHER_BOT, legacy)
    client = m.FeishuClient(settings, acct)
    for chat_name, chat_id in CHAT_TARGETS:
        try:
            msg_id = await client.send_interactive_card(chat_id, card)
            print("SENT ok chat=%s msg_id=%s" % (chat_name, msg_id))
        except Exception as exc:  # noqa: BLE001
            print("SEND FAIL chat=%s err=%r" % (chat_name, exc))


if __name__ == "__main__":
    asyncio.run(go())
