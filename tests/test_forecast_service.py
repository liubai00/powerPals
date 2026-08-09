from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService


class FakeProvider:
    def __init__(self, name: str, point: ForecastPoint, source_metadata: dict[str, str]):
        self.name = name
        self._point = point
        self._source_metadata = source_metadata
        self.source_endpoints = (source_metadata["source_url"],)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=[
                self._point.model_copy(
                    update={"time": f"{request.target_date}T{hour:02d}:00:00+08:00"}
                )
                for hour in range(24)
            ],
            **self._source_metadata,
        )


async def test_forecast_service_builds_standard_weather_submission(
    external_source_metadata,
    verified_test_source_registry,
    test_source_clock,
):
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
                external_source_metadata("open_meteo"),
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
                external_source_metadata("qweather"),
            ),
        },
        settings=Settings(_env_file=None, app_env="test"),
        source_registry=verified_test_source_registry(
            {
                "open_meteo": "https://open_meteo.weather.test/v1/forecast",
                "qweather": "https://qweather.weather.test/v1/forecast",
            }
        ),
        clock=test_source_clock,
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-06-10",
            granularity="1h",
            providers=["open_meteo", "qweather"],
        )
    )

    assert result.task_id == "WEATHER-CN-440300-20260610-DAYAHEAD-001"
    assert result.region == "广东省深圳市"
    assert result.target_date == "2026-06-10"
    assert result.provider_results[0].provider == "open_meteo"
    assert result.aggregated_forecast.providers_used == ["open_meteo", "qweather"]
    assert result.disclaimer
    assert "交易建议" in result.disclaimer
    assert result.key_factors
    assert result.risk_notes
