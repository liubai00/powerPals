from services.weather_bot.location import ResolvedLocation
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService


class CapturingProvider:
    name = "open_meteo"

    def __init__(self):
        self.seen_request = None

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        self.seen_request = request
        return ProviderForecast(
            provider=self.name,
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


class FakeLocationResolver:
    async def resolve(self, request: ForecastRequest) -> ResolvedLocation:
        return ResolvedLocation(
            name="广东省广州市",
            code="440100",
            latitude=23.1291,
            longitude=113.2644,
            source="test",
        )


async def test_forecast_service_uses_resolved_national_location():
    provider = CapturingProvider()
    service = ForecastService(providers={"open_meteo": provider}, location_resolver=FakeLocationResolver())

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
