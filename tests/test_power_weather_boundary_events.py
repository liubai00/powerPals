from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import create_app


class _FailOnForecastService:
    def __init__(self) -> None:
        self.calls = 0

    async def forecast(self, request):
        self.calls += 1
        raise AssertionError("a deterministic data boundary must run before weather retrieval")


def _event(text: str) -> dict:
    token = uuid4().hex
    return {
        "header": {
            "event_id": f"event-{token}",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "user-a"}},
            "message": {
                "message_id": f"message-{token}",
                "chat_id": "chat-private-a",
                "chat_type": "p2p",
                "message_type": "text",
                "content": {"text": text},
            },
        },
    }


@pytest.mark.parametrize(
    ("query", "required_phrases"),
    [
        (
            "四川降雨会让水电增加多少？",
            ("水文气象代理", "不能换算水电增量"),
        ),
        (
            "没有轮毂高度模型时，甘肃轮毂高度风速是多少？",
            ("仅有10米地面风", "不能给出轮毂高度风速"),
        ),
        (
            "广东强对流会不会导致电网故障？",
            ("只能说明气象危险", "不能断言"),
        ),
    ],
)
def test_public_weather_event_fails_closed_before_fetching_unsupported_power_facts(
    query: str,
    required_phrases: tuple[str, ...],
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "memory.db"))
    sends: list[str] = []

    async def record_reply(*_args, **_kwargs) -> str:
        sends.append("reply")
        return "reply-message-id"

    monkeypatch.setattr(FeishuClient, "send_text_message", record_reply)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_reply)
    monkeypatch.setattr(FeishuClient, "reply_text_message", record_reply)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", record_reply)
    service = _FailOnForecastService()
    app = create_app(
        settings=Settings(
            feishu_weather_bot_open_id="ou_weather_bot",
            subscriptions_db=str(tmp_path / "subscriptions.db"),
            power_briefing_cache_db=str(tmp_path / "briefing.db"),
        ),
        forecast_service=service,
    )

    with TestClient(app) as client:
        response = client.post("/feishu/events/weather", json=_event(query))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "data_unavailable"
    assert payload["mode"] == "external_power_data_required"
    assert all(phrase in payload["text"] for phrase in required_phrases)
    assert service.calls == 0
    assert sends == ["reply"]
