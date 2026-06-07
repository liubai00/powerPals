from fastapi.testclient import TestClient

from services.weather_bot.main import create_app
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


class CapturingForecastService:
    def __init__(self):
        self.seen_dates = []

    async def forecast(self, request):
        self.seen_dates.append(request.target_date)
        return WeatherSubmission(
            task_id=f"WEATHER-CN-440100-{request.target_date.replace('-', '')}-DAYAHEAD-001",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time="2026-06-09T16:00:00+08:00",
            provider_results=[],
            aggregated_forecast=AggregatedForecast(
                providers_used=["open_meteo"],
                points=[
                    ForecastPoint(
                        time=f"{request.target_date}T00:00:00+08:00",
                        temperature=28.0,
                        precipitation_probability=20.0,
                        wind_speed=2.0,
                        cloud_cover=60.0,
                    )
                ],
                summary=ForecastSummary(
                    max_temperature=28.0,
                    min_temperature=28.0,
                    rain_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=60.0,
                    main_weather="多云",
                    high_risk_period="无明显高风险时段",
                ),
            ),
            confidence={"score": 0.7, "description": "中等"},
            key_factors=["多源气象预报融合"],
            risk_notes=["局地短时天气存在不确定性"],
        )


def test_forecast_range_endpoint_returns_multiple_daily_submissions():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/api/weather/forecast/range",
        json={"region": "广州", "target_date": "2026-06-10", "days": 3, "providers": ["open_meteo"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 3
    assert [item["target_date"] for item in body["submissions"]] == [
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    assert service.seen_dates == ["2026-06-10", "2026-06-11", "2026-06-12"]
