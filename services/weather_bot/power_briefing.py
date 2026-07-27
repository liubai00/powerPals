# -*- coding: utf-8 -*-
"""电力气象决策晨报 2.0：异常市场、关键时段、变化和置信度优先。"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any, Callable

from services.weather_bot.models import ForecastRequest
from services.weather_bot.typhoon import TyphoonClient, format_active_for_briefing
from services.weather_bot.workbench import collect_forecasts_with_errors


CHAT_TARGETS = [
    ("国峰运营-AI 实验群", "oc_8a6645e28915e2eefe7768e41773ec08"),
    ("小可爱电力社区 Power Pals", "oc_fe8abbef9959e5439c4797c237ad5df8"),
]

# 当前仍是城市代表点，不宣称为省级聚合；后续可替换为市场多点及装机/负荷权重。
MARKET_POINTS = [
    ("山东", "济南"),
    ("山西", "太原"),
    ("甘肃", "兰州"),
    ("蒙西", "呼和浩特"),
    ("广东", "广州"),
    ("广东", "深圳"),
    ("浙江", "杭州"),
    ("江苏", "南京"),
    ("安徽", "合肥"),
    ("四川", "成都"),
    ("云南", "昆明"),
]
PROVINCES = MARKET_POINTS  # 保留旧脚本调用方的兼容名称。
SHANGHAI_TZ = timezone(timedelta(hours=8))
_SEM = asyncio.Semaphore(3)


@dataclass(frozen=True)
class MarketInsight:
    market: str
    city: str
    severity: int
    window: str
    directions: tuple[str, ...]
    driver: str
    change: str
    confidence: str
    cooling_degree_hours: float
    heating_degree_hours: float
    solar_stress: float
    wind_peak: float

    @property
    def label(self) -> str:
        return f"{self.market}·{self.city}代表点"


async def _fetch(service, market: str, city: str, start_date: str) -> dict[str, Any]:
    async with _SEM:
        try:
            request = ForecastRequest(region=city, target_date=start_date, days=2, granularity="1h")
            collected, errors = await collect_forecasts_with_errors(service, request)
            return {
                "market": market,
                "province": market,
                "city": city,
                "submissions": {item.target_date: item for item in collected},
                "errors": errors,
            }
        except Exception as exc:  # noqa: BLE001 - 单市场失败不应阻断整份晨报
            print("FETCH FAIL %s/%s %r" % (market, city, exc))
    return {"market": market, "province": market, "city": city, "submissions": {}, "errors": []}


def _value(point: Any, field: str) -> float | None:
    raw = getattr(point, field, None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _hour(point: Any) -> int | None:
    value = str(getattr(point, "time", ""))
    try:
        return int(value[11:13])
    except (TypeError, ValueError):
        return None


def _daylight_points(submission: Any) -> list[Any]:
    points = list(submission.aggregated_forecast.points)
    summary = submission.aggregated_forecast.summary
    try:
        sunrise = int(str(summary.sunrise or "08:00")[:2])
        sunset = int(str(summary.sunset or "18:00")[:2])
    except ValueError:
        sunrise, sunset = 8, 18
    selected = [point for point in points if (hour := _hour(point)) is not None and sunrise <= hour < sunset]
    return selected or [point for point in points if (hour := _hour(point)) is not None and 8 <= hour < 18]


def _continuous_windows(points: list[Any], predicate: Callable[[Any], bool]) -> str:
    hours = sorted({hour for point in points if predicate(point) and (hour := _hour(point)) is not None})
    if not hours:
        return "无明显异常时段"
    groups: list[list[int]] = [[hours[0]]]
    for hour in hours[1:]:
        if hour == groups[-1][-1] + 1:
            groups[-1].append(hour)
        else:
            groups.append([hour])
    windows = []
    for group in groups[:2]:
        start = group[0]
        end = min(24, group[-1] + 1)
        windows.append(f"{start:02d}:00–{end:02d}:00")
    return "、".join(windows)


def _mean_metric(points: list[Any], field: str) -> float:
    values = [_value(point, field) for point in points]
    usable = [value for value in values if value is not None]
    return mean(usable) if usable else 0.0


def _max_metric(points: list[Any], field: str) -> float:
    values = [_value(point, field) for point in points]
    usable = [value for value in values if value is not None]
    return max(usable) if usable else 0.0


def _min_metric(points: list[Any], field: str) -> float:
    values = [_value(point, field) for point in points]
    usable = [value for value in values if value is not None]
    return min(usable) if usable else 0.0


def _day_metrics(submission: Any) -> dict[str, Any]:
    points = list(submission.aggregated_forecast.points)
    daylight = _daylight_points(submission)
    effective_temperatures = [
        _value(point, "apparent_temperature")
        if _value(point, "apparent_temperature") is not None
        else _value(point, "temperature")
        for point in points
    ]
    temperatures = [value for value in effective_temperatures if value is not None]
    cooling = sum(max(value - 26.0, 0.0) for value in temperatures)
    heating = sum(max(18.0 - value, 0.0) for value in temperatures)
    daylight_cloud = _mean_metric(daylight, "cloud_cover")
    daylight_rain = _max_metric(daylight, "precipitation_probability")
    solar_stress = min(100.0, daylight_cloud * 0.75 + daylight_rain * 0.25)
    wind_peak = _max_metric(points, "wind_speed")
    wind_mean = _mean_metric(points, "wind_speed")
    max_feels = max(temperatures) if temperatures else 0.0
    min_feels = min(temperatures) if temperatures else 0.0

    def is_attention_hour(point: Any) -> bool:
        temperature = _value(point, "apparent_temperature")
        if temperature is None:
            temperature = _value(point, "temperature")
        rain = _value(point, "precipitation_probability") or 0.0
        wind = _value(point, "wind_speed") or 0.0
        cloud = _value(point, "cloud_cover") or 0.0
        hour = _hour(point)
        daylight_hour = hour is not None and 8 <= hour < 18
        return (
            (temperature is not None and (temperature >= 35 or temperature <= 0))
            or wind >= 10
            or (daylight_hour and (rain >= 60 or cloud >= 85))
        )

    return {
        "cooling": cooling,
        "heating": heating,
        "solar_stress": solar_stress,
        "daylight_cloud": daylight_cloud,
        "daylight_rain": daylight_rain,
        "wind_peak": wind_peak,
        "wind_mean": wind_mean,
        "max_feels": max_feels,
        "min_feels": min_feels,
        "window": _continuous_windows(points, is_attention_hour),
        "same_hour_rain_wind": any(
            (_value(point, "precipitation_probability") or 0.0) >= 70
            and (_value(point, "wind_speed") or 0.0) >= 10
            for point in points
        ),
    }


def _confidence_label(submission: Any) -> str:
    usable = [
        result
        for result in submission.provider_results
        if result.status == "ok" and result.points
    ]
    if len(usable) <= 1:
        return "偏低（单一可用源）"

    grouped: dict[tuple[str, str], list[float]] = {}
    for result in usable:
        for point in result.points:
            hour_key = str(point.time)[:13]
            for field in ("temperature", "wind_speed", "cloud_cover"):
                value = _value(point, field)
                if value is not None:
                    grouped.setdefault((hour_key, field), []).append(value)
    spreads = {
        "temperature": [],
        "wind_speed": [],
        "cloud_cover": [],
    }
    for (_hour_key, field), values in grouped.items():
        if len(values) >= 2:
            spreads[field].append(max(values) - min(values))
    temp_spread = mean(spreads["temperature"]) if spreads["temperature"] else 0.0
    wind_spread = mean(spreads["wind_speed"]) if spreads["wind_speed"] else 0.0
    cloud_spread = mean(spreads["cloud_cover"]) if spreads["cloud_cover"] else 0.0
    if temp_spread > 4 or wind_spread > 4 or cloud_spread > 30:
        return "偏低（数据源分歧较大）"
    if len(usable) >= 3 and temp_spread <= 2 and wind_spread <= 2 and cloud_spread <= 15:
        return "较高"
    return "中等"


def _change_text(today: dict[str, Any] | None, tomorrow: dict[str, Any]) -> str:
    if not today:
        return "缺少今日基线"
    changes = []
    load_today = max(today["cooling"], today["heating"])
    load_tomorrow = max(tomorrow["cooling"], tomorrow["heating"])
    if load_tomorrow - load_today >= 12:
        changes.append("负荷天气压力上调")
    elif load_today - load_tomorrow >= 12:
        changes.append("负荷天气压力下调")
    if tomorrow["solar_stress"] - today["solar_stress"] >= 12:
        changes.append("光资源代理转弱")
    elif today["solar_stress"] - tomorrow["solar_stress"] >= 12:
        changes.append("光资源代理改善")
    if tomorrow["wind_mean"] - today["wind_mean"] >= 2:
        changes.append("地面风资源增强")
    elif today["wind_mean"] - tomorrow["wind_mean"] >= 2:
        changes.append("地面风资源减弱")
    return "、".join(changes) if changes else "较今日变化不大"


def _analyze_row(row: dict[str, Any], start_date: str) -> MarketInsight | None:
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    submissions = row.get("submissions") or {}
    tomorrow_submission = submissions.get(tomorrow_date)
    if tomorrow_submission is None:
        return None
    today_submission = submissions.get(start_date)
    tomorrow = _day_metrics(tomorrow_submission)
    today = _day_metrics(today_submission) if today_submission is not None else None

    severity = 0
    directions: list[str] = []
    drivers: list[str] = []
    if tomorrow["same_hour_rain_wind"]:
        severity = max(severity, 4)
        drivers.append("同一时段降水概率与地面风同时偏高")
    if tomorrow["cooling"] >= 24 or tomorrow["max_feels"] >= 35:
        severity = max(severity, 3)
        directions.append("负荷压力↑")
        drivers.append(f"制冷度时 {tomorrow['cooling']:.0f}")
    elif tomorrow["heating"] >= 36 or tomorrow["min_feels"] <= 0:
        severity = max(severity, 3)
        directions.append("负荷压力↑")
        drivers.append(f"采暖度时 {tomorrow['heating']:.0f}")
    if tomorrow["daylight_rain"] >= 60 or tomorrow["daylight_cloud"] >= 75:
        severity = max(severity, 2)
        directions.append("光伏资源代理↓")
        drivers.append(
            f"日照时段云量 {tomorrow['daylight_cloud']:.0f}% / 降水概率 {tomorrow['daylight_rain']:.0f}%"
        )
    elif tomorrow["daylight_cloud"] <= 35 and tomorrow["daylight_rain"] < 30:
        directions.append("光伏资源代理↑")
    if tomorrow["wind_peak"] >= 10:
        severity = max(severity, 2)
        directions.append("地面风资源代理↑")
        drivers.append(f"10米风峰值 {tomorrow['wind_peak']:.1f}m/s")
    elif tomorrow["wind_peak"] <= 3:
        severity = max(severity, 1)
        directions.append("地面风资源代理↓")
        drivers.append(f"10米风峰值仅 {tomorrow['wind_peak']:.1f}m/s")

    return MarketInsight(
        market=row.get("market") or row.get("province") or "未知市场",
        city=row.get("city") or "未知城市",
        severity=severity,
        window=tomorrow["window"],
        directions=tuple(dict.fromkeys(directions)) or ("气象侧无明显异常",),
        driver="；".join(dict.fromkeys(drivers)) or "主要指标处于常用阈值内",
        change=_change_text(today, tomorrow),
        confidence=_confidence_label(tomorrow_submission),
        cooling_degree_hours=tomorrow["cooling"],
        heating_degree_hours=tomorrow["heating"],
        solar_stress=tomorrow["solar_stress"],
        wind_peak=tomorrow["wind_peak"],
    )


def _insight_line(insight: MarketInsight) -> str:
    direction = " / ".join(insight.directions)
    return (
        f"- **{insight.label}｜{insight.window}**　{direction}；"
        f"驱动：{insight.driver}；变化：{insight.change}；置信度：{insight.confidence}"
    )


def _ranking_lines(insights: list[MarketInsight]) -> list[str]:
    load = sorted(insights, key=lambda item: max(item.cooling_degree_hours, item.heating_degree_hours), reverse=True)[:3]
    solar = sorted(insights, key=lambda item: item.solar_stress, reverse=True)[:3]
    wind = sorted(insights, key=lambda item: item.wind_peak, reverse=True)[:3]
    return [
        "负荷天气压力：" + "、".join(item.label for item in load),
        "光资源转弱代理：" + "、".join(item.label for item in solar),
        "地面风资源代理：" + "、".join(item.label for item in wind),
    ]


def _source_summary(rows: list[dict[str, Any]]) -> str:
    sources = {
        provider
        for row in rows
        for submission in (row.get("submissions") or {}).values()
        for provider in submission.aggregated_forecast.providers_used
    }
    return " / ".join(sorted(sources)) if sources else "暂无可用源"


def build_briefing_card(
    rows: list[dict[str, Any]],
    start_date: str,
    typhoon_block: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(SHANGHAI_TZ)
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    insights = [item for row in rows if (item := _analyze_row(row, start_date)) is not None]
    risks = sorted((item for item in insights if item.severity > 0), key=lambda item: -item.severity)
    top_risks = risks[:5]
    missing = [
        f"{row.get('market') or row.get('province')}·{row.get('city')}"
        for row in rows
        if not (row.get("submissions") or {}).get(tomorrow_date)
    ]
    stable_count = sum(1 for item in insights if item.severity == 0)
    top_severity = top_risks[0].severity if top_risks else 0
    template = {4: "red", 3: "orange", 2: "blue", 1: "grey"}.get(top_severity, "wathet")
    if typhoon_block and template in {"grey", "wathet", "blue"}:
        template = "orange"

    if top_risks:
        conclusion = "；".join(
            f"明日{item.label}{item.window}{' / '.join(item.directions[:2])}" for item in top_risks[:3]
        )
    else:
        conclusion = "今日至明日未发现达到阈值的异常市场，仍需结合负荷、出力和机组信息复核。"
    risk_lines = [_insight_line(item) for item in top_risks]
    if not risk_lines:
        risk_lines = ["- 暂无达到展示阈值的异常；稳定市场已折叠。"]

    coverage = f"{len(insights)}/{len(rows)}"
    health = (
        f"生成 {generated.strftime('%m/%d %H:%M')}　·　覆盖 {coverage}　·　"
        f"来源 {_source_summary(rows)}　·　范围 今日+明日"
    )
    collapsed = f"稳定市场 {stable_count} 个，已折叠"
    if missing:
        collapsed += f"；数据暂缺 {len(missing)} 个：{'、'.join(missing)}"

    elements: list[dict[str, Any]] = [
        {"tag": "note", "elements": [{"tag": "plain_text", "content": health}]},
    ]
    if typhoon_block:
        elements.extend(
            [
                {"tag": "div", "text": {"tag": "lark_md", "content": typhoon_block}},
                {"tag": "hr"},
            ]
        )
    elements.extend(
        [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**一句话结论**\n{conclusion}"}},
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**Top 5 气象侧风险**\n" + "\n".join(risk_lines)},
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**资源代理排行**\n" + "\n".join(_ranking_lines(insights))},
            },
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**其余市场**\n{collapsed}"}},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            "当前为城市代表点；云量/降水仅作光伏资源代理，10米风仅作地面风资源代理。"
                            "未接入负荷、出力、机组、联络线及价格数据，不构成交易或报价建议。"
                        ),
                    }
                ],
            },
        ]
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {
                    "tag": "plain_text",
                    "content": f"⚡ 电力气象决策晨报 2.0｜{start_date[5:].replace('-', '/')}–{tomorrow_date[5:].replace('-', '/')}",
                },
            },
            "elements": elements,
        },
    }


async def go() -> None:
    # 定时发送入口才依赖应用组装层；卡片分析与手动生成保持可独立复用。
    from services.weather_bot import main as m

    settings = m.Settings()
    service = m.ForecastService(settings=settings)
    start_date = date.today().isoformat()
    rows = list(await asyncio.gather(*[_fetch(service, market, city, start_date) for market, city in MARKET_POINTS]))
    tomorrow_date = (date.today() + timedelta(days=1)).isoformat()
    ok = sum(1 for row in rows if (row.get("submissions") or {}).get(tomorrow_date) is not None)
    print("MARKETS ok=%d/%d range=%s..%s" % (ok, len(rows), start_date, tomorrow_date))
    if ok == 0:
        print("ERR 全部市场无明日数据，放弃发送")
        return

    typhoon_block = None
    try:
        client = TyphoonClient(settings.qweather_api_key, settings.qweather_api_host)
        active = await client.active_storms()
        typhoon_block = format_active_for_briefing(active)
        print("ACTIVE typhoons=%d" % len(active))
    except Exception as exc:  # noqa: BLE001 - 台风段失败不影响晨报主体
        print("TYPHOON FETCH FAIL %r" % exc)

    card = build_briefing_card(rows, start_date, typhoon_block=typhoon_block)
    if os.getenv("DRY_RUN") == "1":
        print("DRY-RUN(不发送) 头部:", card["card"]["header"]["template"], card["card"]["header"]["title"]["content"])
        for element in card["card"]["elements"]:
            if element.get("tag") == "div":
                print("---")
                print(element["text"]["content"])
        return

    legacy = m._legacy_feishu_account(settings, None)
    account = m._role_feishu_account(settings, m.FEISHU_WEATHER_BOT, legacy)
    feishu = m.FeishuClient(settings, account)
    for chat_name, chat_id in CHAT_TARGETS:
        try:
            message_id = await feishu.send_interactive_card(chat_id, card)
            print("SENT ok chat=%s msg_id=%s" % (chat_name, message_id))
        except Exception as exc:  # noqa: BLE001
            print("SEND FAIL chat=%s err=%r" % (chat_name, exc))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(go())
