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
    assert body["task_id"] == "WEATHER-SZ-20260610-DAYAHEAD-001"
    assert body["status"] == "draft"
    assert body["submission_deadline"] == "2026-06-09T17:00:00+08:00"


def test_publish_weather_task_endpoint_returns_task_card():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post("/api/tasks/weather/publish", json={"target_date": "2026-06-10"})

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "published"
    assert body["card"]["msg_type"] == "interactive"
    assert "WEATHER-SZ-20260610-DAYAHEAD-001" in body["text"]


def test_publish_weather_task_records_local_task_jsonl(tmp_path):
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(local_task_jsonl_path=str(task_log))
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post("/api/tasks/weather/publish", json={"target_date": "2026-06-10"})

    assert response.status_code == 200
    assert task_log.exists()
    assert "WEATHER-SZ-20260610-DAYAHEAD-001" in task_log.read_text(encoding="utf-8")


def test_feishu_event_today_weather_task_returns_task_card():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 今日气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["task"]["status"] == "published"
    assert body["card"]["card"]["header"]["title"]["content"] == "深圳气象预测任务"
