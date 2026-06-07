from datetime import date

from services.weather_bot.scheduler import build_daily_action_plan


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
