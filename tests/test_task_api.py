from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.main import create_app


class FakeForecastService:
    pass


def test_create_weather_task_endpoint_returns_task_contract():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post("/api/tasks/weather/create", json={"target_date": "2026-06-10"})

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "WEATHER-CN-440300-20260610-DAYAHEAD-001"
    assert body["status"] == "draft"
    assert body["submission_deadline"] == "2026-06-09T17:00:00+08:00"


def test_publish_weather_task_endpoint_returns_task_card():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post("/api/tasks/weather/publish", json={"target_date": "2026-06-10"})

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "published"
    assert body["card"]["msg_type"] == "interactive"
    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in body["text"]


def test_publish_weather_task_endpoint_supports_multi_day_task():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post("/api/tasks/weather/publish", json={"target_date": "2026-06-10", "days": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["forecast_days"] == 3
    assert body["task"]["forecast_end"] == "2026-06-12T23:00:00+08:00"
    assert "预测天数" in body["text"]
    assert "3 天" in body["text"]


def test_publish_weather_task_records_local_task_jsonl(tmp_path):
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(local_task_jsonl_path=str(task_log))
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post("/api/tasks/weather/publish", json={"target_date": "2026-06-10"})

    assert response.status_code == 200
    assert task_log.exists()
    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in task_log.read_text(encoding="utf-8")


def test_get_weather_task_returns_created_national_task():
    client = TestClient(create_app(forecast_service=FakeForecastService()))
    created = client.post("/api/tasks/weather/create", json={"region": "广州", "target_date": "2026-06-10"}).json()

    response = client.get(f"/api/tasks/weather/{created['task_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    assert body["region"] == "广东省广州市"
    assert body["location_code"] == "440100"


def test_get_weather_task_returns_created_coordinate_task():
    client = TestClient(create_app(forecast_service=FakeForecastService()))
    created = client.post(
        "/api/tasks/weather/create",
        json={
            "region": "广州南沙",
            "latitude": 22.8016,
            "longitude": 113.5252,
            "target_date": "2026-06-10",
        },
    ).json()

    response = client.get(f"/api/tasks/weather/{created['task_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "WEATHER-CN-COORD-22_8016-113_5252-20260610-DAYAHEAD-001"
    assert body["region"] == "广州南沙"
    assert body["latitude"] == 22.8016
    assert body["longitude"] == 113.5252


def test_get_weather_task_loads_published_coordinate_task_from_local_jsonl_after_restart(tmp_path):
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(local_task_jsonl_path=str(task_log))
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))
    published = client.post(
        "/api/tasks/weather/publish",
        json={
            "region": "广州南沙",
            "latitude": 22.8016,
            "longitude": 113.5252,
            "target_date": "2026-06-10",
        },
    ).json()["task"]

    restarted_client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))
    response = restarted_client.get(f"/api/tasks/weather/{published['task_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "WEATHER-CN-COORD-22_8016-113_5252-20260610-DAYAHEAD-001"
    assert body["status"] == "published"
    assert body["region"] == "广州南沙"
    assert body["latitude"] == 22.8016
    assert body["longitude"] == 113.5252


def test_feishu_event_today_weather_task_without_region_asks_for_region():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 今日气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_region"
    assert body["bot_role"] == "weather_task_bot"
    assert body["mode"] == "clarification"
    assert "缺少城市或区域" in body["text"]
