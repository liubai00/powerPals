from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.source_registry import same_source_endpoint


SHENZHEN_LATITUDE = 22.5431
SHENZHEN_LONGITUDE = 114.0579
_PATH_CREDENTIAL_LOG_VALUES: ContextVar[tuple[str, ...]] = ContextVar(
    "weather_path_credential_log_values",
    default=(),
)


class _PathCredentialLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _PATH_CREDENTIAL_LOG_VALUES.get()
        if not secrets:
            return True
        record.msg = _redact_log_value(record.msg, secrets)
        record.args = _redact_log_value(record.args, secrets)
        return True


_PATH_CREDENTIAL_LOG_FILTER = _PathCredentialLogFilter()
for _logger_name in (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "urllib3",
    "urllib3.connectionpool",
):
    _logger = logging.getLogger(_logger_name)
    if not any(
        isinstance(item, _PathCredentialLogFilter)
        for item in _logger.filters
    ):
        _logger.addFilter(_PATH_CREDENTIAL_LOG_FILTER)


def _redact_log_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED_CREDENTIAL]")
        return value
    if isinstance(value, bytes):
        for secret in secrets:
            value = value.replace(
                secret.encode("utf-8"),
                b"[REDACTED_CREDENTIAL]",
            )
        return value
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item, secrets) for item in value)
    if isinstance(value, list):
        return [_redact_log_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            _redact_log_value(key, secrets): _redact_log_value(item, secrets)
            for key, item in value.items()
        }
    rendered = str(value)
    if any(secret in rendered for secret in secrets):
        return _redact_log_value(rendered, secrets)
    return value


@asynccontextmanager
async def _redact_path_credential(secret: str):
    values = tuple(
        dict.fromkeys(
            value
            for value in (secret, quote(secret, safe=""))
            if value
        )
    )
    token = _PATH_CREDENTIAL_LOG_VALUES.set(
        (*_PATH_CREDENTIAL_LOG_VALUES.get(), *values)
    )
    try:
        yield
    finally:
        _PATH_CREDENTIAL_LOG_VALUES.reset(token)


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    attempts: int = 2,
    backoff: float = 0.5,
    **kwargs: Any,
) -> httpx.Response:
    """数据源抓取: 瞬时网络/DNS/超时错误(httpx.TransportError)自动重试。"""
    last_exc: Exception | None = None
    for index in range(attempts):
        try:
            return await client.get(url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if index < attempts - 1:
                await asyncio.sleep(backoff * (index + 1))
    assert last_exc is not None
    raise last_exc


class OpenMeteoProvider:
    name = "open_meteo"
    source_endpoints = ("https://api.open-meteo.com/v1/forecast",)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        target = date.fromisoformat(request.target_date)
        latitude, longitude = _coordinates(request)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,cloud_cover,apparent_temperature,wind_direction_10m,uv_index,shortwave_radiation",
            "daily": "sunrise,sunset",
            "wind_speed_unit": "ms",
            "timezone": "Asia/Shanghai",
            "start_date": target.isoformat(),
            "end_date": target.isoformat(),
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await _get_with_retry(client, "https://api.open-meteo.com/v1/forecast", params=params)
            response.raise_for_status()
            _assert_response_endpoint(
                response,
                "https://api.open-meteo.com/v1/forecast",
            )
            body = response.json()
        _daily = body.get("daily", {})
        _daily = _daily if isinstance(_daily, dict) else {}
        daily_values = {"sunrise": (_daily.get("sunrise") or [None])[0], "sunset": (_daily.get("sunset") or [None])[0]}
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=_open_meteo_points(body),
            source_url="https://api.open-meteo.com/v1/forecast",
            content_sha256=_content_sha256(body),
            daily=daily_values,
        )


class QWeatherProvider:
    name = "qweather"

    def __init__(
        self,
        api_key: str | None,
        api_host: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.api_key = api_key
        self.api_host = (api_host or "devapi.qweather.com").strip().rstrip("/")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def source_endpoints(self) -> tuple[str, ...]:
        return (f"https://{self.api_host}/v7/weather/168h",)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        if not self.api_key:
            return ProviderForecast(provider=self.name, status="disabled", points=[], error_message="Missing API key")

        latitude, longitude = _coordinates(request)
        params = {"location": f"{longitude},{latitude}"}
        async with httpx.AsyncClient(timeout=8.0) as client:
            # 168h(7天逐时)而非 24h: 按 target_date 过滤对应日, 避免多日/远期请求被"当前24h"数据污染;
            # 目标日超出和风覆盖窗时过滤为空, 由 open_meteo 承接, 不用近端数据冒充
            response = await _get_with_retry(client,
                f"https://{self.api_host}/v7/weather/168h",
                params=params,
                headers={"X-QW-Api-Key": self.api_key or ""},
            )
            response.raise_for_status()
            _assert_response_endpoint(
                response,
                f"https://{self.api_host}/v7/weather/168h",
            )
            body = response.json()
        retrieved_at = _observed_time(self.clock())
        points = [point for point in _qweather_points(body) if str(point.time).startswith(request.target_date)]
        provider_issued_at = body.get("updateTime")
        if not isinstance(provider_issued_at, str) or not provider_issued_at.strip():
            provider_issued_at = None
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=points,
            retrieved_at=retrieved_at,
            provider_issued_at=provider_issued_at,
            source_url=f"https://{self.api_host}/v7/weather/168h",
            content_sha256=_content_sha256(body),
        )


class CaiyunProvider:
    name = "caiyun"
    source_endpoints = (
        "https://api.caiyunapp.com/v2.6/{credential}/{location}/hourly",
    )

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
        try:
            async with _redact_path_credential(self.api_key):
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await _get_with_retry(client, url, params=params)
                    response.raise_for_status()
                    _assert_response_endpoint(response, url)
                    body = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"{type(exc).__name__}: Caiyun provider request failed"
            ) from None
        points = [point for point in _caiyun_points(body) if point.time.startswith(target.isoformat())]
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=points,
            source_url="https://api.caiyunapp.com/v2.6/{credential}/{location}/hourly",
            content_sha256=_content_sha256(body),
        )


def build_default_providers(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or Settings()
    return {
        "open_meteo": OpenMeteoProvider(),
        "qweather": QWeatherProvider(settings.qweather_api_key, settings.qweather_api_host),
        "caiyun": CaiyunProvider(settings.caiyun_api_key),
    }


def _coordinates(request: ForecastRequest) -> tuple[float, float]:
    return (
        request.latitude if request.latitude is not None else SHENZHEN_LATITUDE,
        request.longitude if request.longitude is not None else SHENZHEN_LONGITUDE,
    )


def _content_sha256(body: dict[str, Any]) -> str:
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            apparent_temperature=_value(hourly, "apparent_temperature", index),
            wind_direction=_value(hourly, "wind_direction_10m", index),
            uv_index=_value(hourly, "uv_index", index),
            shortwave_radiation=_value(hourly, "shortwave_radiation", index),
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
        timestamp = _caiyun_datetime(
            temperature,
            precipitation,
            wind,
            cloudrate,
            index=index,
        )
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


def _observed_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Provider clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _assert_response_endpoint(response: httpx.Response, expected_endpoint: str) -> None:
    try:
        actual_endpoint = str(response.request.url.copy_with(query=None))
    except RuntimeError as exc:
        raise ValueError("provider_endpoint_mismatch") from exc
    if response.history or not same_source_endpoint(actual_endpoint, expected_endpoint):
        raise ValueError("provider_endpoint_mismatch")
