from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.main import create_app


class FakeForecastService:
    pass


def _admin_client() -> TestClient:
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=True,
    )
    return TestClient(
        create_app(forecast_service=FakeForecastService(), settings=settings),
        headers={"Authorization": "Bearer test-admin-token"},
    )


def test_publish_weather_task_endpoint_supports_city_region():
    client = _admin_client()

    response = client.post(
        "/api/tasks/weather/publish",
        json={"region": "广州", "target_date": "2026-06-10"},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["task_id"] == "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    assert task["region"] == "广东省广州市"
    assert task["latitude"] == 23.1291
    assert task["longitude"] == 113.2644
    assert "广州" in response.json()["text"]


def test_publish_weather_task_endpoint_supports_coordinates():
    client = _admin_client()

    response = client.post(
        "/api/tasks/weather/publish",
        json={
            "region": "广州南沙",
            "latitude": 22.8016,
            "longitude": 113.5252,
            "target_date": "2026-06-10",
        },
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["task_id"] == "WEATHER-CN-COORD-22_8016-113_5252-20260610-DAYAHEAD-001"
    assert task["region"] == "广州南沙"
    assert task["latitude"] == 22.8016
    assert task["longitude"] == 113.5252
