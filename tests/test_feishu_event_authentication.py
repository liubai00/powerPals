from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot import main as weather_main
from services.weather_bot.main import create_app


class _NoForecastService:
    async def forecast(self, _request: Any) -> Any:
        raise AssertionError("authentication tests must not call a weather provider")


def _settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "local_jsonl_path": str(tmp_path / "submissions.jsonl"),
        "local_task_jsonl_path": str(tmp_path / "tasks.jsonl"),
        "local_news_jsonl_path": str(tmp_path / "news.jsonl"),
        "local_hydrology_jsonl_path": str(tmp_path / "hydrology.jsonl"),
        "subscriptions_db": str(tmp_path / "subscriptions.db"),
        "alerts_db": str(tmp_path / "alerts.db"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _group_event(
    *,
    token: str | None = None,
    chat_id: str = "oc_target",
    event_id: str | None = None,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "event_type": "im.message.receive_v1",
    }
    if event_id is not None:
        header["event_id"] = event_id
    if token is not None:
        header["token"] = token
    return {
        "schema": "2.0",
        "header": header,
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_user"},
            },
            "message": {
                "chat_id": chat_id,
                "chat_type": "group",
                "message_id": "om-auth-1",
                "message_type": "text",
                "content": json.dumps(
                    {"text": "@_user_1 云云能做什么"},
                    ensure_ascii=False,
                ),
                "mentions": [
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou_weather_bot"},
                        "name": "云云",
                    }
                ],
            },
        },
    }


def test_public_event_endpoint_rejects_unsigned_payload_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("FEISHU_ALLOW_UNSIGNED_EVENTS", raising=False)
    settings = _settings(tmp_path)
    client = TestClient(create_app(forecast_service=_NoForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"type": "url_verification", "challenge": "untrusted"},
    )

    assert response.status_code == 403


def test_production_rejects_unsigned_payload_even_when_bypass_is_misconfigured(tmp_path):
    settings = _settings(
        tmp_path,
        app_env="production",
        feishu_allow_unsigned_events=True,
    )
    client = TestClient(create_app(forecast_service=_NoForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"type": "url_verification", "challenge": "untrusted"},
    )

    assert response.status_code == 403


def test_forged_group_chat_id_never_causes_a_send(monkeypatch, tmp_path):
    sends: list[tuple[str, str]] = []

    async def fake_send_text(self, chat_id, text):
        sends.append(("text", chat_id))
        return "om-unexpected"

    async def fake_send_card(self, chat_id, card):
        sends.append(("card", chat_id))
        return "om-unexpected"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send_text)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_card)
    settings = _settings(
        tmp_path,
        app_env="production",
        feishu_weather_verification_token="correct-token",
        feishu_weather_bot_open_id="ou_weather_bot",
    )
    client = TestClient(create_app(forecast_service=_NoForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json=_group_event(
            token="wrong-token",
            chat_id="oc_forged",
        ),
    )

    assert response.status_code == 403
    assert sends == []


def test_authenticated_structured_group_mention_can_receive_passive_reply(
    monkeypatch,
    tmp_path,
):
    sends: list[tuple[str, str]] = []

    async def fake_send_text(self, chat_id, text):
        sends.append(("text", chat_id))
        return "om-reply"

    async def fake_send_card(self, chat_id, card):
        sends.append(("card", chat_id))
        return "om-reply"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send_text)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_card)
    settings = _settings(
        tmp_path,
        app_env="production",
        global_feishu_send_enabled=False,
        feishu_weather_verification_token="correct-token",
        feishu_weather_bot_open_id="ou_weather_bot",
    )
    client = TestClient(create_app(forecast_service=_NoForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json=_group_event(token="correct-token"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "handled"
    assert sends == [("card", "oc_target")]


def test_authenticated_group_addressing_failure_does_not_emit_fallback_message(
    monkeypatch,
    tmp_path,
):
    sends: list[tuple[str, str]] = []

    async def fake_send_text(self, chat_id, text):
        sends.append(("text", chat_id))
        return "om-unexpected"

    async def fake_send_card(self, chat_id, card):
        sends.append(("card", chat_id))
        return "om-unexpected"

    def fail_structured_mention_parse(*args, **kwargs):
        raise RuntimeError("malformed structured mention")

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send_text)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_card)
    monkeypatch.setattr(weather_main, "mentions_expected_bot", fail_structured_mention_parse)
    settings = _settings(
        tmp_path,
        app_env="production",
        feishu_weather_verification_token="correct-token",
        feishu_weather_bot_open_id="ou_weather_bot",
    )
    client = TestClient(create_app(forecast_service=_NoForecastService(), settings=settings))
    payload = _group_event(token="correct-token")

    response = client.post("/feishu/events/weather", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "error_fallback"
    assert sends == []
