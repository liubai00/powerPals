from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService


class FakeProvider:
    def __init__(self, name: str, point: ForecastPoint):
        self.name = name
        self._point = point

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(provider=self.name, status="ok", points=[self._point])


async def test_forecast_service_builds_standard_weather_submission():
    service = ForecastService(
        providers={
            "open_meteo": FakeProvider(
                "open_meteo",
                ForecastPoint(
                    time="2026-06-10T00:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=60.0,
                ),
            ),
            "qweather": FakeProvider(
                "qweather",
                ForecastPoint(
                    time="2026-06-10T00:00:00+08:00",
                    temperature=30.0,
                    precipitation_probability=40.0,
                    wind_speed=4.0,
                    cloud_cover=80.0,
                ),
            ),
        }
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-06-10",
            granularity="1h",
            providers=["open_meteo", "qweather"],
        )
    )

    assert result.task_id == "WEATHER-SZ-20260610-DAYAHEAD-001"
    assert result.region == "广东省深圳市"
    assert result.target_date == "2026-06-10"
    assert result.provider_results[0].provider == "open_meteo"
    assert result.aggregated_forecast.providers_used == ["open_meteo", "qweather"]
    assert result.disclaimer
    assert "交易建议" in result.disclaimer
    assert result.key_factors
    assert result.risk_notes
