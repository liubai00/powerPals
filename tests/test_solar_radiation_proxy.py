from __future__ import annotations

import httpx
import pytest

from services.weather_bot.aggregation import aggregate_provider_forecasts
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastRequest,
    ForecastSummary,
    ProviderForecast,
    WeatherSubmission,
)
from services.weather_bot.power_briefing import build_briefing_card
from services.weather_bot.providers import OpenMeteoProvider


@pytest.mark.asyncio
async def test_open_meteo_fetch_exposes_traceable_shortwave_radiation(monkeypatch):
    observed_hourly_fields: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        observed_hourly_fields.update(request.url.params["hourly"].split(","))
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-08-10T10:00"],
                    "temperature_2m": [30.0],
                    "precipitation_probability": [10.0],
                    "wind_speed_10m": [3.0],
                    "cloud_cover": [20.0],
                    "apparent_temperature": [32.0],
                    "wind_direction_10m": [180.0],
                    "uv_index": [6.0],
                    "shortwave_radiation": [612.5],
                },
                "hourly_units": {"shortwave_radiation": "W/m²"},
                "daily": {
                    "sunrise": ["2026-08-10T05:20"],
                    "sunset": ["2026-08-10T18:45"],
                },
            },
        )

    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("services.weather_bot.providers.httpx.AsyncClient", client_factory)

    result = await OpenMeteoProvider().fetch(
        ForecastRequest(
            region="浙江省杭州市",
            latitude=30.2741,
            longitude=120.1551,
            target_date="2026-08-10",
        )
    )

    assert "shortwave_radiation" in observed_hourly_fields
    assert result.points[0].shortwave_radiation == 612.5
    assert result.source_url == "https://api.open-meteo.com/v1/forecast"


def test_aggregation_preserves_shortwave_radiation_at_the_public_forecast_seam():
    result = aggregate_provider_forecasts(
        [
            ProviderForecast(
                provider="open_meteo",
                points=[
                    ForecastPoint(
                        time="2026-08-10T10:00:00+08:00",
                        shortwave_radiation=480.0,
                    )
                ],
            ),
            ProviderForecast(
                provider="qweather",
                points=[
                    ForecastPoint(
                        time="2026-08-10T10:00:00+08:00",
                        cloud_cover=80.0,
                    )
                ],
            ),
        ]
    )

    assert result.points[0].shortwave_radiation == 480.0


def _submission(
    target_date: str,
    *,
    radiation: float | None,
    cloud_cover: float,
    rain_probability: float,
) -> WeatherSubmission:
    points = [
        ForecastPoint(
            time=f"{target_date}T{hour:02d}:00:00+08:00",
            temperature=25.0,
            apparent_temperature=25.0,
            precipitation_probability=rain_probability,
            cloud_cover=cloud_cover,
            wind_speed=5.0,
            shortwave_radiation=radiation if 6 <= hour < 19 else 0.0,
        )
        for hour in range(24)
    ]
    return WeatherSubmission(
        task_id=f"SOLAR-{target_date}",
        region="浙江省杭州市",
        target_date=target_date,
        data_cutoff_time=f"{target_date}T09:00:00+08:00",
        provider_results=[
            ProviderForecast(provider="open_meteo", status="ok", points=points)
        ],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo"],
            points=points,
            summary=ForecastSummary(
                main_weather="晴",
                high_risk_period="无",
                sunrise="06:00",
                sunset="19:00",
            ),
        ),
        confidence={"score": 0.6},
        key_factors=[],
        risk_notes=[],
    )


def _card_text(card: dict) -> str:
    chunks = [card["card"]["header"]["title"]["content"]]
    for element in card["card"]["elements"]:
        text = element.get("text")
        if isinstance(text, dict):
            chunks.append(text.get("content", ""))
        for item in element.get("elements", []):
            if isinstance(item, dict):
                chunks.append(item.get("content", ""))
    return "\n".join(chunks)


def test_briefing_prefers_shortwave_radiation_over_cloud_and_rain_proxy():
    rows = [
        {
            "market_id": "zhejiang",
            "market": "浙江",
            "province": "浙江",
            "point_id": "hangzhou",
            "city": "杭州",
            "roles": ["solar"],
            "submissions": {
                "2026-08-09": _submission(
                    "2026-08-09",
                    radiation=500.0,
                    cloud_cover=95.0,
                    rain_probability=90.0,
                ),
                "2026-08-10": _submission(
                    "2026-08-10",
                    radiation=50.0,
                    cloud_cover=5.0,
                    rain_probability=0.0,
                ),
            },
        }
    ]

    text = _card_text(build_briefing_card(rows, "2026-08-09"))

    assert "光伏资源代理↓" in text
    assert "短波辐射积分 650 Wh/m²" in text
    assert "非实际光伏出力" in text
    assert "云量 5%" not in text


def test_briefing_labels_cloud_and_rain_as_a_degraded_proxy_when_radiation_is_missing():
    rows = [
        {
            "market_id": "zhejiang",
            "market": "浙江",
            "province": "浙江",
            "point_id": "hangzhou",
            "city": "杭州",
            "roles": ["solar"],
            "submissions": {
                "2026-08-09": _submission(
                    "2026-08-09",
                    radiation=None,
                    cloud_cover=20.0,
                    rain_probability=10.0,
                ),
                "2026-08-10": _submission(
                    "2026-08-10",
                    radiation=None,
                    cloud_cover=90.0,
                    rain_probability=80.0,
                ),
            },
        }
    ]

    text = _card_text(build_briefing_card(rows, "2026-08-09"))

    assert "光伏资源代理↓" in text
    assert "短波辐射缺失" in text
    assert "降级为云量+降水代理" in text
    assert "非实际光伏出力" in text


def test_briefing_does_not_compare_solar_change_across_different_proxy_methods():
    rows = [
        {
            "market_id": "zhejiang",
            "market": "浙江",
            "province": "浙江",
            "point_id": "hangzhou",
            "city": "杭州",
            "roles": ["solar"],
            "submissions": {
                "2026-08-09": _submission(
                    "2026-08-09",
                    radiation=500.0,
                    cloud_cover=20.0,
                    rain_probability=10.0,
                ),
                "2026-08-10": _submission(
                    "2026-08-10",
                    radiation=None,
                    cloud_cover=90.0,
                    rain_probability=80.0,
                ),
            },
        }
    ]

    text = _card_text(build_briefing_card(rows, "2026-08-09"))

    assert "代理口径变化，不作同比" in text
