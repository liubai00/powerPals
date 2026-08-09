from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import (
    _event_thread_id,
    _is_power_briefing_command,
    create_app,
)
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastWindow,
    ForecastPoint,
    ForecastRequest,
    ForecastSummary,
    ProviderForecast,
    TimeInfo,
    WeatherSubmission,
)
from services.weather_bot.power_briefing import MARKET_POINTS


class CapturingForecastService:
    def __init__(self) -> None:
        self.requests: list[ForecastRequest] = []
        self.failures_remaining = 0

    async def forecast(self, request: ForecastRequest) -> WeatherSubmission:
        self.requests.append(request)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("simulated weather provider failure")
        points = [
            ForecastPoint(
                time=f"{request.target_date}T12:00:00+08:00",
                temperature=25.0,
                precipitation_probability=10.0,
                wind_speed=2.0,
                cloud_cover=20.0,
            )
        ]
        return WeatherSubmission(
            task_id=f"task-{len(self.requests)}",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time="2026-07-27T08:00:00+08:00",
            time_info=TimeInfo(
                retrieved_at="2026-07-27T08:00:00+08:00",
                provider_issued_at={"test": "2026-07-27T07:00:00+08:00"},
                aggregation_completed_at="2026-07-27T08:00:01+08:00",
                valid_time=ForecastWindow(
                    start=f"{request.target_date}T00:00:00+08:00",
                    end=f"{request.target_date}T23:00:00+08:00",
                ),
                forecast_run_id=f"source-run-{len(self.requests)}",
            ),
            provider_results=[
                ProviderForecast(
                    provider="test",
                    points=points,
                    retrieved_at="2026-07-27T08:00:00+08:00",
                    provider_issued_at="2026-07-27T07:00:00+08:00",
                    source_url="https://example.test/weather",
                    content_sha256="a" * 64,
                )
            ],
            aggregated_forecast=AggregatedForecast(
                providers_used=["test"],
                points=points,
                summary=ForecastSummary(
                    max_temperature=28.0,
                    min_temperature=20.0,
                    rain_probability=10.0,
                    wind_speed=2.0,
                    cloud_cover=20.0,
                    main_weather="晴",
                    high_risk_period="无",
                ),
            ),
            confidence={"score": 0.8, "description": "测试"},
            key_factors=["测试"],
            risk_notes=[],
        )


def _settings() -> Settings:
    return Settings(
        feishu_app_id=None,
        feishu_app_secret=None,
        feishu_verification_token=None,
        feishu_encrypt_key=None,
        feishu_bot_open_id="ou_weather_bot",
        llm_api_key=None,
        power_briefing_cache_db=str(
            Path("data") / ".test-memory" / f"briefing-{uuid4().hex}.db"
        ),
    )


def _event(
    text: str,
    *,
    message_id: str,
    chat_id: str = "chat-a",
    sender_id: str = "user-a",
    chat_type: str = "p2p",
    thread_id: str | None = None,
    root_id: str | None = None,
) -> dict:
    message = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": "text",
        "content": f'{{"text": "{text}"}}',
    }
    if chat_type == "group" and text.lstrip().startswith("@云云"):
        message["mentions"] = [
            {
                "key": "@_user_1",
                "id": {"open_id": "ou_weather_bot"},
                "name": "云云",
            }
        ]
    if thread_id:
        message["thread_id"] = thread_id
    if root_id:
        message["root_id"] = root_id
    return {
        "header": {"event_id": f"event-{message_id}", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender_id}},
            "message": message,
        },
    }


def _post(client: TestClient, event: dict) -> dict:
    response = client.post("/feishu/events", json=event)
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def isolated_db_path() -> Path:
    base = Path("data") / ".test-memory"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{uuid4().hex}.db"
    yield path
    for suffix in ("", "-shm", "-wal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def _patch_isolated_memory(monkeypatch, db_path: Path) -> None:
    monkeypatch.setattr(weather_memory, "DB_PATH", str(db_path))

    async def fake_send(*args, **kwargs) -> str:
        return "dry-message-id"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", fake_send)


def test_followups_inherit_only_last_successful_request(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州未来3天天气", message_id="m1"))
        before = len(service.requests)
        metric_result = _post(client, _event("只看降雨", message_id="m2"))
        metric_requests = service.requests[before:]
        assert metric_requests == []  # 同一请求可以安全复用本地预报缓存
        assert "广州" in metric_result["region"]
        assert metric_result["days"] == 3
        assert metric_result["metrics"] == ["rain"]

        before = len(service.requests)
        _post(client, _event("换成盘锦", message_id="m3"))
        changed_requests = service.requests[before:]
        assert len(changed_requests) == 3
        assert all("盘锦" in item.region for item in changed_requests)
        assert {item.days for item in changed_requests} == {3}

        before = len(service.requests)
        _post(client, _event("那明天呢", message_id="m4"))
        tomorrow_requests = service.requests[before:]
        assert len(tomorrow_requests) == 1
        assert all("盘锦" in item.region for item in tomorrow_requests)


def test_correction_uses_new_location_only(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州天气", message_id="m1"))
        before = len(service.requests)
        _post(client, _event("不是广州，是深圳", message_id="m2"))

    corrected = service.requests[before:]
    assert len(corrected) == 1
    assert "深圳" in corrected[0].region
    assert "广州" not in corrected[0].region


def test_reset_clears_inherited_weather_state(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州天气", message_id="m1"))
        _post(client, _event("重新查，不要沿用刚才的", message_id="m2"))
        before = len(service.requests)
        result = _post(client, _event("只看降雨", message_id="m3"))

    assert len(service.requests) == before
    assert result["status"] == "needs_region"


def test_context_is_isolated_by_chat(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州天气", message_id="m1", chat_id="chat-a"))
        _post(client, _event("北京天气", message_id="m2", chat_id="chat-b"))
        before = len(service.requests)
        _post(client, _event("只看降雨", message_id="m3", chat_id="chat-a"))

    followup = service.requests[before:]
    assert len(followup) == 1
    assert followup[0].region == "广州"


def test_context_is_isolated_by_user_inside_same_thread(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(
            client,
            _event(
                "@云云 广州天气",
                message_id="m1",
                chat_type="group",
                thread_id="thread-1",
                sender_id="user-a",
            ),
        )
        _post(
            client,
            _event(
                "@云云 上海天气",
                message_id="m2",
                chat_type="group",
                thread_id="thread-1",
                sender_id="user-b",
            ),
        )
        before = len(service.requests)
        _post(
            client,
            _event(
                "@云云 那明天呢",
                message_id="m3",
                chat_type="group",
                thread_id="thread-1",
                sender_id="user-a",
            ),
        )

    followup = service.requests[before:]
    assert len(followup) == 1
    assert followup[0].region == "广州"


def test_group_reply_without_mention_only_works_for_recorded_bot_message(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    def post_weather(client: TestClient, event: dict) -> dict:
        response = client.post("/feishu/events/weather", json=event)
        assert response.status_code == 200
        return response.json()

    with TestClient(app) as client:
        first = post_weather(
            client,
            _event(
                "@云云 广州天气",
                message_id="bot-reply-seed",
                chat_type="group",
            ),
        )
        requests_before_reply = len(service.requests)
        trusted_reply = post_weather(
            client,
            _event(
                "明天呢",
                message_id="trusted-bot-reply",
                chat_type="group",
                root_id=first["event_reply_message_id"],
            ),
        )
        untrusted_reply = post_weather(
            client,
            _event(
                "明天呢",
                message_id="untrusted-thread-reply",
                chat_type="group",
                root_id="somebody-elses-message",
            ),
        )

    assert first["status"] == "handled"
    assert trusted_reply["status"] == "handled"
    assert len(service.requests) == requests_before_reply + 1
    assert service.requests[-1].region == "广州"
    assert untrusted_reply == {
        "status": "ignored",
        "bot_role": "weather_forecast_bot",
        "reason": "group_message_not_addressed",
    }


def test_private_out_of_scope_chat_returns_capability_boundary_without_external_calls(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    sent_messages: list[str] = []

    async def fail_external(*args, **kwargs):
        raise AssertionError("out-of-scope chat must not call LLM, search, or typhoon APIs")

    async def capture_text(_client, _chat_id: str, text: str, *args, **kwargs) -> str:
        sent_messages.append(text)
        return "boundary-message-id"

    monkeypatch.setattr("services.weather_bot.llm.LlmClient.chat", fail_external)
    monkeypatch.setattr("services.weather_bot.search.TavilySearchClient.search", fail_external)
    monkeypatch.setattr("services.weather_bot.typhoon.TyphoonClient.brief_for_text", fail_external)
    monkeypatch.setattr(FeishuClient, "send_text_message", capture_text)
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        result = _post(client, _event("讲个笑话", message_id="private-boundary"))

    assert result["status"] == "handled"
    assert result["mode"] == "capability_boundary"
    assert "天气" in result["text"]
    assert len(result["text"]) <= 120
    assert service.requests == []
    assert sent_messages == [result["text"]]


def test_group_reply_marker_is_isolated_by_bot_role(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    app = create_app(settings=_settings(), forecast_service=CapturingForecastService())

    with TestClient(app) as client:
        weather_response = client.post(
            "/feishu/events/weather",
            json=_event(
                "@云云 广州天气",
                message_id="role-weather-seed",
                chat_type="group",
            ),
        ).json()
        task_response = client.post(
            "/feishu/events/task",
            json=_event(
                "明天呢",
                message_id="role-task-cross-reply",
                chat_type="group",
                root_id=weather_response["event_reply_message_id"],
            ),
        ).json()

    assert weather_response["status"] == "handled"
    assert task_response == {
        "status": "ignored",
        "bot_role": "weather_task_bot",
        "reason": "group_message_not_addressed",
    }


def test_reply_to_recorded_briefing_explains_from_the_same_snapshot_without_refetch(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()

    async def fail_external(*args, **kwargs):
        raise AssertionError("briefing explanation must not call LLM, search, or live weather")

    monkeypatch.setattr("services.weather_bot.llm.LlmClient.chat", fail_external)
    monkeypatch.setattr("services.weather_bot.search.TavilySearchClient.search", fail_external)
    app = create_app(settings=_settings(), forecast_service=service)

    def post_weather(client: TestClient, payload: dict) -> dict:
        response = client.post("/feishu/events/weather", json=payload)
        assert response.status_code == 200
        return response.json()

    with TestClient(app) as client:
        generated = post_weather(
            client,
            _event(
                "@云云 生成今天的电力气象决策晨报 2.0",
                message_id="briefing-explain-seed",
                chat_type="group",
            ),
        )
        requests_after_generation = len(service.requests)
        explained = post_weather(
            client,
            _event(
                "为什么风险升高",
                message_id="briefing-explain-question",
                chat_type="group",
                root_id=generated["event_reply_message_id"],
            ),
        )

    assert generated["status"] == "handled"
    assert explained["status"] == "handled"
    assert explained["mode"] == "power_briefing_explain"
    assert explained["cache_hit"] is True
    assert explained["briefing_cache_key"] == generated["briefing_cache_key"]
    assert explained["generated_at"] == generated["generated_at"]
    assert "同一份晨报快照" in explained["text"]
    assert len(service.requests) == requests_after_generation


def test_duplicate_event_is_deduplicated_across_app_restart(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    payload = _event("广州天气", message_id="same-message")

    app1 = create_app(settings=_settings(), forecast_service=service)
    with TestClient(app1) as client:
        _post(client, payload)

    app2 = create_app(settings=_settings(), forecast_service=service)
    with TestClient(app2) as client:
        result = _post(client, payload)

    assert len(service.requests) == 1
    assert result["status"] == "ignored"


def test_event_processing_lease_covers_nationwide_briefing_window(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])

    assert weather_memory.claim_event("weather", "long-briefing")
    clock["now"] += 600
    assert not weather_memory.claim_event("weather", "long-briefing")
    clock["now"] += 301
    assert weather_memory.claim_event("weather", "long-briefing")


def test_briefing_context_has_independent_36_hour_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "memory.db"))
    now = [1_000_000.0]
    monkeypatch.setattr(weather_memory.time, "time", lambda: now[0])
    conversation_key = "weather_forecast_bot|group|chat-a|main|user-a"
    weather_memory.save_conversation_state(
        conversation_key,
        {"state_version": 2, "last_successful_request": {"region": "广州"}},
        ttl_seconds=weather_memory.GROUP_QUERY_TTL_SECONDS,
    )
    weather_memory.save_briefing_context(
        conversation_key,
        {"last_power_briefing_cache_key": "briefing-cache-key"},
    )

    now[0] += weather_memory.GROUP_QUERY_TTL_SECONDS + 1

    assert weather_memory.load_conversation_state(conversation_key) is None
    assert weather_memory.load_briefing_context(conversation_key) == {
        "last_power_briefing_cache_key": "briefing-cache-key"
    }

    now[0] = 1_000_000.0 + weather_memory.BRIEFING_CONTEXT_TTL_SECONDS

    assert weather_memory.load_briefing_context(conversation_key) is None


def test_failed_event_can_be_retried_with_same_event_id(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    service.failures_remaining = 1
    app = create_app(settings=_settings(), forecast_service=service)
    payload = _event("广州天气", message_id="retry-message")

    with TestClient(app) as client:
        first = _post(client, payload)
        second = _post(client, payload)

    assert first["status"] == "error_fallback"
    assert second["status"] == "handled"
    assert len(service.requests) == 2


def test_pending_region_clarification_survives_app_restart(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()

    app1 = create_app(settings=_settings(), forecast_service=service)
    with TestClient(app1) as client:
        first = _post(
            client,
            _event("预测下最近四天的气象数据", message_id="clarify-before-restart"),
        )

    app2 = create_app(settings=_settings(), forecast_service=service)
    with TestClient(app2) as client:
        second = _post(
            client,
            _event("广州", message_id="clarify-after-restart"),
        )

    assert first["status"] == "needs_region"
    assert second["status"] == "handled"
    assert second["days"] == 4
    assert [request.region for request in service.requests] == ["广州"] * 4


def test_pending_region_clarification_expires_at_ten_minutes(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])

    weather_memory.save_pending_clarification(
        "weather_forecast_bot|p2p|chat-a|main|user-a",
        {"command_type": "forecast", "days": 4},
    )
    clock["now"] += 600

    assert (
        weather_memory.load_pending_clarification(
            "weather_forecast_bot|p2p|chat-a|main|user-a"
        )
        is None
    )


def test_group_weather_context_expires_at_thirty_minutes(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(
            client,
            _event(
                "@云云 广州天气",
                message_id="group-context-before-expiry",
                chat_type="group",
            ),
        )
        requests_before_followup = len(service.requests)
        clock["now"] += 30 * 60
        result = _post(
            client,
            _event(
                "@云云 明天呢",
                message_id="group-context-at-expiry",
                chat_type="group",
            ),
        )

    assert len(service.requests) == requests_before_followup
    assert result.get("region") is None


def test_direct_weather_context_lasts_until_twenty_four_hour_boundary(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(
            client,
            _event("广州天气", message_id="direct-a-seed", sender_id="user-a"),
        )
        _post(
            client,
            _event("上海天气", message_id="direct-b-seed", sender_id="user-b"),
        )
        clock["now"] += 24 * 3600 - 1
        _post(
            client,
            _event("那明天呢", message_id="direct-a-before-expiry", sender_id="user-a"),
        )
        requests_before_expired_followup = len(service.requests)
        clock["now"] += 1
        expired = _post(
            client,
            _event("那明天呢", message_id="direct-b-at-expiry", sender_id="user-b"),
        )

    assert service.requests[-1].region == "广州"
    assert len(service.requests) == requests_before_expired_followup
    assert expired.get("region") is None


def test_cancel_clears_pending_region_clarification(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(
            client,
            _event("预测下最近四天的气象数据", message_id="cancel-pending-seed"),
        )
        cancelled = _post(
            client,
            _event("取消", message_id="cancel-pending-command"),
        )
        city_after_cancel = _post(
            client,
            _event("广州", message_id="cancel-pending-city"),
        )

    assert cancelled["status"] == "handled"
    assert cancelled["mode"] == "clarification_cancelled"
    assert service.requests == []
    assert city_after_cancel.get("days") is None


@pytest.mark.parametrize(
    "interrupting_text",
    ["重新查，不要沿用刚才的", "云云能做什么"],
)
def test_reset_or_help_clears_pending_region_clarification(
    monkeypatch,
    isolated_db_path,
    interrupting_text: str,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(
            client,
            _event("预测下最近四天的气象数据", message_id="interrupt-pending-seed"),
        )
        _post(
            client,
            _event(interrupting_text, message_id="interrupt-pending-command"),
        )
        city_after_interrupt = _post(
            client,
            _event("广州", message_id="interrupt-pending-city"),
        )

    assert service.requests == []
    assert city_after_interrupt.get("days") is None


def test_pending_clarification_is_isolated_by_full_conversation_scope(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    def post_to(client: TestClient, path: str, payload: dict) -> dict:
        response = client.post(path, json=payload)
        assert response.status_code == 200
        return response.json()

    with TestClient(app) as client:
        seed = post_to(
            client,
            "/feishu/events/weather",
            _event(
                "预测下最近四天的气象数据",
                message_id="scope-seed",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )
        post_to(
            client,
            "/feishu/events/weather",
            _event(
                "广州",
                message_id="scope-other-user",
                chat_id="chat-a",
                sender_id="user-b",
                thread_id="thread-a",
            ),
        )
        post_to(
            client,
            "/feishu/events/weather",
            _event(
                "广州",
                message_id="scope-other-chat",
                chat_id="chat-b",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )
        post_to(
            client,
            "/feishu/events/weather",
            _event(
                "广州",
                message_id="scope-other-thread",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-b",
            ),
        )
        post_to(
            client,
            "/feishu/events/weather",
            _event(
                "@云云 广州",
                message_id="scope-other-chat-type",
                chat_id="chat-a",
                sender_id="user-a",
                chat_type="group",
                thread_id="thread-a",
            ),
        )
        post_to(
            client,
            "/feishu/events/task",
            _event(
                "广州",
                message_id="scope-other-bot-role",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )
        matching = post_to(
            client,
            "/feishu/events/weather",
            _event(
                "广州",
                message_id="scope-matching",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )

    assert seed["status"] == "needs_region"
    assert matching["status"] == "handled"
    assert matching["days"] == 4
    assert [request.region for request in service.requests] == ["广州"] * 4


def test_help_clears_only_the_matching_five_dimension_pending_state(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    def post_weather(client: TestClient, payload: dict) -> dict:
        response = client.post("/feishu/events/weather", json=payload)
        assert response.status_code == 200
        return response.json()

    with TestClient(app) as client:
        first_a = post_weather(
            client,
            _event(
                "@云云 预测下最近四天的气象数据",
                message_id="pending-a",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-a",
            ),
        )
        first_b = post_weather(
            client,
            _event(
                "@云云 预测下最近四天的气象数据",
                message_id="pending-b",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-b",
            ),
        )
        help_a = post_weather(
            client,
            _event(
                "@云云 云云能做什么",
                message_id="help-a",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-a",
            ),
        )
        after_help_a = post_weather(
            client,
            _event(
                "@云云 广州",
                message_id="city-a",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-a",
            ),
        )
        still_pending_b = post_weather(
            client,
            _event(
                "@云云 广州",
                message_id="city-b",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-b",
            ),
        )

    assert first_a["status"] == "needs_region"
    assert first_b["status"] == "needs_region"
    assert help_a["status"] == "handled"
    assert after_help_a["status"] == "ignored"
    assert still_pending_b["status"] == "handled"
    assert still_pending_b["days"] == 4
    assert [request.region for request in service.requests] == ["广州"] * 4


def test_failed_provider_request_does_not_replace_last_successful_context(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州天气", message_id="success-before-failure"))
        service.failures_remaining = 1
        failed = _post(client, _event("换成北京", message_id="provider-failure"))
        _post(client, _event("那明天呢", message_id="followup-after-failure"))

    assert failed["status"] == "error_fallback"
    assert [request.region for request in service.requests] == ["广州", "北京", "广州"]


def test_retry_replays_the_failed_request_without_overwriting_last_successful_context(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州天气", message_id="retry-success-seed"))
        service.failures_remaining = 2
        failed = _post(client, _event("换成北京", message_id="retry-failed-request"))
        retried = _post(client, _event("重试一下", message_id="retry-pending-request"))
        inherited = _post(client, _event("那明天呢", message_id="retry-last-success"))

    assert failed["status"] == "error_fallback"
    assert retried["status"] == "error_fallback"
    assert inherited["status"] == "handled"
    assert [request.region for request in service.requests] == ["广州", "北京", "北京", "广州"]


def test_failed_request_retry_expires_after_ten_minutes(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        _post(client, _event("广州天气", message_id="retry-expiry-success"))
        service.failures_remaining = 1
        _post(client, _event("换成北京", message_id="retry-expiry-failure"))
        requests_before_expired_retry = len(service.requests)
        clock["now"] += weather_memory.RETRY_REQUEST_TTL_SECONDS
        expired = _post(client, _event("重试一下", message_id="retry-expired"))
        inherited = _post(client, _event("那明天呢", message_id="retry-expiry-followup"))

    assert expired["status"] == "handled"
    assert expired["mode"] == "retry_unavailable"
    assert inherited["status"] == "handled"
    assert len(service.requests) == requests_before_expired_retry + 1
    assert service.requests[-1].region == "广州"


def test_addressed_group_can_complete_its_own_pending_clarification(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        first = client.post(
            "/feishu/events/weather",
            json=_event(
                "@云云 预测下最近四天的气象数据",
                message_id="group-pending-seed",
                chat_type="group",
                thread_id="thread-a",
            ),
        )
        second = client.post(
            "/feishu/events/weather",
            json=_event(
                "@云云 广州",
                message_id="group-pending-city",
                chat_type="group",
                thread_id="thread-a",
            ),
        )

    assert first.status_code == 200
    assert first.json()["status"] == "needs_region"
    assert second.status_code == 200
    assert second.json()["status"] == "handled"
    assert second.json()["days"] == 4
    assert [request.region for request in service.requests] == ["广州"] * 4


def test_root_message_id_is_used_as_thread_fallback() -> None:
    event = _event("广州天气", message_id="m1", root_id="root-message")["event"]
    assert _event_thread_id(event) == "root-message"


def test_task_query_does_not_create_or_publish_task(monkeypatch, isolated_db_path) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        result = _post(client, _event("查询刚才的气象任务", message_id="m1"))

    assert result["status"] == "needs_task_id"
    assert service.requests == []


def test_task_query_remind_and_close_use_last_task_in_same_conversation(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        created = _post(client, _event("发布广州未来3天气象任务", message_id="m1"))
        queried = _post(client, _event("查询刚才的气象任务", message_id="m2"))
        reminded = _post(client, _event("提醒刚才的气象任务", message_id="m3"))
        closed = _post(client, _event("关闭刚才的气象任务", message_id="m4"))

    assert created["task"]["forecast_days"] == 3
    assert queried["mode"] == "task_query"
    assert queried["task"]["task_id"] == created["task"]["task_id"]
    assert reminded["mode"] == "task_remind"
    assert "请参评 Bot" in reminded["task"]["notes"]
    assert closed["mode"] == "task_close"
    assert closed["task"]["status"] == "closed"
    assert service.requests == []


def test_manual_power_briefing_command_returns_card_without_region_clarification(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        result = _post(
            client,
            _event(
                "请生成今天的电力气象决策晨报 2.0，展示今日和明日的 Top 5 气象风险、"
                "连续风险时段、较今日变化、置信度，以及负荷、光伏和地面风资源代理排行。",
                message_id="briefing-m1",
            ),
        )

    assert result["status"] == "handled"
    assert result["mode"] == "power_briefing"
    assert result["coverage"]["provincial_areas"] == {"covered": 31, "total": 31}
    assert result["coverage"]["markets"]["covered"] == 33
    assert result["coverage"]["points"] == {"covered": 75, "total": 75, "missing": 0}
    assert result["card"]["card"]["header"]["title"]["content"].startswith("⚡ 电力气象决策晨报 3.0")
    assert len(service.requests) == len(MARKET_POINTS) * 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("生成今天的电力气象决策晨报 2.0", True),
        ("生成今天的电力气象决策晨报 3.0", True),
        ("晨报3.0", True),
        ("今天的电力气象晨报", True),
        ("预览晨报2.0", True),
        ("电力气象晨报应该包含什么", False),
        ("晨报有哪些业务指标", False),
    ],
)
def test_power_briefing_intent_requires_a_generation_request(text: str, expected: bool) -> None:
    assert _is_power_briefing_command(text) is expected


def test_expand_all_markets_uses_cached_snapshot_and_isolates_conversation(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        generated = _post(
            client,
            _event(
                "生成今天的电力气象决策晨报 2.0",
                message_id="briefing-generate",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )
        requests_after_generation = len(service.requests)
        expanded = _post(
            client,
            _event(
                "展开全部市场",
                message_id="briefing-expand",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )
        other_user = _post(
            client,
            _event(
                "展开全部市场",
                message_id="briefing-other-user",
                chat_id="chat-a",
                sender_id="user-b",
                thread_id="thread-a",
            ),
        )
        other_chat = _post(
            client,
            _event(
                "展开全部市场",
                message_id="briefing-other-chat",
                chat_id="chat-b",
                sender_id="user-a",
                thread_id="thread-a",
            ),
        )
        other_thread = _post(
            client,
            _event(
                "展开全部市场",
                message_id="briefing-other-thread",
                chat_id="chat-a",
                sender_id="user-a",
                thread_id="thread-b",
            ),
        )

    assert generated["cache_hit"] is False
    assert expanded["status"] == "handled"
    assert expanded["mode"] == "power_briefing_expand"
    assert expanded["cache_hit"] is True
    assert "全部明细" in expanded["card"]["card"]["header"]["title"]["content"]
    assert len(service.requests) == requests_after_generation
    assert other_user["status"] == "needs_briefing_context"
    assert other_chat["status"] == "needs_briefing_context"
    assert other_thread["status"] == "needs_briefing_context"


def test_top_level_briefing_can_expand_from_reply_thread_without_cross_user_leak(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        generated = _post(
            client,
            _event(
                "生成今天的电力气象决策晨报 2.0",
                message_id="briefing-top-level",
                chat_id="chat-a",
                sender_id="user-a",
            ),
        )
        requests_after_generation = len(service.requests)
        expanded = _post(
            client,
            _event(
                "展开全部分析区",
                message_id="briefing-thread-expand",
                chat_id="chat-a",
                sender_id="user-a",
                root_id=generated["event_reply_message_id"],
            ),
        )
        other_user = _post(
            client,
            _event(
                "展开全部分析区",
                message_id="briefing-thread-other-user",
                chat_id="chat-a",
                sender_id="user-b",
                root_id=generated["event_reply_message_id"],
            ),
        )

    assert expanded["status"] == "handled"
    assert expanded["mode"] == "power_briefing_expand"
    assert len(service.requests) == requests_after_generation
    assert other_user["status"] == "needs_briefing_context"


def test_scheduled_briefing_thread_pointer_allows_any_replying_user_to_expand(
    monkeypatch,
    isolated_db_path,
) -> None:
    from scripts import daily_power_briefing

    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        generated = _post(
            client,
            _event(
                "@云云 生成今天的电力气象决策晨报 2.0",
                message_id="scheduled-seed",
                chat_id="scheduled-chat",
                sender_id="operator",
                chat_type="group",
            ),
        )
        remember = getattr(
            daily_power_briefing,
            "_remember_scheduled_briefing_thread",
            None,
        )
        assert callable(remember)
        remember(
            "scheduled-chat",
            "scheduled-card-message",
            generated["briefing_cache_key"],
            generated["generated_at"],
        )
        requests_after_generation = len(service.requests)
        expanded = _post(
            client,
            _event(
                "展开全部分析区",
                message_id="scheduled-expand",
                chat_id="scheduled-chat",
                sender_id="reader",
                chat_type="group",
                root_id="scheduled-card-message",
            ),
        )
        unrelated_thread = _post(
            client,
            _event(
                "展开全部分析区",
                message_id="scheduled-unrelated",
                chat_id="scheduled-chat",
                sender_id="reader",
                chat_type="group",
                root_id="another-message",
            ),
        )

    assert expanded["status"] == "handled"
    assert expanded["mode"] == "power_briefing_expand"
    assert len(service.requests) == requests_after_generation
    assert unrelated_thread == {
        "status": "ignored",
        "bot_role": "legacy_combined_bot",
        "reason": "group_message_not_addressed",
    }


def test_expand_all_markets_without_prior_briefing_does_not_fetch(
    monkeypatch,
    isolated_db_path,
) -> None:
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        result = _post(
            client,
            _event("查看全部市场", message_id="briefing-no-context"),
        )

    assert result["status"] == "needs_briefing_context"
    assert service.requests == []


def test_chinese_window_day_is_not_parsed_as_calendar_day() -> None:
    from services.weather_bot.dates import parse_date_span

    start, days, requested_days, status = parse_date_span("广州接下来四日天气", today=date(2026, 7, 27))
    assert start == "2026-07-27"
    assert days == 4
    assert requested_days == 4
    assert status == "ok"


def test_explicit_start_date_keeps_requested_window() -> None:
    from services.weather_bot.dates import parse_date_span

    start, days, requested_days, status = parse_date_span(
        "发布广州未来3天气象任务 2026-08-10",
        today=date(2026, 7, 27),
    )
    assert start == "2026-08-10"
    assert days == 3
    assert requested_days == 3
    assert status == "ok"


def test_task_bot_followup_never_inherits_weather_bot_location(
    monkeypatch,
    isolated_db_path,
) -> None:
    """The five-dimensional context key must include the addressed bot role."""
    _patch_isolated_memory(monkeypatch, isolated_db_path)
    service = CapturingForecastService()
    app = create_app(settings=_settings(), forecast_service=service)

    with TestClient(app) as client:
        weather_result = client.post(
            "/feishu/events/weather",
            json=_event("广州天气", message_id="cross-bot-weather-seed"),
        ).json()
        task_result = client.post(
            "/feishu/events/task",
            json=_event("明天呢", message_id="cross-bot-task-followup"),
        ).json()

    assert weather_result["status"] == "handled"
    assert len(service.requests) == 1
    assert task_result.get("mode") != "redirect"
    assert task_result["bot_role"] == "weather_task_bot"
