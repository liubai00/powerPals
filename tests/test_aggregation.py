from services.weather_bot.aggregation import aggregate_provider_forecasts
from services.weather_bot.models import ForecastPoint, ProviderForecast


def test_aggregates_available_provider_weights_and_renormalizes_missing_sources():
    qweather = ProviderForecast(
        provider="qweather",
        status="ok",
        points=[
            ForecastPoint(
                time="2026-06-10T00:00:00+08:00",
                temperature=30.0,
                precipitation_probability=40.0,
                wind_speed=4.0,
                cloud_cover=80.0,
            )
        ],
    )
    open_meteo = ProviderForecast(
        provider="open_meteo",
        status="ok",
        points=[
            ForecastPoint(
                time="2026-06-10T00:00:00+08:00",
                temperature=28.0,
                precipitation_probability=20.0,
                wind_speed=2.0,
                cloud_cover=60.0,
            )
        ],
    )
    caiyun = ProviderForecast(provider="caiyun", status="disabled", points=[])

    result = aggregate_provider_forecasts([qweather, open_meteo, caiyun])

    point = result.points[0]
    assert result.providers_used == ["qweather", "open_meteo"]
    assert point.temperature == 29.07
    assert point.precipitation_probability == 32.73
    assert point.wind_speed == 3.07
    assert point.cloud_cover == 70.67
    assert result.summary.max_temperature == 29.07
    assert result.summary.min_temperature == 29.07


def test_aggregation_raises_when_no_provider_has_usable_points():
    disabled = ProviderForecast(provider="qweather", status="disabled", points=[])

    try:
        aggregate_provider_forecasts([disabled])
    except ValueError as exc:
        assert "No usable provider forecasts" in str(exc)
    else:
        raise AssertionError("Expected ValueError for empty provider forecasts")
