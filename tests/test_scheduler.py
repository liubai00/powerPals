import asyncio
from contextlib import suppress
from datetime import date

import pytest

from services.weather_bot import scheduler
from services.weather_bot.config import Settings
from services.weather_bot.scheduler import build_daily_action_plan
from services.weather_bot.scheduler import _forecast_request, _task_request


def test_build_daily_action_plan_follows_powerpals_weather_rhythm():
    plan = build_daily_action_plan(date(2026, 6, 9))

    assert [action.name for action in plan] == [
        "publish_task",
        "remind_task",
        "publish_forecast",
        "close_task",
    ]
    assert [action.run_at.isoformat() for action in plan] == [
        "2026-06-09T09:00:00+08:00",
        "2026-06-09T16:30:00+08:00",
        "2026-06-09T17:00:00+08:00",
        "2026-06-09T17:05:00+08:00",
    ]
    assert {action.target_date for action in plan} == {"2026-06-10"}


def test_scheduler_uses_configured_default_region_for_tasks_and_forecasts():
    settings = Settings(default_weather_region="广州")

    task_request = _task_request("2026-06-10", settings)
    forecast_request = _forecast_request("2026-06-10", settings)

    assert task_request.region == "广州"
    assert forecast_request.region == "广州"
    assert task_request.latitude is None
    assert forecast_request.longitude is None


def test_scheduler_uses_configured_default_coordinates():
    settings = Settings(
        default_weather_region="广州南沙",
        default_weather_latitude=22.8016,
        default_weather_longitude=113.5252,
    )

    task_request = _task_request("2026-06-10", settings)
    forecast_request = _forecast_request("2026-06-10", settings)

    assert task_request.region == "广州南沙"
    assert task_request.latitude == 22.8016
    assert task_request.longitude == 113.5252
    assert forecast_request.latitude == 22.8016
    assert forecast_request.longitude == 113.5252


@pytest.mark.asyncio
async def test_legacy_weather_scheduler_is_inert_by_default_even_when_global_send_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, global_feishu_send_enabled=True)
    app_creations: list[str] = []

    class InertApp:
        routes: list[object] = []

    monkeypatch.setattr(scheduler, "Settings", lambda: settings)
    monkeypatch.setattr(
        scheduler,
        "create_app",
        lambda: app_creations.append("created") or InertApp(),
    )

    task = asyncio.create_task(scheduler.run_community_rhythm_loop())
    try:
        await asyncio.sleep(0)
        assert app_creations == []
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_legacy_daily_publish_loop_uses_the_same_independent_default_off_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, global_feishu_send_enabled=True)
    app_creations: list[str] = []

    class InertApp:
        routes: list[object] = []

    monkeypatch.setattr(scheduler, "Settings", lambda: settings)
    monkeypatch.setattr(
        scheduler,
        "create_app",
        lambda: app_creations.append("created") or InertApp(),
    )

    task = asyncio.create_task(scheduler.run_daily_publish_loop())
    try:
        await asyncio.sleep(0)
        assert app_creations == []
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await task
