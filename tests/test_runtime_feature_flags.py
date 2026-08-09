from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.llm import LlmClient
from services.weather_bot.main import create_app
from services.weather_bot.search import TavilySearchClient


BOT_OPEN_ID = "ou_runtime_flags_weather_bot"


class _ExternalCallTrap:
    def __init__(self) -> None:
        self.forecast_calls = 0

    async def forecast(self, request):
        self.forecast_calls += 1
        raise AssertionError("a disabled capability must not call ForecastService")


def test_runtime_capability_defaults_are_fail_closed(monkeypatch) -> None:
    for name in (
        "ELECTRICITY_WEATHER_ANALYSIS_ENABLED",
        "MANUAL_POWER_BRIEFING_ENABLED",
        "SUBSCRIPTIONS_ENABLED",
        "ALERT_EVALUATION_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.electricity_weather_analysis_enabled is False
    assert settings.manual_power_briefing_enabled is False
    assert settings.subscriptions_enabled is False
    assert settings.alert_evaluation_enabled is False


def _event(
    text: str,
    *,
    chat_type: str = "p2p",
    mention_bot: bool = False,
) -> dict:
    event_id = uuid4().hex
    message = {
        "message_id": f"om-{event_id}",
        "chat_id": "runtime-flags-chat",
        "chat_type": chat_type,
        "message_type": "text",
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
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "runtime-flags-user"},
            },
            "message": message,
        },
    }


def _client(
    monkeypatch,
    tmp_path,
    trap: _ExternalCallTrap,
    send_calls: list[str],
    **overrides,
) -> TestClient:
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "memory.db"))

    async def record_send(*_args, **_kwargs) -> str:
        send_calls.append("send")
        return "runtime-flag-reply"

    async def fail_external_ai(*_args, **_kwargs):
        raise AssertionError("a disabled capability must not call LLM or search")

    monkeypatch.setattr(FeishuClient, "send_text_message", record_send)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "reply_text_message", record_send)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", record_send)
    monkeypatch.setattr(LlmClient, "chat", fail_external_ai)
    monkeypatch.setattr(TavilySearchClient, "search", fail_external_ai)
    settings = Settings(
        app_env="test",
        feishu_weather_bot_open_id=BOT_OPEN_ID,
        subscriptions_db=str(tmp_path / "subscriptions.db"),
        alerts_db=str(tmp_path / "alerts.db"),
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        qweather_api_key=None,
        llm_api_key=None,
        tavily_api_key=None,
        **overrides,
    )
    return TestClient(create_app(forecast_service=trap, settings=settings))


def test_disabled_electricity_weather_analysis_explains_in_private_without_external_calls(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        electricity_weather_analysis_enabled=False,
    ) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("山东明天晚峰负荷天气压力"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "feature_disabled"
    assert response.json()["feature"] == "electricity_weather_analysis"
    assert "未启用" in response.json()["text"]
    assert trap.forecast_calls == 0
    assert sends == ["send"]


def test_disabled_electricity_weather_analysis_stays_silent_in_group(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        electricity_weather_analysis_enabled=False,
    ) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event(
                "@云云 山东明天晚峰负荷天气压力",
                chat_type="group",
                mention_bot=True,
            ),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "bot_role": "weather_forecast_bot",
        "reason": "electricity_weather_analysis_disabled",
    }
    assert trap.forecast_calls == 0
    assert sends == []


def test_disabled_manual_power_briefing_explains_in_private_without_generation(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        manual_power_briefing_enabled=False,
    ) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("生成今天的电力气象决策晨报 3.0"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "feature_disabled"
    assert response.json()["feature"] == "manual_power_briefing"
    assert "未启用" in response.json()["text"]
    assert trap.forecast_calls == 0
    assert sends == ["send"]


def test_disabled_manual_power_briefing_stays_silent_in_group(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        manual_power_briefing_enabled=False,
    ) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event(
                "@云云 生成今天的电力气象决策晨报 3.0",
                chat_type="group",
                mention_bot=True,
            ),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "bot_role": "weather_forecast_bot",
        "reason": "manual_power_briefing_disabled",
    }
    assert trap.forecast_calls == 0
    assert sends == []


def test_disabled_subscriptions_explain_in_private_and_do_not_create_a_draft(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        subscriptions_enabled=False,
    ) as disabled_client:
        disabled = disabled_client.post(
            "/feishu/events/weather",
            json=_event("广东体感温度超过38℃时提醒我"),
        ).json()

    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        subscriptions_enabled=True,
    ) as enabled_client:
        confirmation = enabled_client.post(
            "/feishu/events/weather",
            json=_event("确认订阅"),
        ).json()

    assert disabled["status"] == "feature_disabled"
    assert disabled["feature"] == "subscriptions"
    assert "未启用" in disabled["text"]
    assert confirmation["status"] == "subscription_context_missing"
    assert trap.forecast_calls == 0
    assert sends == ["send", "send"]


def test_disabled_subscriptions_stay_silent_in_group(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        subscriptions_enabled=False,
    ) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event(
                "@云云 广东体感温度超过38℃时提醒我",
                chat_type="group",
                mention_bot=True,
            ),
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "bot_role": "weather_forecast_bot",
        "reason": "subscriptions_disabled",
    }
    assert trap.forecast_calls == 0
    assert sends == []


def test_subscription_activation_does_not_authorize_alert_delivery_and_reports_independent_gates(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        subscriptions_enabled=True,
        alert_evaluation_enabled=False,
        alert_send_enabled=True,
        feishu_passive_reply_enabled=False,
    ) as client:
        draft = client.post(
            "/feishu/events/weather",
            json=_event("广东体感温度超过38℃时提醒我"),
        ).json()
        active = client.post(
            "/feishu/events/weather",
            json=_event("确认订阅"),
        ).json()

    assert draft["status"] == "subscription_draft"
    assert active["status"] == "subscription_active"
    assert active["send_performed"] is False
    assert active["alert_delivery"] == {
        "evaluation_enabled": False,
        "send_enabled": True,
        "activation_authorizes_delivery": False,
    }
    assert trap.forecast_calls == 0
    assert sends == []


def test_disabled_alert_evaluation_explains_in_private_without_evaluating_or_sending_alerts(
    monkeypatch,
    tmp_path,
) -> None:
    trap = _ExternalCallTrap()
    sends: list[str] = []
    with _client(
        monkeypatch,
        tmp_path,
        trap,
        sends,
        alert_evaluation_enabled=False,
        alert_send_enabled=True,
    ) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("立即评估我的订阅告警是否触发"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "feature_disabled"
    assert response.json()["feature"] == "alert_evaluation"
    assert "未启用" in response.json()["text"]
    assert trap.forecast_calls == 0
    assert sends == ["send"]
