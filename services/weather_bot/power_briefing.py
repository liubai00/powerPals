# -*- coding: utf-8 -*-
"""电力气象交易晨报 3.2：完整窗口、同目标比较和代理边界优先。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from statistics import mean
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from services.weather_bot.briefing_cache import BriefingCache
from services.weather_bot.briefing_versions import (
    build_run_provenance,
    compare_market_risk_versions,
    compare_window_assessment_versions,
)
from services.weather_bot.models import ForecastRequest
from services.weather_bot.power_briefing_delta import build_afternoon_delta_card
from services.weather_bot.power_briefing_markets import (
    DEFAULT_ANALYSIS_WINDOWS,
    MARKET_CONFIG_VERSION,
    NATIONAL_MARKETS,
    AnalysisWindow,
    MarketZone,
    RepresentativePoint,
    representative_points,
    validate_analysis_windows,
)
from services.weather_bot.typhoon import TyphoonClient, format_active_for_briefing
from services.weather_bot.workbench import collect_forecasts_with_errors


POWER_BRIEFING_REPORT_VERSION = "power-briefing-3.2"
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
class WindowAttention:
    market_id: str
    market: str
    city: str
    target_date: str
    relative_day: str
    window_id: str
    window_label: str
    start_hour: int
    end_hour: int
    signal_type: str
    direction: str
    severity: int
    driver: str
    why_attention: str
    verification_item: str
    confidence: str


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
            coverage = _representative_point_coverage_text(
                self.covered_points,
                self.configured_points,
            )
            if self.covered_points == 1:
                return f"{self.market}·{self.city}代表点｜{coverage}"
            return f"{self.market}｜{coverage}"
        return f"{self.market}·{self.city}代表点"

    @property
    def risk_label(self) -> str:
        if self.configured_points <= 1:
            return self.label
        return (
            f"{self.market}·{self.city}代表点｜"
            f"{_representative_point_coverage_text(self.covered_points, self.configured_points)}"
        )


def _representative_point_coverage_text(covered: int, configured: int) -> str:
    if covered >= configured:
        return f"用于分析的{configured}个城市都有数据"
    elif covered == 1:
        return f"用于分析的{configured}个城市中，仅1个有数据；不能代表全区"
    else:
        return f"用于分析的{configured}个城市中，{covered}个有数据；结论可能不完整"


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
            return "代表点不足"
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
        "负荷天气压力（同类代表点等权汇总）：" + labels(load, "load"),
        "光资源转弱代理（同类代表点等权汇总）：" + labels(solar, "solar"),
        "地面风资源代理（同类代表点等权汇总）：" + labels(wind, "wind"),
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
    sources: set[str] = set()
    for row in rows:
        for submission in (row.get("submissions") or {}).values():
            reviewed = [
                str(item).strip()
                for item in submission.data_profile.data_sources_summary
                if str(item).strip()
            ]
            sources.update(reviewed or submission.aggregated_forecast.providers_used)
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
        f"（所有代表城市均有数据 {markets['full']}，部分代表城市缺数据 {markets['partial']}，"
        f"仅1个代表城市有数据 {markets['single_point']}）　·　"
        f"明日代表城市 {points['covered']}/{points['total']}　·　"
        f"今日对比城市 {baseline['covered']}/{baseline['total']}"
    )


def _briefing_cutoff(
    start_date: str,
    generated_at: datetime,
    run_metadata: dict[str, Any] | None,
) -> datetime:
    release_slot = str((run_metadata or {}).get("release_slot") or "").strip()
    if re.fullmatch(r"\d{2}:\d{2}", release_slot):
        try:
            return datetime.fromisoformat(f"{start_date}T{release_slot}:00+08:00")
        except ValueError:
            pass
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        return generated_at.replace(tzinfo=SHANGHAI_TZ)
    return generated_at.astimezone(SHANGHAI_TZ)


def _window_points(submission: Any, start_hour: int, end_hour: int) -> list[Any]:
    return [
        point
        for point in submission.aggregated_forecast.points
        if (hour := _hour(point)) is not None and start_hour <= hour < end_hour
    ]


def _detect_window_signal(
    points: list[Any],
    roles: tuple[str, ...],
) -> tuple[str, str, int, str, str, str] | None:
    temperatures = [
        value
        for point in points
        if (
            value := (
                _value(point, "apparent_temperature")
                if _value(point, "apparent_temperature") is not None
                else _value(point, "temperature")
            )
        )
        is not None
    ]
    rain_peak = _max_metric(points, "precipitation_probability")
    cloud_mean = _mean_metric(points, "cloud_cover")
    wind_peak = _max_metric(points, "wind_speed")
    same_hour_compound = any(
        (_value(point, "precipitation_probability") or 0.0) >= 70
        and (_value(point, "wind_speed") or 0.0) >= 10
        for point in points
    )
    if same_hour_compound:
        return (
            "local_compound",
            "局地风雨复合天气风险",
            4,
            f"降水概率峰值{rain_peak:.0f}%且10米风峰值{wind_peak:.1f}m/s",
            "强降水与10米地面风在同一时段出现",
            "场站和线路运行信息、新能源功率预测",
        )
    if "load" in roles and temperatures and (max(temperatures) >= 35 or min(temperatures) <= 0):
        driver = (
            f"体感温度峰值{max(temperatures):.1f}℃"
            if max(temperatures) >= 35
            else f"体感温度低值{min(temperatures):.1f}℃"
        )
        return (
            "load",
            "负荷天气压力代理偏高",
            3,
            driver,
            "体感温度可能放大制冷或采暖天气压力",
            "对应时段负荷预测、机组可用状态",
        )
    if "solar" in roles:
        radiation = [
            value
            for point in points
            if (value := _value(point, "shortwave_radiation")) is not None
        ]
        if radiation and sum(radiation) < 150 * len(points):
            return (
                "solar",
                "光资源代理转弱",
                2,
                f"窗口短波辐射均值{mean(radiation):.0f}W/m²",
                "午间短波辐射天气资源偏弱",
                "新能源功率预测和场站运行信息",
            )
        if not radiation and (rain_peak >= 60 or cloud_mean >= 75):
            return (
                "solar",
                "光资源代理转弱",
                2,
                f"短波辐射缺失，云量{cloud_mean:.0f}% / 降水概率{rain_peak:.0f}%",
                "午间光资源天气条件转弱（云量和降水降级代理）",
                "新能源功率预测和场站运行信息",
            )
    if "wind" in roles and wind_peak >= 10:
        return (
            "wind",
            "10米地面风资源代理增强",
            2,
            f"10米风峰值{wind_peak:.1f}m/s",
            "地面风条件变化较明显",
            "新能源功率预测、场站和线路运行信息",
        )
    return None


def _window_has_attention(points: list[Any], roles: tuple[str, ...]) -> bool:
    return _detect_window_signal(points, roles) is not None


def _window_attention_items(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    cutoff: datetime,
    market_config: tuple[MarketZone, ...] | None = None,
) -> list[WindowAttention]:
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    cutoff_hour = cutoff.hour + cutoff.minute / 60.0
    grouped: dict[tuple[str, str, str], WindowAttention] = {}
    configured_windows = {
        market.market_id: market.analysis_windows
        for market in market_config or ()
    }
    for relative_day, target_date in (("今日", start_date), ("明日", tomorrow_date)):
        for row in rows:
            submission = (row.get("submissions") or {}).get(target_date)
            if submission is None:
                continue
            roles = tuple(row.get("roles") or ("load", "solar", "wind"))
            market_id = str(
                row.get("market_id") or row.get("market") or row.get("province") or "unknown"
            )
            windows = configured_windows.get(market_id, DEFAULT_ANALYSIS_WINDOWS)
            for window in windows:
                start_hour = window.start_hour
                label = window.label
                if relative_day == "今日":
                    if window.end_hour <= cutoff_hour:
                        continue
                    if start_hour < cutoff_hour:
                        start_hour = int(cutoff_hour)
                        label = f"{label}剩余"
                points = _window_points(submission, start_hour, window.end_hour)
                signal = _detect_window_signal(points, roles) if points else None
                if signal is None:
                    continue
                signal_type, direction, severity, driver, why_attention, verification_item = signal
                item = WindowAttention(
                    market_id=market_id,
                    market=str(row.get("market") or row.get("province") or "未知分析区"),
                    city=str(row.get("city") or "未知城市"),
                    target_date=target_date,
                    relative_day=relative_day,
                    window_id=window.window_id,
                    window_label=label,
                    start_hour=start_hour,
                    end_hour=window.end_hour,
                    signal_type=signal_type,
                    direction=direction,
                    severity=severity,
                    driver=driver,
                    why_attention=why_attention,
                    verification_item=verification_item,
                    confidence=_confidence_label(submission),
                )
                key = (item.market_id, target_date, window.window_id)
                current = grouped.get(key)
                if current is None or item.severity > current.severity:
                    grouped[key] = item
    business_priority = {
        "early_peak": 3,
        "midday_solar": 3,
        "evening_peak": 3,
        "afternoon_transition": 2,
        "overnight": 1,
        "night": 1,
    }
    return sorted(
        grouped.values(),
        key=lambda item: (
            0 if item.relative_day == "今日" else 1,
            -item.severity,
            -business_priority.get(item.window_id, 0),
            item.start_hour,
            item.market,
        ),
    )


def _window_valid_time(
    target_date: str,
    start_minutes: int,
    end_minutes: int,
) -> dict[str, str]:
    target = date.fromisoformat(target_date)
    start = datetime.combine(target, datetime.min.time(), tzinfo=SHANGHAI_TZ) + timedelta(
        minutes=start_minutes
    )
    end = datetime.combine(target, datetime.min.time(), tzinfo=SHANGHAI_TZ) + timedelta(
        minutes=end_minutes
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": "Asia/Shanghai",
    }


def _window_assessment_snapshots(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    cutoff: datetime,
    market_config: tuple[MarketZone, ...],
) -> list[dict[str, Any]]:
    """Build derived-only assessments for exact market/date/window identities."""

    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    cutoff_minutes = cutoff.hour * 60 + cutoff.minute
    rows_by_market: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        market_id = str(
            row.get("market_id") or row.get("market") or row.get("province") or "unknown"
        )
        rows_by_market.setdefault(market_id, []).append(row)

    snapshots: list[dict[str, Any]] = []
    for relative_day, target_date in (("今日", start_date), ("明日", tomorrow_date)):
        for market in market_config:
            market_rows = rows_by_market.get(market.market_id, [])
            expected_points = len(market.points)
            configured_point_ids = sorted(point.point_id for point in market.points)
            for window in market.analysis_windows:
                start_minutes = window.start_hour * 60
                end_minutes = window.end_hour * 60
                if relative_day == "今日":
                    if end_minutes <= cutoff_minutes:
                        continue
                    start_minutes = max(start_minutes, cutoff_minutes)

                covered_points = 0
                selected_signal: tuple[str, str, int, str, str, str] | None = None
                selected_city = market.points[0].city if market.points else market.market_name
                selected_confidence = "不可用"
                source_run_ids: set[str] = set()
                covered_point_ids: set[str] = set()
                source_set: set[str] = set()
                for row in market_rows:
                    submission = (row.get("submissions") or {}).get(target_date)
                    if submission is None:
                        continue
                    points = [
                        point
                        for point in submission.aggregated_forecast.points
                        if (
                            (hour := _hour(point)) is not None
                            and start_minutes <= hour * 60 < end_minutes
                        )
                    ]
                    if not points:
                        continue
                    covered_points += 1
                    point_id = str(row.get("point_id") or row.get("city") or "").strip()
                    if point_id:
                        covered_point_ids.add(point_id)
                    source_set.update(
                        str(provider).strip()
                        for provider in submission.aggregated_forecast.providers_used
                        if str(provider).strip()
                    )
                    run_id = str(
                        getattr(submission.time_info, "forecast_run_id", None) or ""
                    ).strip()
                    if run_id:
                        source_run_ids.add(run_id)
                    roles = tuple(row.get("roles") or ("load", "solar", "wind"))
                    signal = _detect_window_signal(points, roles)
                    if signal is not None and (
                        selected_signal is None or signal[2] > selected_signal[2]
                    ):
                        selected_signal = signal
                        selected_city = str(row.get("city") or selected_city)
                        selected_confidence = _confidence_label(submission)
                    elif selected_signal is None:
                        selected_city = str(row.get("city") or selected_city)
                        selected_confidence = _confidence_label(submission)

                if covered_points == 0:
                    status = "insufficient_data"
                    signal_type = "insufficient_data"
                    direction = "数据不足，无法判断"
                    severity = 0
                    driver = "该分析区在此窗口没有可用代表城市数据"
                    verification_item = "补齐经过许可且可追溯的气象数据"
                elif selected_signal is None:
                    status = "checked_no_attention"
                    signal_type = "none"
                    direction = "暂无需要重点跟踪的天气侧信号"
                    severity = 0
                    driver = "已完成该窗口的天气侧阈值检查"
                    verification_item = "如电力侧计划变化，仍需复核对应业务数据"
                else:
                    (
                        signal_type,
                        direction,
                        severity,
                        driver,
                        _why_attention,
                        verification_item,
                    ) = selected_signal
                    status = "attention"

                snapshots.append(
                    {
                        "market_id": market.market_id,
                        "market": market.market_name,
                        "representative_point": selected_city,
                        "target_date": target_date,
                        "relative_day": relative_day,
                        "window_id": window.window_id,
                        "window_label": window.label,
                        "target_valid_time": _window_valid_time(
                            target_date,
                            start_minutes,
                            end_minutes,
                        ),
                        "proxy_metric": "window_attention_severity",
                        "signal_type": signal_type,
                        "event_id": (
                            f"{market.market_id}|{target_date}|{signal_type}|"
                            f"{POWER_WEATHER_PROXY_VERSION}"
                            if status == "attention"
                            else None
                        ),
                        "status": status,
                        "severity": severity,
                        "direction": direction,
                        "driver": driver,
                        "verification_item": verification_item,
                        "confidence": selected_confidence,
                        "covered_points": covered_points,
                        "configured_points": expected_points,
                        "configured_point_ids": configured_point_ids,
                        "covered_point_ids": sorted(covered_point_ids),
                        "source_set": sorted(source_set),
                        "source_forecast_run_ids": sorted(source_run_ids),
                        "proxy_method_version": POWER_WEATHER_PROXY_VERSION,
                        "weight_version": MARKET_RISK_WEIGHT_VERSION,
                    }
                )
    return snapshots


def _clock_range(valid_time: dict[str, Any] | None) -> str:
    if not isinstance(valid_time, dict):
        return "时间未提供"
    start = _parse_aware_timestamp(valid_time.get("start"))
    end = _parse_aware_timestamp(valid_time.get("end"))
    if start is None or end is None:
        return "时间未提供"
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI_TZ)


def _today_revision_text(
    window_change: dict[str, Any] | None,
    start_date: str,
    *,
    release_slot: str,
) -> str:
    if not isinstance(window_change, dict):
        return "没有可用的同目标时段版本记录。"
    previous_run_id = str(window_change.get("previous_run_id") or "").strip()
    current_changes = [
        item
        for item in (window_change.get("items") or [])
        if isinstance(item, dict)
        and item.get("target_date") == start_date
        and item.get("lifecycle") in {"upgraded", "weakened", "resolved"}
    ]
    basis = f"对比昨日{release_slot}对今日相同时段的预测"
    if not current_changes:
        if previous_run_id:
            return basis + "\n- 同目标时段未发现达到展示阈值的预测修正。"
        return basis + "\n- 未找到昨日同目标时段的可追溯快照，无法进行版本修正比较。"
    lines = [basis]
    lifecycle_labels = {
        "upgraded": "风险升级",
        "weakened": "风险减弱",
        "resolved": "结束跟踪",
    }
    for item in current_changes:
        lines.extend(
            (
                (
                    f"- **{item.get('market')}·{item.get('representative_point')}代表点｜"
                    f"今日{_clock_range(item.get('target_valid_time'))}｜{item.get('window_label')}｜"
                    f"{lifecycle_labels.get(str(item.get('lifecycle')), '预测修正')}**"
                ),
                (
                    f"  {_plain_weather_message(item.get('previous_direction') or '此前无判断')} → "
                    f"{_plain_weather_message(item.get('current_direction'))}"
                ),
                f"  变化原因：{item.get('driver')}",
                (
                    f"  继续观察：{item.get('verification_item')}｜"
                    f"可靠程度：{_plain_reliability(item.get('confidence'))}"
                ),
            )
        )
    return "\n".join(lines)


def _tomorrow_first_observation_text(
    window_change: dict[str, Any] | None,
    tomorrow_date: str,
) -> str:
    items = [
        item
        for item in ((window_change or {}).get("items") or [])
        if isinstance(item, dict)
        and item.get("target_date") == tomorrow_date
        and item.get("lifecycle") == "first_observation"
        and int(item.get("current_severity") or 0) > 0
    ]
    if not items:
        return "明日全部窗口已完成首次检查，暂无达到关注阈值的天气侧信号。"
    lines = ["首次纳入观察，暂无同时间可比预测。"]
    for item in items[:3]:
        lines.extend(
            (
                (
                    f"- **{item.get('market')}·{item.get('representative_point')}代表点｜"
                    f"明日{_clock_range(item.get('target_valid_time'))}｜{item.get('window_label')}**"
                ),
                (
                    f"  {_plain_weather_message(item.get('current_direction'))}；"
                    f"原因：{item.get('driver')}"
                ),
                (
                    f"  继续观察：{item.get('verification_item')}｜"
                    f"可靠程度：{_plain_reliability(item.get('confidence'))}"
                ),
            )
        )
    return "\n".join(lines)


def _top_focus_text(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    cutoff: datetime,
    market_config: tuple[MarketZone, ...] | None = None,
) -> str:
    items = _window_attention_items(
        rows,
        start_date,
        cutoff=cutoff,
        market_config=market_config,
    )[:3]
    if not items:
        return "- 全部未来窗口已检查，暂无需要重点跟踪的天气侧信号。"
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.extend(
            (
                (
                    f"{index}. **{item.market}·{item.city}代表点｜{item.relative_day}"
                    f"{item.start_hour:02d}:00–{item.end_hour:02d}:00｜{item.window_label}**"
                ),
                f"   {_plain_weather_message(item.direction)}；原因：{item.driver}",
                f"   为什么关注：{item.why_attention}",
                f"   继续观察：{item.verification_item}",
                f"   可靠程度：{_plain_reliability(item.confidence)}",
            )
        )
    return "\n".join(lines)


def _focus_conclusion(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    cutoff: datetime,
    market_config: tuple[MarketZone, ...] | None,
) -> str:
    items = _window_attention_items(
        rows,
        start_date,
        cutoff=cutoff,
        market_config=market_config,
    )[:3]
    if not items:
        return (
            "今天没有需要特别提醒的气象变化。"
            "早峰、午间光伏时段和晚峰均未发现达到关注阈值的天气信号。"
        )
    return "；".join(
        (
            f"{item.relative_day}{item.market}·{item.city}代表点"
            f"{item.start_hour:02d}:00–{item.end_hour:02d}:00"
            f"{_plain_weather_message(item.direction)}"
        )
        for item in items
    ) + "。"


def _reassurance_text() -> str:
    return (
        "当前未配置经交易团队审核的重点市场清单，因此不把全国稳定分析区逐个列为“放心”。"
        "请先配置关注市场；其余窗口仍已在“时间窗口检查”中给出稳定、关注或数据不足状态。"
    )


def _validity_text(run_metadata: dict[str, Any] | None) -> str:
    release_slot = str((run_metadata or {}).get("release_slot") or "09:00")
    if release_slot == "09:00":
        next_update = "计划于15:00复核；只有相较本次09:00预测出现实质变化才推送快报。"
    else:
        next_update = "下一次固定完整更新为次日09:00；期间仅满足告警条件时推送。"
    return (
        f"- {next_update}\n"
        "- 以下情况会推翻当前结论：官方预警新增或升级、关键时段明显移动、"
        "天气代理严重性变化、来源分歧扩大或数据转为不可用。\n"
        "- 电力侧计划、负荷预测、新能源功率预测、机组或线路状态变化需独立复核。"
    )


def _window_status(
    rows: list[dict[str, Any]],
    *,
    target_date: str,
    start_hour: int,
    end_hour: int,
    applicable_market_ids: set[str] | None = None,
) -> str:
    expected_points: dict[str, set[str]] = {}
    covered_points: dict[str, set[str]] = {}
    attention_areas: set[str] = set()
    for row in rows:
        area_id = str(
            row.get("market_id") or row.get("market") or row.get("province") or "unknown"
        )
        if applicable_market_ids is not None and area_id not in applicable_market_ids:
            continue
        point_id = str(row.get("point_id") or row.get("city") or f"row-{id(row)}")
        expected_points.setdefault(area_id, set()).add(point_id)
        submission = (row.get("submissions") or {}).get(target_date)
        if submission is None:
            continue
        points = _window_points(submission, start_hour, end_hour)
        if not points:
            continue
        covered_points.setdefault(area_id, set()).add(point_id)
        roles = tuple(row.get("roles") or ("load", "solar", "wind"))
        if _window_has_attention(points, roles):
            attention_areas.add(area_id)
    if not covered_points:
        return "数据不足，无法判断"
    missing_areas = sum(1 for area_id in expected_points if not covered_points.get(area_id))
    partial_areas = sum(
        1
        for area_id, expected in expected_points.items()
        if covered_points.get(area_id) and covered_points[area_id] != expected
    )
    quality_suffix = ""
    if missing_areas:
        quality_suffix += f"；另{missing_areas}个分析区数据不足"
    if partial_areas:
        quality_suffix += f"；另{partial_areas}个分析区代表城市数据不完整"
    if attention_areas:
        return f"已检查，{len(attention_areas)}个分析区需要重点跟踪{quality_suffix}"
    return f"已检查，暂无需要重点跟踪的信号{quality_suffix}"


def _window_definitions(
    market_config: tuple[MarketZone, ...] | None,
) -> list[tuple[AnalysisWindow, set[str] | None]]:
    if market_config is None:
        return [(window, None) for window in DEFAULT_ANALYSIS_WINDOWS]
    grouped: dict[tuple[str, str, int, int], set[str]] = {}
    definitions: dict[tuple[str, str, int, int], AnalysisWindow] = {}
    for market in market_config:
        validate_analysis_windows(market.analysis_windows)
        for window in market.analysis_windows:
            key = (window.window_id, window.label, window.start_hour, window.end_hour)
            definitions[key] = window
            grouped.setdefault(key, set()).add(market.market_id)
    return [
        (definitions[key], grouped[key])
        for key in sorted(grouped, key=lambda item: (item[2], item[3], item[0]))
    ]


def _verified_morning_review_text(
    observations: list[dict[str, Any]] | None,
    start_date: str,
    *,
    cutoff: datetime,
) -> str:
    metric_labels = {
        "temperature": "气温",
        "apparent_temperature": "体感温度",
        "wind_speed": "10米风速",
        "precipitation": "降水量",
    }
    lines: list[str] = []
    for item in observations or []:
        if not isinstance(item, dict) or item.get("availability_status") != "allowed_for_calculation":
            continue
        metric = str(item.get("metric") or "")
        if metric not in metric_labels:
            continue
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        market = str(item.get("market") or "").strip()
        representative_point = str(item.get("representative_point") or "").strip()
        unit = str(item.get("unit") or "").strip()
        provenance_ref = str(item.get("provenance_ref") or "").strip()
        source_url = str(item.get("source_url") or "").strip()
        parsed_url = urlsplit(source_url)
        valid_time = item.get("valid_time")
        if (
            not market
            or not representative_point
            or not unit
            or not provenance_ref
            or parsed_url.scheme != "https"
            or not parsed_url.netloc
            or not isinstance(valid_time, dict)
        ):
            continue
        start = _parse_aware_timestamp(valid_time.get("start"))
        end = _parse_aware_timestamp(valid_time.get("end"))
        retrieved = _parse_aware_timestamp(item.get("retrieved_at"))
        if start is None or end is None or retrieved is None:
            continue
        if (
            start.date().isoformat() != start_date
            or end <= start
            or end > cutoff
            or retrieved > cutoff + timedelta(minutes=30)
        ):
            continue
        lines.append(
            f"- {market}·{representative_point}代表点｜"
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}｜"
            f"实况{metric_labels[metric]} {float(value):g}{unit}"
        )
    if not lines:
        return (
            "今日早间回顾：未接入经过可用性门禁的实况数据；"
            "已过时段不作为实况展示。"
        )
    return "今日早间回顾（已核验实况）\n" + "\n".join(lines)


def _window_check_text(
    rows: list[dict[str, Any]],
    start_date: str,
    *,
    cutoff: datetime,
    market_config: tuple[MarketZone, ...] | None = None,
    verified_observations: list[dict[str, Any]] | None = None,
) -> str:
    tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    cutoff_minutes = cutoff.hour * 60 + cutoff.minute
    lines = [
        f"分析范围：今日{cutoff.strftime('%H:%M')}–24:00 + 明日00:00–24:00",
        _verified_morning_review_text(
            verified_observations,
            start_date,
            cutoff=cutoff,
        ),
        "",
        "今日剩余时段",
    ]
    definitions = _window_definitions(market_config)
    for window, applicable_market_ids in definitions:
        start_minutes = window.start_hour * 60
        end_minutes = window.end_hour * 60
        rendered_start = window.start_hour * 60
        label = window.label
        if end_minutes <= cutoff_minutes:
            status = "已经过去，不作为未来预报"
        else:
            rendered_start = max(start_minutes, cutoff_minutes)
            if rendered_start > start_minutes:
                label = f"{label}剩余"
            status = _window_status(
                rows,
                target_date=start_date,
                start_hour=rendered_start // 60,
                end_hour=window.end_hour,
                applicable_market_ids=applicable_market_ids,
            )
        lines.append(
            f"今日{rendered_start // 60:02d}:{rendered_start % 60:02d}–{window.end_hour:02d}:00｜"
            f"{label}｜{status}"
        )
    lines.extend(("", "明日完整观察"))
    for window, applicable_market_ids in definitions:
        lines.append(
            f"明日{window.start_hour:02d}:00–{window.end_hour:02d}:00｜{window.label}｜"
            + _window_status(
                rows,
                target_date=tomorrow_date,
                start_hour=window.start_hour,
                end_hour=window.end_hour,
                applicable_market_ids=applicable_market_ids,
            )
        )
    return "\n".join(lines)


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
    snapshots: list[dict[str, Any]] = []
    for item in insights:
        market_rows = [
            row for row in rows if str(row.get("market_id") or "") == item.market_id
        ]
        configured_point_ids = sorted(
            {
                point_id
                for row in market_rows
                if (point_id := str(row.get("point_id") or "").strip())
            }
        )
        covered_point_ids: set[str] = set()
        source_set: set[str] = set()
        for row in market_rows:
            submission = (row.get("submissions") or {}).get(target_date)
            if submission is None or not submission.aggregated_forecast.points:
                continue
            point_id = str(row.get("point_id") or "").strip()
            if point_id:
                covered_point_ids.add(point_id)
            source_set.update(
                str(provider).strip()
                for provider in submission.aggregated_forecast.providers_used
                if str(provider).strip()
            )
        snapshots.append(
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
                "configured_point_ids": configured_point_ids,
                "covered_point_ids": sorted(covered_point_ids),
                "source_set": sorted(source_set),
                "target_valid_time": _market_target_valid_time(
                    rows,
                    market_id=item.market_id,
                    target_date=target_date,
                ),
                "proxy_method_version": POWER_WEATHER_PROXY_VERSION,
                "weight_version": MARKET_RISK_WEIGHT_VERSION,
            }
        )
    return snapshots


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
        previous_report_date = str(version_change.get("previous_report_date") or "日期未记录")
        release_slot = str(run_metadata.get("release_slot") or "时次未记录")
        version_line = (
            f"同目标时段比较基准：{previous_report_date} {release_slot} 运行 "
            f"`{version_change.get('previous_run_id')}`；本次"
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
            "no_previous_same_release": "同目标时段比较基准：未找到可追溯快照；本卡不输出升降结论",
            "previous_version_incomplete": "同目标时段比较基准：快照信息不完整；本卡不输出升降结论",
            "target_valid_time_mismatch": "比较基准与本次目标有效时间不同，不进行升降比较",
            "proxy_method_version_mismatch": "比较基准与本次代理口径版本不同，不进行升降比较",
            "weight_version_mismatch": "比较基准与本次权重版本不同，不进行升降比较",
            "current_comparison_provenance_incomplete": "当前版本溯源信息不完整，不进行版本升降比较",
            "previous_comparison_provenance_incomplete": "比较基准溯源信息不完整，不进行版本升降比较",
            "market_comparison_metadata_incomplete": "分析区有效时间或方法版本不完整，不进行版本升降比较",
            "current_methodology_metadata_conflict": "当前分析区方法版本与运行元数据冲突，不进行版本升降比较",
            "previous_methodology_metadata_conflict": "比较基准的分析区方法版本与运行元数据冲突，不进行版本升降比较",
        }.get(
            reason,
            "同目标时段比较基准不可用；本卡不输出升降结论",
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


def _should_show_run_quality_details(
    run_metadata: dict[str, Any],
    coverage: dict[str, Any],
    *,
    expanded: bool,
) -> bool:
    """Technical provenance belongs only to the explicit detail card."""
    return expanded


def _briefing_source_line(
    rows: list[dict[str, Any]],
    run_metadata: dict[str, Any],
) -> str:
    providers = [
        _provider_label(str(provider))
        for provider in run_metadata.get("sources") or []
        if str(provider).strip()
    ]
    source_text = "、".join(dict.fromkeys(providers)) or _source_summary(rows).replace(
        " / ",
        "、",
    )
    retrieved = _parse_aware_timestamp(run_metadata.get("retrieved_at"))
    update_text = retrieved.strftime("%H:%M") if retrieved is not None else "未记录"
    return f"数据来源：{source_text}｜更新时间：{update_text}"


def _plain_weather_message(value: Any) -> str:
    text = str(value or "").strip()
    if "负荷天气压力代理" in text:
        if any(word in text for word in ("下调", "减弱", "缓解")):
            return "高温或严寒影响有所缓解，用电天气压力减轻"
        if "轻微" in text:
            return "高温或严寒影响轻微增加"
        return "高温或严寒可能增加用电需求，需结合负荷预测观察"
    if "光资源代理" in text or "光资源天气条件" in text:
        if any(word in text for word in ("改善", "增强")):
            return "云量减少，光伏发电天气条件有所改善"
        return "云量或降雨增加，光伏发电天气条件转弱"
    if "地面风" in text or "10米风" in text:
        return "地面风速明显变化，新能源预测波动可能增加"
    if "风雨复合" in text:
        return "风雨同时增强，需关注新能源预测和电网运行变化"
    return text or "暂未形成可展示的天气判断"


def _plain_reliability(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("较高") or text.startswith("高"):
        return "较可靠"
    if text.startswith("中等"):
        return "可参考"
    return "继续观察"


def _compact_trading_windows_text(
    rows: list[dict[str, Any]],
    start_date: str,
) -> str:
    windows = (
        ("早峰", 8, 10),
        ("午间光伏", 11, 15),
        ("晚峰", 17, 21),
    )
    lines: list[str] = []
    for label, start_hour, end_hour in windows:
        status = _window_status(
            rows,
            target_date=start_date,
            start_hour=start_hour,
            end_hour=end_hour,
        )
        if "数据不足" in status:
            summary = "部分地区数据不足，本次不强行判断"
        elif match := re.search(r"(\d+)个分析区需要重点跟踪", status):
            summary = f"{match.group(1)}个地区出现需关注变化，详见上方重点"
        else:
            summary = "暂未发现需要特别提醒的变化"
        lines.append(f"- {label} {start_hour:02d}:00–{end_hour:02d}:00：{summary}")
    return "\n".join(lines)


def _compact_other_regions_text(coverage: dict[str, Any]) -> str:
    areas = coverage.get("provincial_areas") or {}
    total = int(areas.get("total") or 0)
    covered = int(areas.get("covered") or 0)
    missing = max(0, total - covered)
    if missing:
        base = f"{total}个地区中{covered}个已更新，{missing}个最新数据暂缺；缺失地区不作判断。"
    else:
        base = "其余已覆盖地区暂未发现需要特别提醒的气象变化。"
    if int((coverage.get("markets") or {}).get("partial") or 0) > 0:
        base += "部分地区仅有部分代表城市数据，不外推为全区结论。"
    return base


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
    window_version_change: dict[str, Any] | None = None,
    verified_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(SHANGHAI_TZ)
    cutoff = _briefing_cutoff(start_date, generated, run_metadata)
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
    if run_metadata is not None:
        conclusion = _focus_conclusion(
            rows,
            start_date,
            cutoff=cutoff,
            market_config=market_config,
        )
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
        f"来源 {_source_summary(rows)}"
    )
    if run_metadata is not None:
        health = _briefing_source_line(rows, run_metadata)
    other_lines: list[str] = []
    if remaining_risks and not expanded:
        other_lines.append(f"另有 {len(remaining_risks)} 个较低优先级风险分析区未在卡片展开")
    if stable:
        if not expanded:
            other_lines.append(f"稳定分析区 {len(stable)} 个，精简卡未逐一列出")
    if coverage["markets"]["partial"]:
        other_lines.append(
            f"代表城市数据不齐的分析区 {coverage['markets']['partial']} 个；"
            "只按已有城市展示，不外推整个分析区"
        )
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

    if run_metadata is not None and not expanded:
        compact_elements: list[dict[str, Any]] = [
            {"tag": "note", "elements": [{"tag": "plain_text", "content": health}]},
        ]
        if typhoon_block:
            compact_elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": typhoon_block}}
            )
        compact_elements.extend(
            [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**今日一句话**\n{conclusion}",
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**今天重点看**\n"
                        + _top_focus_text(
                            rows,
                            start_date,
                            cutoff=cutoff,
                            market_config=market_config,
                        ),
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**全天时段**\n"
                        + _compact_trading_windows_text(rows, start_date),
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**今日预测变化**\n"
                            + _today_revision_text(
                                window_version_change,
                                start_date,
                                release_slot=str(
                                    run_metadata.get("release_slot") or "09:00"
                                ),
                            )
                        ),
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**明日提前关注**\n"
                        + _tomorrow_first_observation_text(
                            window_version_change,
                            tomorrow_date,
                        ),
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**其他地区**\n" + _compact_other_regions_text(coverage),
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": (
                                "当前为代表城市气象扫描，不代表全省实况。"
                                "未接入负荷、出力、机组、联络线及价格数据，"
                                "不构成交易或报价建议。"
                            ),
                        }
                    ],
                },
            ]
        )
        display_date = start_date[5:].replace("-", "/")
        release_slot = str(run_metadata.get("release_slot") or "09:00")
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": template,
                    "title": {
                        "tag": "plain_text",
                        "content": f"⚡ 电力气象晨报｜{display_date} {release_slot}",
                    },
                },
                "elements": compact_elements,
            },
        }

    elements: list[dict[str, Any]] = [
        {"tag": "note", "elements": [{"tag": "plain_text", "content": health}]},
    ]
    if run_metadata is not None and _should_show_run_quality_details(
        run_metadata,
        coverage,
        expanded=expanded,
    ):
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
                    "content": "**今天先看哪三件事**\n"
                    + _top_focus_text(
                        rows,
                        start_date,
                        cutoff=cutoff,
                        market_config=market_config,
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**今日预测修正**\n"
                    + _today_revision_text(
                        window_version_change,
                        start_date,
                        release_slot=str((run_metadata or {}).get("release_slot") or "09:00"),
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**明日首次观察**\n"
                    + _tomorrow_first_observation_text(
                        window_version_change,
                        tomorrow_date,
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**时间窗口检查**\n"
                    + _window_check_text(
                        rows,
                        start_date,
                        cutoff=cutoff,
                        market_config=market_config,
                        verified_observations=verified_observations,
                    ),
                },
            },
            {"tag": "hr"},
        ]
    )
    if run_metadata is None or expanded:
        elements.extend(
            [
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
                    "text": {
                        "tag": "lark_md",
                        "content": "**资源代理排行**\n" + "\n".join(_ranking_lines(insights)),
                    },
                },
            ]
        )
    elif run_metadata is not None:
        elements.extend(
            [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "**放心清单**\n" + _reassurance_text()},
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**结论有效期**\n" + _validity_text(run_metadata),
                    },
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
                            "当前为多城市代表点扫描，不等于全省实况、调度口径或交易市场结论；"
                            "资源排行按同类代表点等权汇总，单点覆盖分析区不参与全区排行；"
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
                        (
                            f"⚡ 电力气象交易晨报｜{start_date[5:].replace('-', '/')} "
                            f"{str((run_metadata or {}).get('release_slot') or generated.strftime('%H:%M'))}"
                            if run_metadata is not None
                            else (
                                f"⚡ 电力气象决策晨报 2.0｜"
                                f"{start_date[5:].replace('-', '/')}–"
                                f"{tomorrow_date[5:].replace('-', '/')}"
                            )
                        )
                        + ("·全部明细" if expanded else "")
                    ),
                },
            },
            "elements": elements,
        },
    }


def briefing_cache_key(start_date: str, *, release_slot: str | None = None) -> str:
    base = f"{start_date}:{MARKET_CONFIG_VERSION}:{POWER_BRIEFING_REPORT_VERSION}"
    declared_slot = str(release_slot or "09:00").strip()
    if declared_slot == "09:00":
        return base
    if not re.fullmatch(r"\d{2}:\d{2}", declared_slot):
        raise ValueError("release_slot must use HH:MM")
    return f"{base}:release-{declared_slot.replace(':', '')}"


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
    comparison_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(SHANGHAI_TZ)
    run_id = forecast_run_id or f"briefing-{uuid4().hex}"
    # Callers that need a non-09:00 release must declare it explicitly so the
    # snapshot identity always matches the cache lease/storage key.
    declared_release_slot = release_slot or "09:00"
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
    previous = comparison_snapshot
    if previous is None:
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
    cutoff = _briefing_cutoff(start_date, generated, run_metadata)
    window_assessments = _window_assessment_snapshots(
        rows,
        start_date,
        cutoff=cutoff,
        market_config=NATIONAL_MARKETS,
    )
    window_version_change = compare_window_assessment_versions(
        window_assessments,
        previous,
        current_run_metadata=run_metadata,
    )
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
                    await typhoon_client.active_storms(),
                    market_ids={market.market_id for market in NATIONAL_MARKETS},
                )
            except Exception:  # noqa: BLE001 - fail closed without leaking provider details
                typhoon_block = (
                    "**🌀 台风实时数据不可用**\n"
                    "已许可来源本次未返回可用数据，本期不展示台风事实；这不代表无活跃台风。"
                )

    cache_key = briefing_cache_key(start_date, release_slot=declared_release_slot)
    expires = generated + timedelta(seconds=cache.ttl_seconds)
    snapshot: dict[str, Any] = {
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
        "window_assessment_snapshots": window_assessments,
        "window_version_change": window_version_change,
        "summary_card": build_briefing_card(
            rows,
            start_date,
            typhoon_block=typhoon_block,
            generated_at=generated,
            market_config=NATIONAL_MARKETS,
            run_metadata=run_metadata,
            version_change=version_change,
            window_version_change=window_version_change,
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
            window_version_change=window_version_change,
        ),
    }
    if declared_release_slot == "15:00":
        delta_card = build_afternoon_delta_card(snapshot)
        snapshot["afternoon_delta_card"] = delta_card
        snapshot["afternoon_send_required"] = delta_card is not None
    return snapshot


async def get_or_generate_briefing(
    service: Any,
    typhoon_client: TyphoonClient | None,
    start_date: str,
    *,
    cache: BriefingCache,
    wait_seconds: float = 600.0,
    release_slot: str | None = None,
    comparison_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    cache_key = briefing_cache_key(start_date, release_slot=release_slot)
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
        generate_kwargs: dict[str, Any] = {
            "cache": cache,
            "release_slot": release_slot,
        }
        if comparison_snapshot is not None:
            generate_kwargs["comparison_snapshot"] = comparison_snapshot
        snapshot = await generate_briefing_snapshot(
            service,
            typhoon_client,
            start_date,
            **generate_kwargs,
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
