from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.main import create_app
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    WeatherSubmission,
)


class _CapturingForecastService:
    def __init__(self) -> None:
        self.requests = []

    async def forecast(self, request) -> WeatherSubmission:
        self.requests.append(request)
        point = ForecastPoint(
            time=f"{request.target_date}T18:00:00+08:00",
            temperature=31.0,
            precipitation_probability=20.0,
            wind_speed=4.0,
            cloud_cover=50.0,
        )
        return WeatherSubmission(
            task_id="WEATHER-TEST-ELECTRICITY-ROUTING",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time=f"{request.target_date}T16:00:00+08:00",
            provider_results=[],
            aggregated_forecast=AggregatedForecast(
                providers_used=["open_meteo"],
                points=[point],
                summary=ForecastSummary(
                    max_temperature=31.0,
                    min_temperature=31.0,
                    rain_probability=20.0,
                    wind_speed=4.0,
                    cloud_cover=50.0,
                    main_weather="多云",
                    high_risk_period="无明显高风险时段",
                ),
            ),
            confidence={"score": 0.6, "description": "单一数据源"},
            key_factors=["代表点天气"],
            risk_notes=[],
        )


def _post(client: TestClient, text: str) -> dict:
    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": "ou_trader"},
                },
                "message": {
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": {"text": text},
                },
            }
        },
    )
    assert response.status_code == 200
    return response.json()


def test_invalid_trading_clock_requires_clarification_without_weather_call():
    service = _CapturingForecastService()
    client = TestClient(
        create_app(
            forecast_service=service,
            settings=Settings(feishu_weather_verification_token=None),
        )
    )

    result = _post(client, "山东明天25:00晚峰风险")

    assert result["status"] == "needs_clarification"
    assert result["mode"] == "electricity_entities"
    assert result["clarification_reasons"] == ["invalid_clock_time"]
    assert service.requests == []


def test_actual_power_fact_request_fails_closed_without_llm_or_weather_call():
    service = _CapturingForecastService()
    client = TestClient(
        create_app(
            forecast_service=service,
            settings=Settings(feishu_weather_verification_token=None),
        )
    )

    result = _post(client, "山东当前实际负荷多少")

    assert result["status"] == "data_unavailable"
    assert result["mode"] == "external_power_data_required"
    assert result["blocked_fact_types"] == ["actual_load"]
    assert "当前没有可回溯的外部实际负荷数据" in result["text"]
    assert service.requests == []


def test_analysis_zone_query_uses_declared_representative_point_and_exposes_scope():
    service = _CapturingForecastService()
    client = TestClient(
        create_app(
            forecast_service=service,
            settings=Settings(feishu_weather_verification_token=None),
        )
    )

    result = _post(client, "蒙西明日晚峰风险")

    assert result["status"] == "handled"
    assert len(service.requests) == 1
    assert service.requests[0].region == "内蒙古自治区呼和浩特市"
    assert result["mode"] == "electricity_weather_proxy"
    assert result["electricity_entities"]["analysis_areas"][0]["area_id"] == "cn-15-mengxi"
    assert result["electricity_entities"]["trading_window"]["kind"] == "evening_peak"
    assert result["representative_point"] == {
        "market_id": "cn-15-mengxi",
        "city": "呼和浩特",
        "query": "内蒙古自治区呼和浩特市",
    }
    assert "代表点" in str(result["card"])
    assert "实际负荷" in str(result["card"])
