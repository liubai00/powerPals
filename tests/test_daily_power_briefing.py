from datetime import datetime

from scripts.daily_power_briefing import _confidence_label, _continuous_windows, build_briefing_card
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ProviderForecast,
    WeatherSubmission,
)


def _points(
    target_date: str,
    *,
    base_temperature: float = 25.0,
    hot_hours: set[int] | None = None,
    cloudy_hours: set[int] | None = None,
    wind_hours: set[int] | None = None,
    offset: float = 0.0,
) -> list[ForecastPoint]:
    hot_hours = hot_hours or set()
    cloudy_hours = cloudy_hours or set()
    wind_hours = wind_hours or set()
    return [
        ForecastPoint(
            time=f"{target_date}T{hour:02d}:00:00+08:00",
            temperature=(37.0 if hour in hot_hours else base_temperature) + offset,
            apparent_temperature=(39.0 if hour in hot_hours else base_temperature + 1) + offset,
            precipitation_probability=75.0 if hour in cloudy_hours else 10.0,
            cloud_cover=92.0 if hour in cloudy_hours else 25.0,
            wind_speed=11.0 if hour in wind_hours else 4.0,
        )
        for hour in range(24)
    ]


def _submission(
    target_date: str,
    *,
    hot_hours: set[int] | None = None,
    cloudy_hours: set[int] | None = None,
    wind_hours: set[int] | None = None,
    provider_offset: float = 0.5,
) -> WeatherSubmission:
    aggregated = _points(
        target_date,
        hot_hours=hot_hours,
        cloudy_hours=cloudy_hours,
        wind_hours=wind_hours,
    )
    provider_a = _points(
        target_date,
        hot_hours=hot_hours,
        cloudy_hours=cloudy_hours,
        wind_hours=wind_hours,
    )
    provider_b = _points(
        target_date,
        hot_hours=hot_hours,
        cloudy_hours=cloudy_hours,
        wind_hours=wind_hours,
        offset=provider_offset,
    )
    return WeatherSubmission(
        task_id=f"WEATHER-CN-TEST-{target_date.replace('-', '')}-DAYAHEAD-001",
        region="测试城市",
        target_date=target_date,
        data_cutoff_time=f"{target_date}T09:00:00+08:00",
        provider_results=[
            ProviderForecast(provider="open_meteo", status="ok", points=provider_a),
            ProviderForecast(provider="qweather", status="ok", points=provider_b),
        ],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo", "qweather"],
            points=aggregated,
            summary=ForecastSummary(
                max_temperature=max(point.temperature for point in aggregated),
                min_temperature=min(point.temperature for point in aggregated),
                rain_probability=max(point.precipitation_probability for point in aggregated),
                wind_speed=max(point.wind_speed for point in aggregated),
                cloud_cover=sum(point.cloud_cover for point in aggregated) / len(aggregated),
                main_weather="多云",
                high_risk_period="无明显高风险时段",
                sunrise="06:00",
                sunset="19:00",
            ),
        ),
        confidence={"score": 0.7, "description": "中等"},
        key_factors=[],
        risk_notes=[],
    )


def _card_text(card: dict) -> str:
    chunks = [card["card"]["header"]["title"]["content"]]
    for element in card["card"]["elements"]:
        if isinstance(element.get("text"), dict):
            chunks.append(element["text"].get("content", ""))
        for item in element.get("elements", []):
            if isinstance(item, dict):
                chunks.append(item.get("content", ""))
    return "\n".join(chunks)


def test_continuous_windows_reports_ranges_instead_of_first_hit():
    points = _points("2026-07-28", hot_hours={17, 18, 19}, cloudy_hours={11, 12, 13})

    result = _continuous_windows(
        points,
        lambda point: point.temperature >= 35 or point.cloud_cover >= 85,
    )

    assert result == "11:00–14:00、17:00–20:00"


def test_briefing_2_0_prioritizes_risk_change_confidence_and_proxy_boundaries():
    rows = [
        {
            "market": "山东",
            "city": "济南",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    hot_hours={17, 18, 19, 20},
                    cloudy_hours={11, 12, 13, 14, 15},
                    wind_hours={12, 13},
                ),
            },
        },
        {
            "market": "山西",
            "city": "太原",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        },
    ]

    card = build_briefing_card(
        rows,
        "2026-07-27",
        generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
    )
    text = _card_text(card)

    assert "电力气象决策晨报 2.0" in text
    assert "Top 5 气象侧风险" in text
    assert "山东·济南代表点" in text
    assert "负荷压力↑" in text
    assert "光伏资源代理↓" in text
    assert "地面风资源代理↑" in text
    assert "变化：负荷天气压力上调" in text
    assert "置信度：中等" in text
    assert "稳定市场 1 个，已折叠" in text
    assert "当前为城市代表点" in text
    for unsupported_claim in ("强对流", "现货承压", "电价", "风电出力"):
        assert unsupported_claim not in text


def test_confidence_uses_provider_disagreement_not_only_provider_count():
    submission = _submission("2026-07-28", provider_offset=8.0)

    assert _confidence_label(submission) == "偏低（数据源分歧较大）"
