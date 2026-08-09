from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.location import AmbiguousLocationError, LocationNotFoundError
from services.weather_bot.main import create_app
from services.weather_bot.models import ForecastRequest


class LocationFailureForecastService:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.requests: list[ForecastRequest] = []

    async def forecast(self, request: ForecastRequest):
        self.requests.append(request)
        raise self.failure


def _event(text: str, *, event_id: str) -> dict:
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user_a"}},
            "message": {
                "message_id": f"message-{event_id}",
                "chat_id": "oc_private_a",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text": "' + text + '"}',
            },
        },
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        feishu_weather_bot_open_id="ou_weather_bot",
        llm_api_key=None,
        subscriptions_db=str(tmp_path / "subscriptions.db"),
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
    )


def _disable_external_sends(monkeypatch, tmp_path: Path) -> None:
    memory_path = tmp_path / f"memory-{uuid4().hex}.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(memory_path))

    async def fake_send(*_args, **_kwargs) -> str:
        return "dry-message-id"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", fake_send)


def test_ambiguous_location_from_public_weather_event_requests_a_choice(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _disable_external_sends(monkeypatch, tmp_path)
    service = LocationFailureForecastService(
        AmbiguousLocationError("朝阳", ("北京市朝阳区", "辽宁省朝阳市"))
    )
    app = create_app(settings=_settings(tmp_path), forecast_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("朝阳明天天气", event_id="ambiguous-location"),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_location_clarification"
    assert payload["mode"] == "location_clarification"
    assert payload["location_entity"] == "朝阳"
    assert payload["location_candidates"] == ["北京市朝阳区", "辽宁省朝阳市"]
    assert "请选择" in payload["text"]
    assert len(service.requests) == 1


def test_unknown_location_in_multi_day_event_is_not_reported_as_provider_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _disable_external_sends(monkeypatch, tmp_path)
    service = LocationFailureForecastService(LocationNotFoundError("火星市"))
    app = create_app(settings=_settings(tmp_path), forecast_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("火星市未来3天天气", event_id="unknown-location"),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_location_clarification"
    assert payload["mode"] == "location_clarification"
    assert payload["reason"] == "location_not_found"
    assert payload["location_entity"] == "火星市"
    assert payload["location_candidates"] == []
    assert "省、市或区县" in payload["text"]
    assert len(service.requests) == 1
