from fastapi.testclient import TestClient

from services.weather_bot.main import create_app
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


class FakeForecastService:
    async def forecast(self, request):
        return WeatherSubmission(
            task_id="WEATHER-CN-440300-20260610-DAYAHEAD-001",
            region="广东省深圳市",
            target_date="2026-06-10",
            data_cutoff_time="2026-06-09T16:00:00+08:00",
            provider_results=[],
            aggregated_forecast=AggregatedForecast(
                providers_used=["open_meteo"],
                points=[
                    ForecastPoint(
                        time="2026-06-10T00:00:00+08:00",
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
            disclaimer="本输出仅用于小可爱电力社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。",
        )


def test_health_endpoint_reports_ok():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_forecast_endpoint_returns_standard_submission():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post(
        "/api/weather/forecast",
        json={
            "region": "深圳",
            "target_date": "2026-06-10",
            "granularity": "1h",
            "providers": ["open_meteo"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "WEATHER-CN-440300-20260610-DAYAHEAD-001"
    assert body["region"] == "广东省深圳市"
    assert body["aggregated_forecast"]["providers_used"] == ["open_meteo"]


def test_feishu_event_rejects_invalid_verification_token():
    client = TestClient(
        create_app(
            forecast_service=FakeForecastService(),
            feishu_verification_token="expected-token",
        )
    )

    response = client.post("/feishu/events", json={"token": "bad-token"})

    assert response.status_code == 403


def test_feishu_url_verification_returns_challenge():
    client = TestClient(
        create_app(
            forecast_service=FakeForecastService(),
            feishu_verification_token="expected-token",
        )
    )

    response = client.post(
        "/feishu/events",
        json={
            "type": "url_verification",
            "token": "expected-token",
            "challenge": "challenge-code",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-code"}
