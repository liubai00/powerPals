from fastapi.testclient import TestClient

from services.weather_bot.main import create_app
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


class CapturingForecastService:
    def __init__(self):
        self.seen_request = None
        self.seen_requests = []

    async def forecast(self, request):
        self.seen_request = request
        self.seen_requests.append(request)
        return WeatherSubmission(
            task_id="WEATHER-CN-440100-20260610-DAYAHEAD-001",
            region="广东省广州市",
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
        )


def test_feishu_event_supports_city_weather_forecast_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 广州明天天气"}}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "handled"
    assert response.json()["bot_role"] == "weather_forecast_bot"
    assert service.seen_request.region == "广州"


def test_feishu_event_supports_coordinate_weather_forecast_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 22.8016,113.5252 明天天气"}}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "handled"
    assert response.json()["bot_role"] == "weather_forecast_bot"
    assert service.seen_request.region == "经纬度 22.8016,113.5252"
    assert service.seen_request.latitude == 22.8016
    assert service.seen_request.longitude == 113.5252


def test_feishu_event_supports_city_weather_task_command():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 今日广州气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert body["task"]["task_id"].startswith("WEATHER-CN-440100-")
    assert body["task"]["region"] == "广东省广州市"


def test_feishu_event_supports_coordinate_weather_task_command():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 22.8016,113.5252 今日气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert body["task"]["task_id"].startswith("WEATHER-CN-COORD-22_8016-113_5252-")
    assert body["task"]["region"] == "经纬度 22.8016,113.5252"


def test_feishu_event_supports_city_multi_day_weather_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 广州未来三天天气"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["days"] == 3
    assert len(body["submissions"]) == 3
    assert [request.region for request in service.seen_requests] == ["广州", "广州", "广州"]


def test_feishu_help_describes_two_weather_bots():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 帮助"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert "全国气象预测机器人" in body["text"]
    assert "气象任务发布机器人" in body["text"]
