from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import create_app


BOT_OPEN_ID = "ou_weather_bot"


class _FailOnForecastService:
    async def forecast(self, request):
        raise AssertionError("weather provider must not be called for ignored group events")


def _group_event(text: str, *, sender_type: str = "user") -> dict:
    return {
        "event": {
            "sender": {
                "sender_type": sender_type,
                "sender_id": {"open_id": "ou_sender"},
            },
            "message": {
                "message_id": "om_group_gate",
                "chat_id": "oc_group_gate",
                "chat_type": "group",
                "message_type": "text",
                "content": {"text": text},
                "mentions": [
                    {
                        "key": "@_user_1",
                        "name": "云云",
                        "id": {"open_id": BOT_OPEN_ID},
                    }
                ],
            },
        }
    }


def _client() -> TestClient:
    return TestClient(
        create_app(
            forecast_service=_FailOnForecastService(),
            settings=Settings(
                feishu_weather_verification_token=None,
                feishu_weather_bot_open_id=BOT_OPEN_ID,
            ),
        )
    )


def test_group_mention_with_irrelevant_chat_stays_silent(monkeypatch):
    async def fail_send(*args, **kwargs):
        raise AssertionError("ignored group message must not send a Feishu reply")

    monkeypatch.setattr(FeishuClient, "send_text_message", fail_send)
    client = _client()

    response = client.post(
        "/feishu/events/weather",
        json=_group_event("@云云 给我讲个笑话"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "irrelevant_group_message"


def test_message_from_another_bot_stays_silent_even_when_it_mentions_yunyun(monkeypatch):
    async def fail_send(*args, **kwargs):
        raise AssertionError("bot-authored event must not send a Feishu reply")

    monkeypatch.setattr(FeishuClient, "send_text_message", fail_send)
    client = _client()

    response = client.post(
        "/feishu/events/weather",
        json=_group_event("@云云 山东明天天气", sender_type="app"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "automated_sender"
