from services.weather_bot.config import Settings
from services.weather_bot.location import ResolvedLocation
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService


class CapturingProvider:
    name = "open_meteo"

    def __init__(self, source_metadata: dict[str, str]):
        self.seen_request = None
        self.source_metadata = source_metadata
        self.source_endpoints = (source_metadata["source_url"],)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        self.seen_request = request
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=60.0,
                )
                for hour in range(24)
            ],
            **self.source_metadata,
        )


class FakeLocationResolver:
    async def resolve(self, request: ForecastRequest) -> ResolvedLocation:
        return ResolvedLocation(
            name="广东省广州市",
            code="440100",
            latitude=23.1291,
            longitude=113.2644,
            source="test",
        )


async def test_forecast_service_uses_resolved_national_location(
    external_source_metadata,
    verified_test_source_registry,
    test_source_clock,
):
    provider = CapturingProvider(external_source_metadata("open_meteo"))
    service = ForecastService(
        providers={"open_meteo": provider},
        location_resolver=FakeLocationResolver(),
        settings=Settings(_env_file=None, app_env="test"),
        source_registry=verified_test_source_registry(
            {"open_meteo": "https://open_meteo.weather.test/v1/forecast"}
        ),
        clock=test_source_clock,
    )

    result = await service.forecast(
        ForecastRequest(region="广州", target_date="2026-06-10", providers=["open_meteo"])
    )

    assert provider.seen_request.latitude == 23.1291
    assert provider.seen_request.longitude == 113.2644
    assert provider.seen_request.region == "广东省广州市"
    assert provider.seen_request.location_code == "440100"
    assert result.task_id == "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    assert result.region == "广东省广州市"
    assert result.scope.location["latitude"] == 23.1291
