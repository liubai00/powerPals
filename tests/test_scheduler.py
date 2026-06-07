from datetime import date

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
