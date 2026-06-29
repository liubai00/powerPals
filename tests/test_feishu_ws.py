from types import SimpleNamespace

from services.weather_bot.config import Settings
from services.weather_bot.feishu_ws import _sdk_event_payload, _ws_bot_config


def test_weather_ws_bot_uses_role_specific_or_legacy_credentials():
    settings = Settings(
        feishu_app_id="legacy-app",
        feishu_app_secret="legacy-secret",
        feishu_weather_verification_token="weather-token",
    )

    bot = _ws_bot_config(settings, "weather")

    assert bot.app_id == "legacy-app"
    assert bot.app_secret == "legacy-secret"
    assert bot.endpoint_path == "/feishu/events/weather"
    assert bot.verification_token == "weather-token"


def test_task_ws_bot_uses_task_credentials():
    settings = Settings(
        feishu_app_id="legacy-app",
        feishu_app_secret="legacy-secret",
        feishu_task_app_id="task-app",
        feishu_task_app_secret="task-secret",
        feishu_task_verification_token="task-token",
    )

    bot = _ws_bot_config(settings, "task")

    assert bot.app_id == "task-app"
    assert bot.app_secret == "task-secret"
    assert bot.endpoint_path == "/feishu/events/task"
    assert bot.verification_token == "task-token"


def test_sdk_event_payload_adds_verification_token():
    bot = SimpleNamespace(verification_token="weather-token")
    event = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"message_id": "om_1", "content": '{"text":"?"}'}},
    }

    payload = _sdk_event_payload(event, bot)

    assert payload["header"]["token"] == "weather-token"
    assert payload["event"]["message"]["message_id"] == "om_1"
