from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.llm import LlmClient
from services.weather_bot.main import create_app
from services.weather_bot.search import TavilySearchClient


BOT_OPEN_ID = "ou_weather_bot"


class _FailOnForecastService:
    async def forecast(self, request):
        raise AssertionError("subscription commands must not call a weather provider")


def _event(
    text: str,
    *,
    user_id: str = "user-a",
    chat_type: str = "p2p",
    chat_id: str = "chat-a",
    thread_id: str = "thread-a",
    mention_bot: bool = False,
) -> dict:
    message = {
        "message_id": f"om-{uuid4().hex}",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": "text",
        "thread_id": thread_id,
        "content": {"text": text},
    }
    if mention_bot:
        message["mentions"] = [
            {
                "key": "@_user_1",
                "name": "云云",
                "id": {"open_id": BOT_OPEN_ID},
            }
        ]
    return {
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": user_id},
            },
            "message": message,
        }
    }


def _client(
    tmp_path,
    monkeypatch,
    *,
    admin_ids: str = "[]",
    send_calls: list[str] | None = None,
) -> TestClient:
    calls = send_calls if send_calls is not None else []

    async def record_send(*args, **kwargs):
        calls.append("send")
        return "unexpected-message-id"

    async def fail_external_call(*args, **kwargs):
        raise AssertionError("subscription events must not call LLM or search")

    monkeypatch.setattr(FeishuClient, "send_text_message", record_send)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "reply_text_message", record_send)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", record_send)
    monkeypatch.setattr(LlmClient, "chat", fail_external_call)
    monkeypatch.setattr(TavilySearchClient, "search", fail_external_call)
    settings = Settings(
        feishu_weather_verification_token=None,
        feishu_weather_bot_open_id=BOT_OPEN_ID,
        subscriptions_db=str(tmp_path / "subscriptions.db"),
        subscription_admin_open_ids_json=admin_ids,
    )
    return TestClient(
        create_app(forecast_service=_FailOnForecastService(), settings=settings)
    )


def test_private_subscription_draft_uses_exact_scope_and_only_sends_a_direct_reply(
    tmp_path,
    monkeypatch,
) -> None:
    send_calls: list[str] = []
    client = _client(tmp_path, monkeypatch, send_calls=send_calls)

    response = client.post(
        "/feishu/events/weather",
        json=_event("每天8:30给我看山东、河南和河北"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "subscription_draft"
    assert body["send_performed"] is False
    assert body["event_reply_message_id"] == "unexpected-message-id"
    assert body["subscription"]["status"] == "draft"
    assert body["subscription"]["scope"] == {
        "bot_role": "weather_forecast_bot",
        "chat_type": "p2p",
        "chat_id": "chat-a",
        "thread_id": "thread-a",
        "user_id": "user-a",
    }
    assert send_calls == ["send"]


def test_private_subscription_only_exact_confirmation_activates_in_same_scope(
    tmp_path,
    monkeypatch,
) -> None:
    send_calls: list[str] = []
    client = _client(tmp_path, monkeypatch, send_calls=send_calls)
    draft = client.post(
        "/feishu/events/weather",
        json=_event("广东体感温度超过38℃时提醒我"),
    ).json()

    vague = client.post(
        "/feishu/events/weather",
        json=_event("可以"),
    ).json()
    active = client.post(
        "/feishu/events/weather",
        json=_event("确认订阅"),
    ).json()

    assert draft["status"] == "subscription_draft"
    assert vague["status"] == "subscription_confirmation_required"
    assert "确认订阅" in vague["text"]
    assert active["status"] == "subscription_active"
    assert active["subscription"]["version"] == 2
    assert active["subscription"]["confirmed_by_user_id"] == "user-a"
    assert active["send_performed"] is False
    assert draft["event_reply_message_id"] == "unexpected-message-id"
    assert vague["event_reply_message_id"] == "unexpected-message-id"
    assert active["event_reply_message_id"] == "unexpected-message-id"
    assert send_calls == ["send", "send", "send"]


def test_private_subscription_cannot_be_confirmed_by_other_user_or_thread(
    tmp_path,
    monkeypatch,
) -> None:
    send_calls: list[str] = []
    client = _client(tmp_path, monkeypatch, send_calls=send_calls)
    client.post(
        "/feishu/events/weather",
        json=_event("每天8:30给我看山东"),
    )

    other_user = client.post(
        "/feishu/events/weather",
        json=_event("确认订阅", user_id="user-b"),
    ).json()
    other_thread = client.post(
        "/feishu/events/weather",
        json=_event("确认订阅", thread_id="thread-b"),
    ).json()
    owner = client.post(
        "/feishu/events/weather",
        json=_event("确认订阅"),
    ).json()

    assert other_user["status"] == "subscription_context_missing"
    assert other_thread["status"] == "subscription_context_missing"
    assert owner["status"] == "subscription_active"
    assert owner["subscription"]["version"] == 2
    assert send_calls == ["send", "send", "send", "send"]


def test_group_subscription_requires_structured_mention_then_member_and_admin_confirmations(
    tmp_path,
    monkeypatch,
) -> None:
    send_calls: list[str] = []
    client = _client(
        tmp_path,
        monkeypatch,
        admin_ids='["group-admin"]',
        send_calls=send_calls,
    )

    unaddressed = client.post(
        "/feishu/events/weather",
        json=_event(
            "广东体感温度超过38℃时提醒我",
            chat_type="group",
        ),
    ).json()
    no_unaddressed_draft = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 确认订阅",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()
    draft = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 广东体感温度超过38℃时提醒我",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()
    pending = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 确认订阅",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()
    active = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 确认订阅",
            user_id="group-admin",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()

    assert unaddressed == {
        "status": "ignored",
        "bot_role": "weather_forecast_bot",
        "reason": "group_message_not_addressed",
    }
    assert no_unaddressed_draft["status"] == "subscription_context_missing"
    assert draft["status"] == "subscription_draft"
    assert pending["status"] == "subscription_pending_confirmation"
    assert active["status"] == "subscription_active"
    assert active["subscription"]["confirmed_by_user_id"] == "group-admin"
    assert active["subscription"]["scope"]["user_id"] == "user-a"
    assert send_calls == ["send", "send", "send", "send"]


def test_group_admin_cannot_confirm_from_another_thread_and_vague_ack_is_silent(
    tmp_path,
    monkeypatch,
) -> None:
    send_calls: list[str] = []
    client = _client(
        tmp_path,
        monkeypatch,
        admin_ids='["group-admin"]',
        send_calls=send_calls,
    )
    client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 每天8:30给我看山东",
            chat_type="group",
            mention_bot=True,
        ),
    )

    vague = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 好的",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()
    pending = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 确认订阅",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()
    wrong_thread = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 确认订阅",
            user_id="group-admin",
            chat_type="group",
            thread_id="thread-b",
            mention_bot=True,
        ),
    ).json()
    active = client.post(
        "/feishu/events/weather",
        json=_event(
            "@云云 确认订阅",
            user_id="group-admin",
            chat_type="group",
            mention_bot=True,
        ),
    ).json()

    assert vague["status"] == "ignored"
    assert vague["reason"] == "irrelevant_group_message"
    assert pending["subscription"]["version"] == 2
    assert wrong_thread["status"] == "subscription_context_missing"
    assert active["status"] == "subscription_active"
    assert active["subscription"]["version"] == 3
    assert send_calls == ["send", "send", "send", "send"]


def test_private_threshold_update_and_cancel_are_scoped_visible_replies_only(
    tmp_path,
    monkeypatch,
) -> None:
    send_calls: list[str] = []
    client = _client(tmp_path, monkeypatch, send_calls=send_calls)

    draft = client.post(
        "/feishu/events/weather",
        json=_event("广东体感温度超过38℃时提醒我"),
    ).json()
    updated = client.post(
        "/feishu/events/weather",
        json=_event("把阈值38℃改成39℃"),
    ).json()
    cancelled = client.post(
        "/feishu/events/weather",
        json=_event("取消订阅"),
    ).json()

    assert draft["status"] == "subscription_draft"
    assert updated["status"] == "subscription_updated"
    assert updated["subscription"]["version"] == 2
    assert updated["subscription"]["spec"]["trigger_threshold"] == 39.0
    assert cancelled["status"] == "subscription_cancelled"
    assert cancelled["subscription"]["version"] == 3
    assert cancelled["send_performed"] is False
    assert send_calls == ["send", "send", "send"]
