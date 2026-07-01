from __future__ import annotations

from collections import defaultdict
from statistics import mean

from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ProviderForecast,
)


METRIC_WEIGHTS: dict[str, dict[str, float]] = {
    "temperature": {"qweather": 0.4, "open_meteo": 0.35, "caiyun": 0.25},
    "wind_speed": {"qweather": 0.4, "open_meteo": 0.35, "caiyun": 0.25},
    "cloud_cover": {"qweather": 0.4, "open_meteo": 0.35, "caiyun": 0.25},
    "precipitation_probability": {"caiyun": 0.45, "qweather": 0.35, "open_meteo": 0.2},
    "apparent_temperature": {"open_meteo": 1.0},
    "wind_direction": {"open_meteo": 1.0},
    "uv_index": {"open_meteo": 1.0},
}


def aggregate_provider_forecasts(provider_results: list[ProviderForecast]) -> AggregatedForecast:
    usable = [result for result in provider_results if result.status == "ok" and result.points]
    if not usable:
        raise ValueError("No usable provider forecasts")

    points_by_hour: dict[str, list[tuple[str, ForecastPoint]]] = defaultdict(list)
    time_repr: dict[str, str] = {}
    for result in usable:
        for point in result.points:
            hour_key = str(point.time)[:13]  # 归一化到小时 YYYY-MM-DDTHH, 合并各源不同的时间戳格式(13:00 vs 13:00:00)
            points_by_hour[hour_key].append((result.provider, point))
            time_repr.setdefault(hour_key, point.time)

    aggregated_points: list[ForecastPoint] = []
    for hour_key in sorted(points_by_hour):
        provider_points = points_by_hour[hour_key]
        aggregated_points.append(
            ForecastPoint(
                time=time_repr[hour_key],
                temperature=_weighted_metric(provider_points, "temperature"),
                precipitation_probability=_weighted_metric(provider_points, "precipitation_probability"),
                wind_speed=_weighted_metric(provider_points, "wind_speed"),
                cloud_cover=_weighted_metric(provider_points, "cloud_cover"),
                apparent_temperature=_weighted_metric(provider_points, "apparent_temperature"),
                wind_direction=_weighted_metric(provider_points, "wind_direction"),
                uv_index=_weighted_metric(provider_points, "uv_index"),
            )
        )

    if not aggregated_points:
        raise ValueError("No usable provider forecasts")

    sunrise = sunset = None
    for result in usable:
        daily = result.daily or {}
        if not sunrise and daily.get("sunrise"):
            sunrise = _hhmm(daily.get("sunrise"))
        if not sunset and daily.get("sunset"):
            sunset = _hhmm(daily.get("sunset"))

    return AggregatedForecast(
        providers_used=[result.provider for result in usable],
        points=aggregated_points,
        summary=_build_summary(aggregated_points, sunrise, sunset),
    )


def _hhmm(value: object) -> str | None:
    if not value or not isinstance(value, str):
        return None
    if "T" in value and len(value) >= 16:
        return value[11:16]
    return value


def _weighted_metric(provider_points: list[tuple[str, ForecastPoint]], metric: str) -> float | None:
    weighted_values: list[tuple[float, float]] = []
    weights = METRIC_WEIGHTS[metric]

    for provider, point in provider_points:
        value = getattr(point, metric)
        if value is None:
            continue
        weighted_values.append((float(value), weights.get(provider, 0.0)))

    total_weight = sum(weight for _, weight in weighted_values)
    if total_weight <= 0:
        return None

    return round(sum(value * weight for value, weight in weighted_values) / total_weight, 2)


def _build_summary(points: list[ForecastPoint], sunrise: str | None = None, sunset: str | None = None) -> ForecastSummary:
    temperatures = [point.temperature for point in points if point.temperature is not None]
    rains = [point.precipitation_probability for point in points if point.precipitation_probability is not None]
    winds = [point.wind_speed for point in points if point.wind_speed is not None]
    clouds = [point.cloud_cover for point in points if point.cloud_cover is not None]
    feels = [point.apparent_temperature for point in points if point.apparent_temperature is not None]
    uvs = [point.uv_index for point in points if point.uv_index is not None]
    wind_dir_pairs = [
        (point.wind_speed, point.wind_direction)
        for point in points
        if point.wind_speed is not None and point.wind_direction is not None
    ]

    rain_probability = round(max(rains), 2) if rains else None
    cloud_cover = round(mean(clouds), 2) if clouds else None
    wind_speed = round(max(winds), 2) if winds else None

    return ForecastSummary(
        max_temperature=round(max(temperatures), 2) if temperatures else None,
        min_temperature=round(min(temperatures), 2) if temperatures else None,
        rain_probability=rain_probability,
        wind_speed=wind_speed,
        cloud_cover=cloud_cover,
        main_weather=_describe_weather(rain_probability, cloud_cover),
        high_risk_period=_find_high_risk_period(points),
        feels_like=round(max(feels), 2) if feels else None,
        wind_direction=max(wind_dir_pairs)[1] if wind_dir_pairs else None,
        uv_index=round(max(uvs), 2) if uvs else None,
        sunrise=sunrise,
        sunset=sunset,
    )


def _describe_weather(rain_probability: float | None, cloud_cover: float | None) -> str:
    if rain_probability is not None and rain_probability >= 60:
        return "有明显降水风险"
    if cloud_cover is not None and cloud_cover >= 75:
        return "多云到阴"
    if cloud_cover is not None and cloud_cover >= 45:
        return "多云"
    return "晴到多云"


def _find_high_risk_period(points: list[ForecastPoint]) -> str:
    risky = [
        point.time[11:16]
        for point in points
        if (point.precipitation_probability is not None and point.precipitation_probability >= 50)
        or (point.wind_speed is not None and point.wind_speed >= 10)
    ]
    if not risky:
        return "无明显高风险时段"
    return f"{risky[0]} 起存在局地天气不确定性"
