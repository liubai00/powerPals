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
}


def aggregate_provider_forecasts(provider_results: list[ProviderForecast]) -> AggregatedForecast:
    usable = [result for result in provider_results if result.status == "ok" and result.points]
    if not usable:
        raise ValueError("No usable provider forecasts")

    points_by_time: dict[str, list[tuple[str, ForecastPoint]]] = defaultdict(list)
    for result in usable:
        for point in result.points:
            points_by_time[point.time].append((result.provider, point))

    aggregated_points: list[ForecastPoint] = []
    for timestamp in sorted(points_by_time):
        provider_points = points_by_time[timestamp]
        aggregated_points.append(
            ForecastPoint(
                time=timestamp,
                temperature=_weighted_metric(provider_points, "temperature"),
                precipitation_probability=_weighted_metric(provider_points, "precipitation_probability"),
                wind_speed=_weighted_metric(provider_points, "wind_speed"),
                cloud_cover=_weighted_metric(provider_points, "cloud_cover"),
            )
        )

    if not aggregated_points:
        raise ValueError("No usable provider forecasts")

    return AggregatedForecast(
        providers_used=[result.provider for result in usable],
        points=aggregated_points,
        summary=_build_summary(aggregated_points),
    )


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


def _build_summary(points: list[ForecastPoint]) -> ForecastSummary:
    temperatures = [point.temperature for point in points if point.temperature is not None]
    rains = [point.precipitation_probability for point in points if point.precipitation_probability is not None]
    winds = [point.wind_speed for point in points if point.wind_speed is not None]
    clouds = [point.cloud_cover for point in points if point.cloud_cover is not None]

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
