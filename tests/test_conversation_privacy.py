from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import create_app


class _NoForecastService:
    async def forecast(self, _request: Any) -> Any:
        raise AssertionError("capability requests must not call a weather provider")


def _settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "feishu_allow_unsigned_events": True,
        "feishu_weather_bot_open_id": "ou_weather_bot",
        "local_jsonl_path": str(tmp_path / "submissions.jsonl"),
        "local_task_jsonl_path": str(tmp_path / "tasks.jsonl"),
        "local_news_jsonl_path": str(tmp_path / "news.jsonl"),
        "local_hydrology_jsonl_path": str(tmp_path / "hydrology.jsonl"),
        "subscriptions_db": str(tmp_path / "subscriptions.db"),
        "alerts_db": str(tmp_path / "alerts.db"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _direct_event(text: str, *, event_id: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_private_user"},
            },
            "message": {
                "chat_id": "oc_private_chat",
                "chat_type": "p2p",
                "message_id": f"om_{event_id}",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def test_public_event_keeps_free_text_history_off_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    sent: list[str] = []

    async def fake_send_text(self, chat_id, text):
        sent.append(text)
        return "om_reply"

    async def fake_send_card(self, chat_id, card):
        sent.append(str(card))
        return "om_reply"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send_text)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_card)
    raw_text = "云云能做什么，我的手机号是13800138000"
    app = create_app(
        settings=_settings(tmp_path),
        forecast_service=_NoForecastService(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_direct_event(raw_text, event_id="evt-default-no-history"),
        )

    assert response.status_code == 200
    assert sent
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    assert raw_text.encode("utf-8") not in database.read_bytes()


def test_explicit_history_opt_in_persists_only_redacted_text(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    raw_text = "查山东明天天气，token=private-token，手机号13800138000"

    weather_memory.record_turn(
        "weather|p2p|chat|main|user",
        "user",
        raw_text,
        enabled=True,
        ttl_seconds=300,
        max_per_key=4,
    )

    turns = weather_memory.recent_turns(
        "weather|p2p|chat|main|user",
        enabled=True,
        ttl_seconds=300,
    )
    assert turns[0]["role"] == "user"
    assert "查山东明天天气" in turns[0]["content"]
    assert "private-token" not in turns[0]["content"]
    assert "13800138000" not in turns[0]["content"]


def test_opt_in_history_enforces_turn_limit_and_physically_purges_expiry(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    clock = {"now": 1_000.0}
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    key = "weather|group|chat|thread|user"

    for index in range(5):
        weather_memory.record_turn(
            key,
            "user",
            f"turn-{index}",
            enabled=True,
            ttl_seconds=60,
            max_per_key=3,
        )
        clock["now"] += 1

    assert [
        turn["content"]
        for turn in weather_memory.recent_turns(
            key,
            enabled=True,
            ttl_seconds=60,
        )
    ] == ["turn-2", "turn-3", "turn-4"]

    clock["now"] += 61
    assert weather_memory.recent_turns(
        key,
        enabled=True,
        ttl_seconds=60,
    ) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_default_history_policy_physically_removes_legacy_turns_on_startup(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    weather_memory.record_turn(
        "weather|p2p|legacy-chat|main|legacy-user",
        "user",
        "legacy free text",
        enabled=True,
        ttl_seconds=300,
        max_per_key=4,
    )

    create_app(
        settings=_settings(tmp_path),
        forecast_service=_NoForecastService(),
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_conversation_state_hashes_the_five_dimension_scope_key(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    raw_key = "weather_forecast_bot|group|oc_secret_chat|om_secret_thread|ou_secret_user"
    payload = {
        "state_version": 2,
        "last_successful_request": {
            "region": "广州",
            "target_date": "2026-08-10",
            "days": 3,
            "metrics": ["rain"],
        },
    }

    weather_memory.save_conversation_state(raw_key, payload)

    assert weather_memory.load_conversation_state(raw_key) == payload
    with sqlite3.connect(database) as connection:
        stored_key = connection.execute(
            "SELECT k FROM conversation_state"
        ).fetchone()[0]
    assert stored_key.startswith("scope-sha256:v1:")
    assert raw_key.encode("utf-8") not in database.read_bytes()


def test_opt_in_turn_history_hashes_the_five_dimension_scope_key(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    raw_key = "weather_forecast_bot|group|oc_private|om_private|ou_private"

    weather_memory.record_turn(
        raw_key,
        "user",
        "广州天气",
        enabled=True,
        ttl_seconds=300,
        max_per_key=4,
    )

    assert weather_memory.recent_turns(
        raw_key,
        enabled=True,
        ttl_seconds=300,
    ) == [{"role": "user", "content": "广州天气"}]
    with sqlite3.connect(database) as connection:
        stored_key = connection.execute("SELECT k FROM turns").fetchone()[0]
    assert stored_key.startswith("scope-sha256:v1:")
    assert raw_key.encode("utf-8") not in database.read_bytes()


def test_memory_identifiers_are_hashed_for_preferences_events_and_reply_markers(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "identifier-memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    raw_values = {
        "sender": "ou_sensitive_user_identifier",
        "event": "evt_sensitive_event_identifier",
        "chat": "oc_sensitive_chat_identifier",
        "thread": "omt_sensitive_thread_identifier",
        "message": "om_sensitive_message_identifier",
    }

    weather_memory.remember_query(
        "weather_forecast_bot",
        raw_values["sender"],
        "广州",
        3,
    )
    assert weather_memory.preferred_region(
        "weather_forecast_bot",
        raw_values["sender"],
    )["region"] == "广州"
    assert weather_memory.claim_event("weather", raw_values["event"]) is True
    weather_memory.complete_event("weather", raw_values["event"], {"status": "handled"})
    marker_scope = {
        "bot_role": "weather_forecast_bot",
        "chat_type": "group",
        "chat_id": raw_values["chat"],
        "thread_id": raw_values["thread"],
        "user_id": raw_values["sender"],
        "message_id": raw_values["message"],
    }
    weather_memory.save_bot_reply_marker(**marker_scope)
    assert weather_memory.load_bot_reply_marker(**marker_scope) == {
        "source": "recorded_bot_reply",
        **marker_scope,
    }

    stored = database.read_bytes()
    for raw in raw_values.values():
        assert raw.encode("utf-8") not in stored


def test_retry_state_accepts_only_structured_weather_entities(monkeypatch, tmp_path) -> None:
    database = tmp_path / "retry-memory.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    raw_text = "查广州天气，手机号13800138000，token=private-token"

    weather_memory.save_retry_request(
        "weather|p2p|chat|main|user",
        {
            "command_type": "forecast",
            "region": "广州",
            "target_date": "2026-08-10",
            "days": 3,
            "metrics": ["rain"],
            "text": raw_text,
        },
    )

    assert weather_memory.load_retry_request(
        "weather|p2p|chat|main|user"
    ) == {
        "command_type": "forecast",
        "region": "广州",
        "target_date": "2026-08-10",
        "days": 3,
        "metrics": ["rain"],
    }
    assert raw_text.encode("utf-8") not in database.read_bytes()
