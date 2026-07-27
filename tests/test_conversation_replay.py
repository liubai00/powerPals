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
    ForecastPoint,
    ForecastRequest,
    ForecastSummary,
    WeatherSubmission,
)


class CapturingForecastService:
    def __init__(self) -> None:
        self.requests: list[ForecastRequest] = []
        self.failures_remaining = 0

    async def forecast(self, request: ForecastRequest) -> WeatherSubmission:
        self.requests.append(request)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("simulated weather provider failure")
        return WeatherSubmission(
            task_id=f"task-{len(self.requests)}",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time="2026-07-27T08:00:00+08:00",
            provider_results=[],
            aggregated_forecast=AggregatedForecast(
                providers_used=["test"],
                points=[
                    ForecastPoint(
                        time=f"{request.target_date}T12:00:00+08:00",
                        temperature=25.0,
                        precipitation_probability=10.0,
                        wind_speed=2.0,
                        cloud_cover=20.0,
                    )
                ],
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
        llm_api_key=None,
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
    assert result["coverage"] == "11/11"
    assert result["card"]["card"]["header"]["title"]["content"].startswith("⚡ 电力气象决策晨报 2.0")
    assert len(service.requests) == 22


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("生成今天的电力气象决策晨报 2.0", True),
        ("今天的电力气象晨报", True),
        ("预览晨报2.0", True),
        ("电力气象晨报应该包含什么", False),
        ("晨报有哪些业务指标", False),
    ],
)
def test_power_briefing_intent_requires_a_generation_request(text: str, expected: bool) -> None:
    assert _is_power_briefing_command(text) is expected


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
