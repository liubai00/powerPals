# -*- coding: utf-8 -*-
"""电力气象决策晨报 3.0：版本、来源、质量和代理边界优先。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from statistics import mean
from typing import Any, Callable
from uuid import uuid4

from services.weather_bot.briefing_cache import BriefingCache
from services.weather_bot.briefing_versions import (
    build_run_provenance,
    compare_market_risk_versions,
)
from services.weather_bot.models import ForecastRequest
from services.weather_bot.power_briefing_markets import (
    MARKET_CONFIG_VERSION,
    NATIONAL_MARKETS,
    MarketZone,
    RepresentativePoint,
    representative_points,
)
from services.weather_bot.typhoon import TyphoonClient, format_active_for_briefing
from services.weather_bot.workbench import collect_forecasts_with_errors


POWER_BRIEFING_REPORT_VERSION = "power-briefing-3.0"
POWER_WEATHER_PROXY_VERSION = "power-weather-proxy-v1"
MARKET_RISK_WEIGHT_VERSION = "market-risk-weight-v1"
MARKET_POINTS = representative_points()
PROVINCES = NATIONAL_MARKETS  # 保留旧脚本导入名；实际为 33 个电力气象分析区。
SHANGHAI_TZ = timezone(timedelta(hours=8))
_SEM = asyncio.Semaphore(8)


@dataclass(frozen=True)
class SignalEvent:
    signal_type: str
    direction: str
    severity: int
    window: str
    driver: str
    change: str


@dataclass(frozen=True)
class PointInsight:
    market_id: str
    market: str
    province: str
    point_id: str
    city: str
    roles: tuple[str, ...]
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
    signal_events: tuple[SignalEvent, ...]

    @property
    def label(self) -> str:
        return f"{self.market}·{self.city}代表点"


@dataclass(frozen=True)
class MarketInsight:
    market_id: str
    market: str
    province: str
    city: str
    configured_points: int
    covered_points: int
    severity: int
    window: str
    directions: tuple[str, ...]
    driver: str
    change: str
    confidence: str
    cooling_degree_hours: float | None
    heating_degree_hours: float | None
    solar_stress: float | None
    wind_peak: float | None
    point_insights: tuple[PointInsight, ...]
    risk_events: tuple[PointInsight, ...]
    risk_signal_count: int

    @property
    def label(self) -> str:
        if self.configured_points > 1:
            if self.covered_points == 1:
                return (
                    f"{self.market}·{self.city}代表点"
                    f"（1/{self.configured_points}点，不可外推全区）"
                )
            coverage = "完整样本" if self.covered_points == self.configured_points else "部分样本综合"
            return f"{self.market}（{self.covered_points}/{self.configured_points}点，{coverage}）"
        return f"{self.market}·{self.city}代表点"

    @property
    def risk_label(self) -> str:
        if self.configured_points <= 1:
            return self.label
        if self.covered_points == 1:
            coverage = "不可外推全区"
        elif self.covered_points == self.configured_points:
            coverage = "完整样本"
        else:
            coverage = "部分样本综合"
        return (
            f"{self.market}·{self.city}代表点"
            f"（{self.covered_points}/{self.configured_points}点，{coverage}）"
        )


async def _fetch(
    service: Any,
    market: MarketZone,
    point: RepresentativePoint,
    start_date: str,
) -> dict[str, Any]:
    async with _SEM:
        try:
            request = ForecastRequest(region=point.query, target_date=start_date, days=2, granularity="1h")
            collected, errors = await collect_forecasts_with_errors(service, request)
            return {
                "market_id": market.market_id,
                "market": market.market_name,
                "province": market.provincial_area,
                "point_id": point.point_id,
                "city": point.city,
                "roles": list(point.roles),
                "submissions": {item.target_date: item for item in collected},
                "errors": errors,
            }
        except Exception as exc:  # noqa: BLE001 - 单市场失败不应阻断整份晨报
            print(
                "FETCH FAIL %s/%s error_type=%s"
                % (market.market_name, point.city, type(exc).__name__)
            )
    return {
        "market_id": market.market_id,
        "market": market.market_name,
        "province": market.provincial_area,
        "point_id": point.point_id,
        "city": point.city,
        "roles": list(point.roles),
        "submissions": {},
        "errors": [],
    }


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
    daylight_radiation = [
        value
        for point in daylight
        if (value := _value(point, "shortwave_radiation")) is not None
    ]
    radiation_integral = sum(daylight_radiation) if daylight_radiation else None
    if radiation_integral is not None:
        # Hourly Open-Meteo values are W/m². Summing one-hour samples yields a
        # transparent Wh/m² weather-resource proxy; it is never plant output.
        solar_stress = min(100.0, max(0.0, (3000.0 - radiation_integral) / 30.0))
        solar_proxy_method = "shortwave_radiation"
        solar_proxy_quality_reason = None
    else:
        solar_stress = min(100.0, daylight_cloud * 0.75 + daylight_rain * 0.25)
        solar_proxy_method = "cloud_rain_fallback"
        solar_proxy_quality_reason = "短波辐射缺失，降级为云量+降水代理"
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
        "solar_radiation_integral": radiation_integral,
        "solar_proxy_method": solar_proxy_method,
        "solar_proxy_quality_reason": solar_proxy_quality_reason,
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


def _change_for_signal(
    today: dict[str, Any] | None,
    tomorrow: dict[str, Any],
    signal_type: str,
) -> str:
    if not today:
        return "缺少今日基线"
    if signal_type == "load":
        load_today = max(today["cooling"], today["heating"])
        load_tomorrow = max(tomorrow["cooling"], tomorrow["heating"])
        if load_tomorrow - load_today >= 12:
            return "负荷天气压力代理上调"
        if load_today - load_tomorrow >= 12:
            return "负荷天气压力代理下调"
    elif signal_type == "solar":
        if today["solar_proxy_method"] != tomorrow["solar_proxy_method"]:
            return "代理口径变化，不作同比"
        if tomorrow["solar_stress"] - today["solar_stress"] >= 12:
            return "光资源代理转弱"
        if today["solar_stress"] - tomorrow["solar_stress"] >= 12:
            return "光资源代理改善"
    elif signal_type == "wind":
        if tomorrow["wind_mean"] - today["wind_mean"] >= 2:
            return "地面风资源增强"
        if today["wind_mean"] - tomorrow["wind_mean"] >= 2:
            return "地面风资源减弱"
    elif signal_type == "local_compound":
        if tomorrow["same_hour_rain_wind"] and not today["same_hour_rain_wind"]:
            return "较今日新增风雨复合时段"
        if today["same_hour_rain_wind"] and not tomorrow["same_hour_rain_wind"]:
            return "较今日风雨复合风险缓解"
    return "较今日变化不大"


def _analyze_row(row: dict[str, Any], start_date: str) -> PointInsight | None:
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    submissions = row.get("submissions") or {}
    tomorrow_submission = submissions.get(tomorrow_date)
    if tomorrow_submission is None:
        return None
    today_submission = submissions.get(start_date)
    tomorrow = _day_metrics(tomorrow_submission)
    today = _day_metrics(today_submission) if today_submission is not None else None
    roles = tuple(row.get("roles") or ("load", "solar", "wind"))
    points = list(tomorrow_submission.aggregated_forecast.points)
    daylight = _daylight_points(tomorrow_submission)
    signal_events: list[SignalEvent] = []

    def effective_temperature(point: Any) -> float | None:
        value = _value(point, "apparent_temperature")
        return value if value is not None else _value(point, "temperature")

    if tomorrow["same_hour_rain_wind"]:
        signal_events.append(
            SignalEvent(
                signal_type="local_compound",
                direction="局地风雨复合风险",
                severity=4,
                window=_continuous_windows(
                    points,
                    lambda point: (
                        (_value(point, "precipitation_probability") or 0.0) >= 70
                        and (_value(point, "wind_speed") or 0.0) >= 10
                    ),
                ),
                driver="同一时段降水概率与地面风同时偏高",
                change=_change_for_signal(today, tomorrow, "local_compound"),
            )
        )
    if "load" in roles and (tomorrow["cooling"] >= 24 or tomorrow["max_feels"] >= 35):
        signal_events.append(
            SignalEvent(
                signal_type="load",
                direction="负荷天气压力代理↑",
                severity=3,
                window=_continuous_windows(
                    points,
                    lambda point: (
                        (temperature := effective_temperature(point)) is not None
                        and temperature > 26
                    ),
                ),
                driver=f"制冷度时 {tomorrow['cooling']:.0f}",
                change=_change_for_signal(today, tomorrow, "load"),
            )
        )
    elif "load" in roles and (tomorrow["heating"] >= 36 or tomorrow["min_feels"] <= 0):
        signal_events.append(
            SignalEvent(
                signal_type="load",
                direction="负荷天气压力代理↑",
                severity=3,
                window=_continuous_windows(
                    points,
                    lambda point: (
                        (temperature := effective_temperature(point)) is not None
                        and temperature < 18
                    ),
                ),
                driver=f"采暖度时 {tomorrow['heating']:.0f}",
                change=_change_for_signal(today, tomorrow, "load"),
            )
        )
    if (
        "solar" in roles
        and tomorrow["solar_proxy_method"] == "shortwave_radiation"
        and tomorrow["solar_radiation_integral"] < 2000
    ):
        signal_events.append(
            SignalEvent(
                signal_type="solar",
                direction="光伏资源代理↓",
                severity=2,
                window=_continuous_windows(
                    daylight,
                    lambda point: (
                        (value := _value(point, "shortwave_radiation")) is not None
                        and value < 150
                    ),
                ),
                driver=(
                    f"日照时段短波辐射积分 {tomorrow['solar_radiation_integral']:.0f} Wh/m²"
                    "（气象辐射代理，非实际光伏出力）"
                ),
                change=_change_for_signal(today, tomorrow, "solar"),
            )
        )
    elif (
        "solar" in roles
        and tomorrow["solar_proxy_method"] == "shortwave_radiation"
        and tomorrow["solar_radiation_integral"] >= 4000
    ):
        signal_events.append(
            SignalEvent(
                signal_type="solar",
                direction="光伏资源代理↑",
                severity=0,
                window=_continuous_windows(
                    daylight,
                    lambda point: (
                        (value := _value(point, "shortwave_radiation")) is not None
                        and value >= 300
                    ),
                ),
                driver=(
                    f"日照时段短波辐射积分 {tomorrow['solar_radiation_integral']:.0f} Wh/m²"
                    "（气象辐射代理，非实际光伏出力）"
                ),
                change=_change_for_signal(today, tomorrow, "solar"),
            )
        )
    elif "solar" in roles and tomorrow["solar_proxy_method"] == "cloud_rain_fallback" and (
        tomorrow["daylight_rain"] >= 60 or tomorrow["daylight_cloud"] >= 75
    ):
        signal_events.append(
            SignalEvent(
                signal_type="solar",
                direction="光伏资源代理↓",
                severity=2,
                window=_continuous_windows(
                    daylight,
                    lambda point: (
                        (_value(point, "precipitation_probability") or 0.0) >= 60
                        or (_value(point, "cloud_cover") or 0.0) >= 75
                    ),
                ),
                driver=(
                    f"日照时段云量 {tomorrow['daylight_cloud']:.0f}% / "
                    f"降水概率 {tomorrow['daylight_rain']:.0f}%"
                    "（短波辐射缺失，降级为云量+降水代理，非实际光伏出力）"
                ),
                change=_change_for_signal(today, tomorrow, "solar"),
            )
        )
    elif (
        "solar" in roles
        and tomorrow["solar_proxy_method"] == "cloud_rain_fallback"
        and tomorrow["daylight_cloud"] <= 35
        and tomorrow["daylight_rain"] < 30
    ):
        signal_events.append(
            SignalEvent(
                signal_type="solar",
                direction="光伏资源代理↑",
                severity=0,
                window=_continuous_windows(
                    daylight,
                    lambda point: (
                        (_value(point, "precipitation_probability") or 0.0) < 30
                        and (_value(point, "cloud_cover") or 0.0) <= 35
                    ),
                ),
                driver=(
                    "日照时段云量和降水概率较低"
                    "（短波辐射缺失，降级为云量+降水代理，非实际光伏出力）"
                ),
                change=_change_for_signal(today, tomorrow, "solar"),
            )
        )
    if "wind" in roles and tomorrow["wind_peak"] >= 10:
        signal_events.append(
            SignalEvent(
                signal_type="wind",
                direction="地面风资源代理↑",
                severity=2,
                window=_continuous_windows(
                    points,
                    lambda point: (_value(point, "wind_speed") or 0.0) >= 10,
                ),
                driver=f"10米风峰值 {tomorrow['wind_peak']:.1f}m/s",
                change=_change_for_signal(today, tomorrow, "wind"),
            )
        )
    elif "wind" in roles and tomorrow["wind_peak"] <= 3:
        signal_events.append(
            SignalEvent(
                signal_type="wind",
                direction="地面风资源代理↓",
                severity=1,
                window=_continuous_windows(
                    points,
                    lambda point: (_value(point, "wind_speed") or 0.0) <= 3,
                ),
                driver=f"10米风峰值仅 {tomorrow['wind_peak']:.1f}m/s",
                change=_change_for_signal(today, tomorrow, "wind"),
            )
        )

    if not signal_events:
        signal_events.append(
            SignalEvent(
                signal_type="neutral",
                direction="气象侧无明显异常",
                severity=0,
                window="无明显异常时段",
                driver="主要指标处于常用阈值内",
                change="缺少今日基线" if today is None else "较今日变化不大",
            )
        )
    signal_events.sort(key=lambda event: (-event.severity, event.signal_type))
    primary = signal_events[0]

    return PointInsight(
        market_id=str(row.get("market_id") or row.get("market") or row.get("province") or "unknown"),
        market=row.get("market") or row.get("province") or "未知市场",
        province=row.get("province") or row.get("market") or "未知地区",
        point_id=str(row.get("point_id") or row.get("city") or "unknown"),
        city=row.get("city") or "未知城市",
        roles=roles,
        severity=primary.severity,
        window=primary.window,
        directions=(primary.direction,),
        driver=primary.driver,
        change=primary.change,
        confidence=_confidence_label(tomorrow_submission),
        cooling_degree_hours=tomorrow["cooling"],
        heating_degree_hours=tomorrow["heating"],
        solar_stress=tomorrow["solar_stress"],
        wind_peak=tomorrow["wind_peak"],
        signal_events=tuple(signal_events),
    )


def _worst_confidence(insights: list[PointInsight]) -> str:
    order = {"较高": 3, "中等": 2}
    return min(
        (item.confidence for item in insights),
        key=lambda value: order.get(value, 1),
    )


def _role_insights(insights: list[PointInsight], role: str) -> list[PointInsight]:
    return [item for item in insights if role in item.roles]


def _combined_directions(insights: list[PointInsight]) -> tuple[str, ...]:
    directions = list(
        dict.fromkeys(
            event.direction
            for item in insights
            for event in item.signal_events
        )
    )
    for upward, downward, divergent in (
        ("光伏资源代理↑", "光伏资源代理↓", "光伏资源代理区内分化"),
        ("地面风资源代理↑", "地面风资源代理↓", "地面风资源代理区内分化"),
    ):
        if upward in directions and downward in directions:
            directions = [
                direction
                for direction in directions
                if direction not in {upward, downward}
            ]
            directions.append(divergent)
    if len(directions) > 1 and "气象侧无明显异常" in directions:
        directions.remove("气象侧无明显异常")
    return tuple(directions) or ("气象侧无明显异常",)


def _aggregate_market_insights(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    market_config: tuple[MarketZone, ...] | None = None,
) -> list[MarketInsight]:
    configured_lookup = (
        {
            (market.market_id, point.point_id): (market, point)
            for market in market_config
            for point in market.points
        }
        if market_config is not None
        else None
    )
    filtered_rows: list[dict[str, Any]] = []
    seen_points: set[tuple[str, str]] = set()
    for row in rows:
        market_id = str(row.get("market_id") or row.get("market") or row.get("province") or "unknown")
        point_id = str(row.get("point_id") or row.get("city") or "unknown")
        key = (market_id, point_id)
        if key in seen_points:
            continue
        if configured_lookup is not None:
            configured = configured_lookup.get(key)
            if configured is None:
                continue
            market, point = configured
            row = {
                **row,
                "market_id": market.market_id,
                "market": market.market_name,
                "province": market.provincial_area,
                "point_id": point.point_id,
                "city": point.city,
                "roles": list(point.roles),
            }
        seen_points.add(key)
        filtered_rows.append(row)

    point_insights = [
        item
        for row in filtered_rows
        if (item := _analyze_row(row, start_date)) is not None
    ]
    configured_counts = (
        {market.market_id: len(market.points) for market in market_config}
        if market_config is not None
        else {}
    )
    for row in filtered_rows:
        market_id = str(row.get("market_id") or row.get("market") or row.get("province") or "unknown")
        if market_config is None:
            configured_counts[market_id] = configured_counts.get(market_id, 0) + 1

    grouped: dict[str, list[PointInsight]] = {}
    for insight in point_insights:
        grouped.setdefault(insight.market_id, []).append(insight)

    aggregated: list[MarketInsight] = []
    for market_id, insights in grouped.items():
        risk_events = sorted(
            (item for item in insights if item.severity > 0),
            key=lambda item: (
                -item.severity,
                -max(item.cooling_degree_hours, item.heating_degree_hours),
                -item.solar_stress,
                -item.wind_peak,
            ),
        )
        primary = max(
            risk_events or insights,
            key=lambda item: (
                item.severity,
                max(item.cooling_degree_hours, item.heating_degree_hours),
                item.solar_stress,
                item.wind_peak,
            ),
        )
        load_points = _role_insights(insights, "load")
        solar_points = _role_insights(insights, "solar")
        wind_points = _role_insights(insights, "wind")

        def role_mean(items: list[PointInsight], field: str) -> float | None:
            values = [float(getattr(item, field)) for item in items]
            return mean(values) if values else None

        aggregated.append(
            MarketInsight(
                market_id=market_id,
                market=primary.market,
                province=primary.province,
                city=primary.city,
                configured_points=configured_counts.get(market_id, len(insights)),
                covered_points=len(insights),
                severity=max(item.severity for item in insights),
                window=primary.window,
                directions=primary.directions,
                driver=f"{primary.city}：{primary.driver}",
                change=primary.change,
                confidence=_worst_confidence(insights),
                cooling_degree_hours=role_mean(load_points, "cooling_degree_hours"),
                heating_degree_hours=role_mean(load_points, "heating_degree_hours"),
                solar_stress=role_mean(solar_points, "solar_stress"),
                wind_peak=role_mean(wind_points, "wind_peak"),
                point_insights=tuple(insights),
                risk_events=tuple(risk_events),
                risk_signal_count=sum(
                    1
                    for item in insights
                    for event in item.signal_events
                    if event.severity > 0
                ),
            )
        )
    return aggregated


def _insight_line(
    insight: MarketInsight,
    *,
    include_supplemental: bool = False,
) -> str:
    direction = " / ".join(insight.directions)
    supplemental_events: list[str] = []
    skipped_primary = False
    for point in insight.point_insights:
        for event in point.signal_events:
            if event.severity <= 0:
                continue
            if (
                not skipped_primary
                and point.city == insight.city
                and event.direction in insight.directions
                and event.window == insight.window
            ):
                skipped_primary = True
                continue
            supplemental_events.append(
                f"{point.city} {event.window} {event.direction}"
            )
    if include_supplemental and supplemental_events:
        additional = "；补充：" + "；".join(supplemental_events)
    elif supplemental_events:
        additional = (
            f"；区内另有 {len(supplemental_events)} 个独立风险信号"
            "（时段不合并，展开版可见）"
        )
    else:
        additional = ""
    return (
        f"- **{insight.risk_label}｜{insight.window}**　{direction}；"
        f"驱动：{insight.driver}；变化：{insight.change}；置信度：{insight.confidence}"
        f"{additional}"
    )


def _ranking_lines(insights: list[MarketInsight]) -> list[str]:
    def eligible(item: MarketInsight) -> bool:
        return item.configured_points <= 1 or item.covered_points >= 2

    load = sorted(
        (
            item
            for item in insights
            if eligible(item)
            and item.cooling_degree_hours is not None
            and item.heating_degree_hours is not None
        ),
        key=lambda item: max(
            float(item.cooling_degree_hours or 0.0),
            float(item.heating_degree_hours or 0.0),
        ),
        reverse=True,
    )[:3]
    solar = sorted(
        (item for item in insights if eligible(item) and item.solar_stress is not None),
        key=lambda item: float(item.solar_stress or 0.0),
        reverse=True,
    )[:3]
    wind = sorted(
        (item for item in insights if eligible(item) and item.wind_peak is not None),
        key=lambda item: float(item.wind_peak or 0.0),
        reverse=True,
    )[:3]

    def labels(items: list[MarketInsight], role: str) -> str:
        if not items:
            return "样本不足"
        rendered = []
        divergent_label = {
            "solar": "光伏资源代理区内分化",
            "wind": "地面风资源代理区内分化",
        }.get(role)
        for item in items:
            label = item.label
            if divergent_label and divergent_label in _combined_directions(
                _role_insights(list(item.point_insights), role)
            ):
                label += f"（{divergent_label}）"
            rendered.append(label)
        return "、".join(rendered)

    return [
        "负荷天气压力（角色内等权样本）：" + labels(load, "load"),
        "光资源转弱代理（角色内等权样本）：" + labels(solar, "solar"),
        "地面风资源代理（角色内等权样本）：" + labels(wind, "wind"),
    ]


def _risk_window_duration_hours(window: str) -> float:
    total = 0.0
    for start_hour, start_minute, end_hour, end_minute in re.findall(
        r"(\d{2}):(\d{2})[–-](\d{2}):(\d{2})",
        window or "",
    ):
        start = int(start_hour) * 60 + int(start_minute)
        end = int(end_hour) * 60 + int(end_minute)
        if end < start:
            end += 24 * 60
        total += max(0, end - start) / 60.0
    return total


def _risk_relative_strength(insight: MarketInsight) -> float:
    """Compare unlike proxy signals against their transparent trigger scale."""

    candidates = [
        float(insight.cooling_degree_hours or 0.0) / 24.0,
        float(insight.heating_degree_hours or 0.0) / 36.0,
        float(insight.solar_stress or 0.0) / 75.0,
        float(insight.wind_peak or 0.0) / 10.0,
    ]
    return max(candidates)


def _risk_change_priority(change: str) -> int:
    if re.search(r"新增|上调|增强|转弱", change or ""):
        return 2
    if re.search(r"变化不大|持平", change or ""):
        return 1
    if re.search(r"缓解|下调|减弱|改善", change or ""):
        return 0
    return -1


def _risk_confidence_priority(confidence: str) -> int:
    if str(confidence).startswith("较高"):
        return 3
    if str(confidence).startswith("中等"):
        return 2
    if str(confidence).startswith("偏低"):
        return 1
    return 0


def _risk_priority_key(insight: MarketInsight) -> tuple[float, float, int, float, int, str]:
    return (
        -float(insight.severity),
        -_risk_relative_strength(insight),
        -_risk_change_priority(insight.change),
        -_risk_window_duration_hours(insight.window),
        -_risk_confidence_priority(insight.confidence),
        insight.market,
    )


def _source_summary(rows: list[dict[str, Any]]) -> str:
    sources = {
        provider
        for row in rows
        for submission in (row.get("submissions") or {}).values()
        for provider in submission.aggregated_forecast.providers_used
    }
    return " / ".join(sorted(sources)) if sources else "暂无可用源"


def briefing_coverage(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    market_config: tuple[MarketZone, ...] | None = None,
) -> dict[str, Any]:
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    markets: dict[str, dict[str, Any]] = {}
    areas: dict[str, set[str]] = {}
    if market_config is not None:
        for market in market_config:
            markets[market.market_id] = {
                "name": market.market_name,
                "province": market.provincial_area,
                "total_points": len(market.points),
                "configured_point_ids": {point.point_id for point in market.points},
                "covered_point_ids": set(),
                "baseline_point_ids": set(),
            }
            areas.setdefault(market.provincial_area, set()).add(market.market_id)

    for row in rows:
        market_id = str(row.get("market_id") or row.get("market") or row.get("province") or "unknown")
        if market_config is not None and market_id not in markets:
            continue
        market = markets.setdefault(
            market_id,
            {
                "name": row.get("market") or row.get("province") or "未知分析区",
                "province": row.get("province") or row.get("market") or "未知地区",
                "total_points": 0,
                "configured_point_ids": set(),
                "covered_point_ids": set(),
                "baseline_point_ids": set(),
            },
        )
        point_id = str(row.get("point_id") or row.get("city") or f"row-{id(row)}")
        if (
            market_config is not None
            and point_id not in market["configured_point_ids"]
        ):
            continue
        if market_config is None:
            market["configured_point_ids"].add(point_id)
            market["total_points"] = len(market["configured_point_ids"])
        province = str(market["province"])
        areas.setdefault(province, set()).add(market_id)
        if (row.get("submissions") or {}).get(tomorrow_date) is not None:
            market["covered_point_ids"].add(point_id)
        if (row.get("submissions") or {}).get(start_date) is not None:
            market["baseline_point_ids"].add(point_id)

    for market in markets.values():
        market["covered_points"] = len(market["covered_point_ids"])
        market["baseline_points"] = len(market["baseline_point_ids"])

    covered_markets = sum(1 for item in markets.values() if item["covered_points"] > 0)
    full_markets = sum(
        1
        for item in markets.values()
        if item["covered_points"] == item["total_points"] and item["total_points"] > 0
    )
    partial_markets = sum(
        1
        for item in markets.values()
        if 0 < item["covered_points"] < item["total_points"]
    )
    single_point_markets = sum(
        1
        for item in markets.values()
        if item["covered_points"] == 1 and item["total_points"] > 1
    )
    missing_markets = [item["name"] for item in markets.values() if item["covered_points"] == 0]
    covered_areas = sum(
        1
        for market_ids in areas.values()
        if any(markets[market_id]["covered_points"] > 0 for market_id in market_ids)
    )
    total_points = sum(int(item["total_points"]) for item in markets.values())
    covered_points = sum(int(item["covered_points"]) for item in markets.values())
    baseline_points = sum(int(item["baseline_points"]) for item in markets.values())
    return {
        "provincial_areas": {
            "covered": covered_areas,
            "total": len(areas),
        },
        "markets": {
            "covered": covered_markets,
            "total": len(markets),
            "full": full_markets,
            "partial": partial_markets,
            "single_point": single_point_markets,
            "missing": len(missing_markets),
        },
        "points": {
            "covered": covered_points,
            "total": total_points,
            "missing": total_points - covered_points,
        },
        "baseline_points": {
            "covered": baseline_points,
            "total": total_points,
            "missing": total_points - baseline_points,
        },
        "missing_market_names": missing_markets,
    }


def _coverage_text(coverage: dict[str, Any]) -> str:
    areas = coverage["provincial_areas"]
    markets = coverage["markets"]
    points = coverage["points"]
    baseline = coverage["baseline_points"]
    return (
        f"省级地区 {areas['covered']}/{areas['total']}　·　"
        f"分析区有数据 {markets['covered']}/{markets['total']}"
        f"（完整 {markets['full']}，部分 {markets['partial']}，其中单点 {markets['single_point']}）　·　"
        f"明日代表点 {points['covered']}/{points['total']}　·　"
        f"今日对比基线 {baseline['covered']}/{baseline['total']}"
    )


def _market_target_valid_time(
    rows: list[dict[str, Any]],
    *,
    market_id: str,
    target_date: str,
) -> dict[str, str] | None:
    windows: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.get("market_id") or "") != market_id:
            continue
        submission = (row.get("submissions") or {}).get(target_date)
        if submission is None:
            continue
        valid_time = getattr(submission.time_info, "valid_time", None)
        start = str(getattr(valid_time, "start", None) or "").strip()
        end = str(getattr(valid_time, "end", None) or "").strip()
        timezone_name = str(getattr(valid_time, "timezone", None) or "").strip()
        if start and end and timezone_name:
            windows.add((start, end, timezone_name))
    if len(windows) != 1:
        return None
    start, end, timezone_name = next(iter(windows))
    return {"start": start, "end": end, "timezone": timezone_name}


def _market_risk_snapshots(
    insights: list[MarketInsight],
    rows: list[dict[str, Any]],
    start_date: str,
) -> list[dict[str, Any]]:
    """Persist derived market features only; provider raw payloads never enter versions."""

    target_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    return [
        {
            "market_id": item.market_id,
            "market": item.market,
            "province": item.province,
            "representative_point": item.city,
            "severity": item.severity,
            "window": item.window,
            "directions": list(item.directions),
            "confidence": item.confidence,
            "covered_points": item.covered_points,
            "configured_points": item.configured_points,
            "target_valid_time": _market_target_valid_time(
                rows,
                market_id=item.market_id,
                target_date=target_date,
            ),
            "proxy_method_version": POWER_WEATHER_PROXY_VERSION,
            "weight_version": MARKET_RISK_WEIGHT_VERSION,
        }
        for item in insights
    ]


def _display_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "未提供"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "未提供"
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return "未提供"
    return timestamp.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")


def _provider_label(provider: str) -> str:
    return {
        "open_meteo": "Open-Meteo",
        "qweather": "和风天气",
        "caiyun": "彩云天气",
    }.get(provider, provider)


def _quality_reason_label(reason: str) -> str:
    if ":" in reason:
        code, provider = reason.split(":", 1)
        if code == "provider_issued_at_missing":
            return f"{_provider_label(provider)}起报时间未提供"
        if code == "source_url_missing":
            return f"{_provider_label(provider)}来源链接不可追溯"
        if code == "content_sha256_missing":
            return f"{_provider_label(provider)}内容指纹不可用"
    return {
        "retrieved_at_missing": "实际抓取时间未提供",
        "valid_time_missing": "预报有效时间未提供",
        "representative_point_coverage_incomplete": "代表点覆盖不完整",
        "today_baseline_coverage_incomplete": "今日对比基线不完整",
        "external_provider_missing": "没有可用外部气象源",
        "single_external_provider": "仅单一外部气象源",
    }.get(reason, "质量信息降级")


def _run_quality_text(
    run_metadata: dict[str, Any],
    version_change: dict[str, Any],
) -> str:
    valid_time = run_metadata.get("valid_time") or {}
    sources = run_metadata.get("sources") or []
    quality = run_metadata.get("quality") or {}
    confidence = run_metadata.get("confidence") or {}
    coverage = run_metadata.get("metric_coverage") or {}
    point_coverage = coverage.get("weather_points") or {}
    issued_lines = []
    for provider, issued_at in (run_metadata.get("provider_issued_at") or {}).items():
        if issued_at:
            issued_lines.append(f"{_provider_label(str(provider))} 起报 {_display_timestamp(issued_at)}")
        else:
            issued_lines.append(f"{_provider_label(str(provider))} 起报时间未提供")
    if not issued_lines:
        issued_lines.append("数据源起报时间未提供")

    source_evidence_lines: list[str] = []
    for item in run_metadata.get("provider_run_metadata") or []:
        if not isinstance(item, dict):
            continue
        provider_label = _provider_label(str(item.get("provider") or "unknown"))
        urls = [str(value) for value in item.get("source_urls") or [] if str(value).strip()]
        hashes = [
            str(value)
            for value in item.get("content_sha256s") or []
            if str(value).strip()
        ]
        if urls and hashes:
            source_evidence_lines.append(
                f"- {provider_label} source_url {urls[0]}｜SHA-256 {hashes[0]}"
                + (f"｜共 {len(hashes)} 个内容指纹" if len(hashes) > 1 else "")
            )
    if not source_evidence_lines:
        source_evidence_lines.append("- source_url / SHA-256 未提供")

    if version_change.get("status") == "available":
        excluded_markets = int(version_change.get("excluded_markets") or 0)
        version_line = (
            f"较上一同发布时次 {version_change.get('previous_run_id')}："
            f"{version_change.get('upgraded_markets', 0)} 个分析区上调，"
            f"{version_change.get('downgraded_markets', 0)} 个下调，"
            f"{version_change.get('unchanged_markets', 0)} 个不变"
            + (
                f"；另 {excluded_markets} 个因有效时间或方法版本不一致未纳入"
                if excluded_markets
                else ""
            )
        )
    else:
        reason = str(version_change.get("reason") or "")
        version_line = {
            "target_valid_time_mismatch": "目标有效时间不同，不进行版本升降比较",
            "proxy_method_version_mismatch": "代理口径版本不同，不进行版本升降比较",
            "weight_version_mismatch": "权重版本不同，不进行版本升降比较",
            "current_comparison_provenance_incomplete": "当前版本溯源信息不完整，不进行版本升降比较",
            "previous_comparison_provenance_incomplete": "上一版本溯源信息不完整，不进行版本升降比较",
            "market_comparison_metadata_incomplete": "分析区有效时间或方法版本不完整，不进行版本升降比较",
            "current_methodology_metadata_conflict": "当前分析区方法版本与运行元数据冲突，不进行版本升降比较",
            "previous_methodology_metadata_conflict": "上一分析区方法版本与运行元数据冲突，不进行版本升降比较",
        }.get(
            reason,
            "上一同发布时次版本不可用"
            "（没有可比快照）",
        )

    reasons = quality.get("reasons") or []
    quality_reason = (
        "、".join(_quality_reason_label(str(reason)) for reason in reasons)
        if reasons
        else "元数据与覆盖完整"
    )
    quality_status = {
        "good": "良好",
        "degraded": "降级",
        "unusable": "不可用",
    }.get(str(quality.get("status")), "不可用")
    return "\n".join(
        [
            f"- 预报运行 `{run_metadata.get('forecast_run_id')}`｜发布时次 {run_metadata.get('release_slot')}",
            (
                f"- 代理口径 {run_metadata.get('proxy_method_version') or '未提供'}｜"
                f"权重版本 {run_metadata.get('weight_version') or '未提供'}"
            ),
            f"- 实际抓取 {_display_timestamp(run_metadata.get('retrieved_at'))}",
            f"- 聚合完成 {_display_timestamp(run_metadata.get('aggregation_completed_at'))}",
            (
                f"- 有效时间 {_display_timestamp(valid_time.get('start'))} 至 "
                f"{_display_timestamp(valid_time.get('end'))}｜{valid_time.get('timezone') or '未提供'}"
            ),
            f"- 来源 {' / '.join(_provider_label(str(item)) for item in sources) or '暂无可追溯来源'}",
            *source_evidence_lines,
            "- " + "；".join(issued_lines),
            (
                f"- 数据质量 {quality_status}｜"
                f"代表点 {point_coverage.get('covered', 0)}/{point_coverage.get('total', 0)}｜"
                f"置信度 {confidence.get('level') or '不可用'}｜{quality_reason}"
            ),
            f"- {version_line}",
        ]
    )


def build_briefing_card(
    rows: list[dict[str, Any]],
    start_date: str,
    typhoon_block: str | None = None,
    generated_at: datetime | None = None,
    *,
    expanded: bool = False,
    market_config: tuple[MarketZone, ...] | None = None,
    run_metadata: dict[str, Any] | None = None,
    version_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(SHANGHAI_TZ)
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    insights = _aggregate_market_insights(
        rows,
        start_date,
        market_config=market_config,
    )
    risks = sorted(
        (item for item in insights if item.severity > 0),
        key=_risk_priority_key,
    )
    top_risks = risks[:5]
    remaining_risks = risks[5:]
    stable = sorted((item for item in insights if item.severity == 0), key=lambda item: item.market)
    coverage = briefing_coverage(rows, start_date, market_config=market_config)
    missing_markets = list(coverage["missing_market_names"])
    top_severity = top_risks[0].severity if top_risks else 0
    template = {4: "red", 3: "orange", 2: "blue", 1: "grey"}.get(top_severity, "wathet")
    if typhoon_block and template in {"grey", "wathet", "blue"}:
        template = "orange"

    if top_risks:
        conclusion = "；".join(
            f"明日{item.risk_label}{item.window}{' / '.join(item.directions[:2])}"
            for item in top_risks[:3]
        )
    else:
        conclusion = "今日至明日未发现达到阈值的异常分析区，仍需结合负荷、出力和机组信息复核。"
    displayed_insights = (
        [*risks, *stable]
        if expanded
        else top_risks
    )
    risk_lines = [
        _insight_line(item, include_supplemental=expanded)
        for item in displayed_insights
    ]
    if not risk_lines:
        risk_lines = ["- 暂无达到展示阈值的异常。"]

    health = (
        f"生成 {generated.strftime('%m/%d %H:%M')}　·　{_coverage_text(coverage)}　·　"
        f"来源 {_source_summary(rows)}　·　范围 今日+明日"
    )
    other_lines: list[str] = []
    if remaining_risks and not expanded:
        other_lines.append(f"另有 {len(remaining_risks)} 个较低优先级风险分析区未在卡片展开")
    if stable:
        if not expanded:
            other_lines.append(f"稳定分析区 {len(stable)} 个，精简卡未逐一列出")
    if coverage["markets"]["partial"]:
        other_lines.append(f"部分覆盖分析区 {coverage['markets']['partial']} 个")
    if missing_markets:
        other_lines.append(
            f"明日数据完全缺失分析区 {len(missing_markets)} 个：{'、'.join(missing_markets)}"
        )
    if coverage["points"]["missing"]:
        other_lines.append(f"明日缺失代表点 {coverage['points']['missing']} 个")
    if coverage["baseline_points"]["missing"]:
        other_lines.append(f"今日对比基线缺失代表点 {coverage['baseline_points']['missing']} 个")
    fallback_points = 0
    for row in rows:
        if "solar" not in tuple(row.get("roles") or ("load", "solar", "wind")):
            continue
        submission = (row.get("submissions") or {}).get(tomorrow_date)
        if submission is None:
            continue
        if _day_metrics(submission)["solar_proxy_method"] == "cloud_rain_fallback":
            fallback_points += 1
    if fallback_points:
        other_lines.append(
            "光资源代理质量：短波辐射缺失，"
            f"{fallback_points} 个代表点降级为云量+降水代理"
        )

    elements: list[dict[str, Any]] = [
        {"tag": "note", "elements": [{"tag": "plain_text", "content": health}]},
    ]
    if run_metadata is not None:
        elements.extend(
            [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**预报版本与数据质量**\n"
                            + _run_quality_text(run_metadata, version_change or {})
                        ),
                    },
                },
                {"tag": "hr"},
            ]
        )
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
                "text": {
                    "tag": "lark_md",
                    "content": (
                        (
                            "**全部分析区明细（每区优先点位，跨点时段不合并）**\n"
                            if expanded
                            else "**Top 5 气象侧风险**\n"
                        )
                        + "\n".join(risk_lines)
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**资源代理排行**\n" + "\n".join(_ranking_lines(insights))},
            },
        ]
    )
    if other_lines:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**其余分析区与数据质量**\n" + "\n".join(other_lines)},
            }
        )
    if not expanded:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "回复 **“展开全部分析区”** 查看全部分析区明细（直接读取本次快照，不重复抓取）。",
                },
            }
        )
    elements.extend(
        [
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            "当前为多城市代表点样本扫描，不等于全省实况、调度口径或交易市场结论；"
                            "资源排行为角色内代表点等权样本，单点覆盖分析区不参与全区排行；"
                            "光资源优先采用日照时段短波辐射积分；辐射缺失时云量/降水仅作降级代理，"
                            "均非实际光伏出力。10米风仅作地面风资源代理。"
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
                    "content": (
                        f"⚡ 电力气象决策晨报 {'3.0' if run_metadata is not None else '2.0'}"
                        f"{'·全部明细' if expanded else ''}｜"
                        f"{start_date[5:].replace('-', '/')}–{tomorrow_date[5:].replace('-', '/')}"
                    ),
                },
            },
            "elements": elements,
        },
    }


def briefing_cache_key(start_date: str) -> str:
    return f"{start_date}:{MARKET_CONFIG_VERSION}:{POWER_BRIEFING_REPORT_VERSION}"


def briefing_statistics(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    market_config: tuple[MarketZone, ...] | None = None,
) -> dict[str, int]:
    coverage = briefing_coverage(rows, start_date, market_config=market_config)
    insights = _aggregate_market_insights(
        rows,
        start_date,
        market_config=market_config,
    )
    risk_count = sum(1 for item in insights if item.severity > 0)
    stable_count = sum(1 for item in insights if item.severity == 0)
    missing_count = int(coverage["markets"]["missing"])
    top_count = min(5, risk_count)
    return {
        "top_risks": top_count,
        "remaining_risks": max(0, risk_count - top_count),
        "stable_markets": stable_count,
        "missing_markets": missing_count,
        "configured_markets": int(coverage["markets"]["total"]),
        "classified_markets": top_count + max(0, risk_count - top_count) + stable_count + missing_count,
    }


async def generate_briefing_snapshot(
    service: Any,
    typhoon_client: TyphoonClient | None,
    start_date: str,
    *,
    cache: BriefingCache,
    generated_at: datetime | None = None,
    forecast_run_id: str | None = None,
    release_slot: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(SHANGHAI_TZ)
    run_id = forecast_run_id or f"briefing-{uuid4().hex}"
    declared_release_slot = release_slot or generated.strftime("%H:00")
    rows = list(
        await asyncio.gather(
            *[
                _fetch(service, market, point, start_date)
                for market, point in MARKET_POINTS
            ]
        )
    )
    coverage = briefing_coverage(
        rows,
        start_date,
        market_config=NATIONAL_MARKETS,
    )
    if coverage["points"]["covered"] == 0:
        raise RuntimeError("全部代表点均无明日数据，晨报未生成")

    insights = _aggregate_market_insights(
        rows,
        start_date,
        market_config=NATIONAL_MARKETS,
    )
    risk_snapshots = _market_risk_snapshots(insights, rows, start_date)
    previous = cache.load_previous_same_release(
        report_date=start_date,
        release_slot=declared_release_slot,
        market_config_version=MARKET_CONFIG_VERSION,
        report_version=POWER_BRIEFING_REPORT_VERSION,
    )
    run_metadata = build_run_provenance(
        rows,
        coverage,
        forecast_run_id=run_id,
        release_slot=declared_release_slot,
        proxy_method_version=POWER_WEATHER_PROXY_VERSION,
        weight_version=MARKET_RISK_WEIGHT_VERSION,
    )
    if run_metadata["quality"]["status"] == "unusable":
        raise RuntimeError("briefing requires traceable external weather provenance")
    version_change = compare_market_risk_versions(
        risk_snapshots,
        previous,
        current_run_metadata=run_metadata,
    )

    typhoon_block = None
    if typhoon_client is not None:
        if not typhoon_client.enabled:
            typhoon_block = (
                "**🌀 台风实时数据不可用**\n"
                "来源许可或数据门禁未通过，本期不展示台风事实；这不代表无活跃台风。"
            )
        else:
            try:
                typhoon_block = format_active_for_briefing(
                    await typhoon_client.active_storms()
                )
            except Exception:  # noqa: BLE001 - fail closed without leaking provider details
                typhoon_block = (
                    "**🌀 台风实时数据不可用**\n"
                    "已许可来源本次未返回可用数据，本期不展示台风事实；这不代表无活跃台风。"
                )

    cache_key = briefing_cache_key(start_date)
    expires = generated + timedelta(seconds=cache.ttl_seconds)
    return {
        "schema_version": 2,
        "cache_key": cache_key,
        "report_date": start_date,
        "market_config_version": MARKET_CONFIG_VERSION,
        "report_version": POWER_BRIEFING_REPORT_VERSION,
        "generated_at": generated.isoformat(),
        "expires_at": expires.isoformat(),
        **run_metadata,
        "previous_run_id": version_change.get("previous_run_id"),
        "version_change": version_change,
        "coverage": coverage,
        "statistics": briefing_statistics(
            rows,
            start_date,
            market_config=NATIONAL_MARKETS,
        ),
        "market_risk_snapshots": risk_snapshots,
        "summary_card": build_briefing_card(
            rows,
            start_date,
            typhoon_block=typhoon_block,
            generated_at=generated,
            market_config=NATIONAL_MARKETS,
            run_metadata=run_metadata,
            version_change=version_change,
        ),
        "detail_card": build_briefing_card(
            rows,
            start_date,
            typhoon_block=typhoon_block,
            generated_at=generated,
            expanded=True,
            market_config=NATIONAL_MARKETS,
            run_metadata=run_metadata,
            version_change=version_change,
        ),
    }


async def get_or_generate_briefing(
    service: Any,
    typhoon_client: TyphoonClient | None,
    start_date: str,
    *,
    cache: BriefingCache,
    wait_seconds: float = 600.0,
    release_slot: str | None = None,
) -> tuple[dict[str, Any], bool]:
    cache_key = briefing_cache_key(start_date)
    cached = cache.load_fresh(cache_key)
    if cached is not None and (
        release_slot is None or str(cached.get("release_slot") or "") == release_slot
    ):
        return cached, True

    owner = uuid4().hex
    deadline = asyncio.get_running_loop().time() + max(1.0, wait_seconds)
    while True:
        cached = cache.load_fresh(cache_key)
        if cached is not None and (
            release_slot is None or str(cached.get("release_slot") or "") == release_slot
        ):
            return cached, True
        if cache.claim_generation(cache_key, owner, lease_seconds=max(60, int(wait_seconds))):
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("等待另一进程生成晨报超时")
        await asyncio.sleep(0.25)

    lease_owned = True
    try:
        snapshot = await generate_briefing_snapshot(
            service,
            typhoon_client,
            start_date,
            cache=cache,
            release_slot=release_slot,
        )
        generated_at = datetime.fromisoformat(str(snapshot["generated_at"])).timestamp()
        cache.save_and_release(
            cache_key,
            owner,
            snapshot,
            generator_version=POWER_BRIEFING_REPORT_VERSION,
            generated_at=generated_at,
        )
        lease_owned = False
        return snapshot, False
    finally:
        if lease_owned:
            try:
                cache.release_generation(cache_key, owner)
            except Exception:  # noqa: BLE001 - preserve the original error/cancellation
                pass
