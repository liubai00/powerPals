from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast


SHENZHEN_LATITUDE = 22.5431
SHENZHEN_LONGITUDE = 114.0579


class OpenMeteoProvider:
    name = "open_meteo"

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        target = date.fromisoformat(request.target_date)
        latitude, longitude = _coordinates(request)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,cloud_cover",
            "timezone": "Asia/Shanghai",
            "start_date": target.isoformat(),
            "end_date": target.isoformat(),
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            response.raise_for_status()
            body = response.json()
        return ProviderForecast(provider=self.name, status="ok", points=_open_meteo_points(body), raw=body)


class QWeatherProvider:
    name = "qweather"

    def __init__(self, api_key: str | None):
        self.api_key = api_key

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        if not self.api_key:
            return ProviderForecast(provider=self.name, status="disabled", points=[], error_message="Missing API key")

        latitude, longitude = _coordinates(request)
        params = {"location": f"{longitude},{latitude}", "key": self.api_key}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get("https://devapi.qweather.com/v7/weather/24h", params=params)
            response.raise_for_status()
            body = response.json()
        return ProviderForecast(provider=self.name, status="ok", points=_qweather_points(body), raw=body)


class CaiyunProvider:
    name = "caiyun"

    def __init__(self, api_key: str | None):
        self.api_key = api_key

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        if not self.api_key:
            return ProviderForecast(provider=self.name, status="disabled", points=[], error_message="Missing API key")

        target = date.fromisoformat(request.target_date)
        latitude, longitude = _coordinates(request)
        url = (
            f"https://api.caiyunapp.com/v2.6/{self.api_key}/"
            f"{longitude},{latitude}/hourly"
        )
        params = {"hourlysteps": 24}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            body = response.json()
        points = [point for point in _caiyun_points(body) if point.time.startswith(target.isoformat())]
        return ProviderForecast(provider=self.name, status="ok", points=points, raw=body)


def build_default_providers(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or Settings()
    return {
        "open_meteo": OpenMeteoProvider(),
        "qweather": QWeatherProvider(settings.qweather_api_key),
        "caiyun": CaiyunProvider(settings.caiyun_api_key),
    }


def _coordinates(request: ForecastRequest) -> tuple[float, float]:
    return (
        request.latitude if request.latitude is not None else SHENZHEN_LATITUDE,
        request.longitude if request.longitude is not None else SHENZHEN_LONGITUDE,
    )


def _open_meteo_points(body: dict[str, Any]) -> list[ForecastPoint]:
    hourly = body.get("hourly", {})
    times = hourly.get("time", [])
    return [
        ForecastPoint(
            time=_with_timezone(timestamp),
            temperature=_value(hourly, "temperature_2m", index),
            precipitation_probability=_value(hourly, "precipitation_probability", index),
            wind_speed=_value(hourly, "wind_speed_10m", index),
            cloud_cover=_value(hourly, "cloud_cover", index),
        )
        for index, timestamp in enumerate(times)
    ]


def _qweather_points(body: dict[str, Any]) -> list[ForecastPoint]:
    points = []
    for item in body.get("hourly", []):
        wind_kph = _to_float(item.get("windSpeed"))
        points.append(
            ForecastPoint(
                time=item.get("fxTime"),
                temperature=_to_float(item.get("temp")),
                precipitation_probability=_to_float(item.get("pop")),
                wind_speed=round(wind_kph / 3.6, 2) if wind_kph is not None else None,
                cloud_cover=_to_float(item.get("cloud")),
            )
        )
    return points


def _caiyun_points(body: dict[str, Any]) -> list[ForecastPoint]:
    hourly = body.get("result", {}).get("hourly", {})
    temperature = hourly.get("temperature", [])
    precipitation = hourly.get("precipitation", [])
    wind = hourly.get("wind", [])
    cloudrate = hourly.get("cloudrate", [])
    count = max(len(temperature), len(precipitation), len(wind), len(cloudrate))
    points = []
    for index in range(count):
        timestamp = _caiyun_datetime(temperature, precipitation, wind, cloudrate, index)
        points.append(
            ForecastPoint(
                time=timestamp,
                temperature=_series_value(temperature, index),
                precipitation_probability=_caiyun_precip_probability(precipitation, index),
                wind_speed=_caiyun_wind_speed(wind, index),
                cloud_cover=_caiyun_cloud_cover(cloudrate, index),
            )
        )
    return points


def _value(hourly: dict[str, list[Any]], key: str, index: int) -> float | None:
    values = hourly.get(key) or []
    if index >= len(values):
        return None
    return _to_float(values[index])


def _series_value(series: list[dict[str, Any]], index: int) -> float | None:
    if index >= len(series):
        return None
    return _to_float(series[index].get("value"))


def _caiyun_precip_probability(series: list[dict[str, Any]], index: int) -> float | None:
    if index >= len(series):
        return None
    probability = series[index].get("probability")
    if probability is None:
        return None
    value = _to_float(probability)
    return round(value * 100, 2) if value is not None and value <= 1 else value


def _caiyun_wind_speed(series: list[dict[str, Any]], index: int) -> float | None:
    if index >= len(series):
        return None
    return _to_float(series[index].get("speed"))


def _caiyun_cloud_cover(series: list[dict[str, Any]], index: int) -> float | None:
    if index >= len(series):
        return None
    value = _to_float(series[index].get("value"))
    return round(value * 100, 2) if value is not None and value <= 1 else value


def _caiyun_datetime(*series: list[dict[str, Any]], index: int) -> str:
    for items in series:
        if index < len(items) and items[index].get("datetime"):
            return _with_timezone(items[index]["datetime"])
    fallback = datetime.now() + timedelta(hours=index)
    return _with_timezone(fallback.strftime("%Y-%m-%dT%H:%M"))


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _with_timezone(timestamp: str) -> str:
    if timestamp.endswith("+08:00"):
        return timestamp
    if "T" not in timestamp and " " in timestamp:
        timestamp = timestamp.replace(" ", "T")
    return f"{timestamp}:00+08:00" if len(timestamp) == 16 else f"{timestamp}+08:00"
