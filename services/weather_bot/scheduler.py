from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from services.weather_bot.main import create_app
from services.weather_bot.models import ForecastRequest
from services.weather_bot.tasks import WeatherTaskRequest


SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ScheduledAction:
    name: str
    run_at: datetime
    target_date: str


def build_daily_action_plan(run_date: date) -> list[ScheduledAction]:
    target = run_date + timedelta(days=1)
    return [
        ScheduledAction("publish_task", _at(run_date, 9, 0), target.isoformat()),
        ScheduledAction("remind_task", _at(run_date, 16, 30), target.isoformat()),
        ScheduledAction("publish_forecast", _at(run_date, 17, 0), target.isoformat()),
        ScheduledAction("close_task", _at(run_date, 17, 5), target.isoformat()),
    ]


async def run_daily_publish_loop() -> None:
    app = create_app()
    publish = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/weather/publish")
    while True:
        await _sleep_until_next_publish()
        target = date.today() + timedelta(days=1)
        await publish(ForecastRequest(region="深圳", target_date=target.isoformat(), granularity="1h"))


async def run_community_rhythm_loop() -> None:
    app = create_app()
    endpoints = {getattr(route, "path", ""): route.endpoint for route in app.routes}
    while True:
        action = _next_action(datetime.now(SHANGHAI_TZ))
        await asyncio.sleep((action.run_at - datetime.now(SHANGHAI_TZ)).total_seconds())
        await _execute_action(action, endpoints)


async def _sleep_until_next_publish() -> None:
    now = datetime.now(SHANGHAI_TZ)
    next_run = datetime.combine(now.date(), time(hour=17), tzinfo=SHANGHAI_TZ)
    if now >= next_run:
        next_run += timedelta(days=1)
    await asyncio.sleep((next_run - now).total_seconds())


def _next_action(now: datetime) -> ScheduledAction:
    for action in build_daily_action_plan(now.date()):
        if action.run_at >= now:
            return action
    return build_daily_action_plan(now.date() + timedelta(days=1))[0]


async def _execute_action(action: ScheduledAction, endpoints: dict[str, Any]) -> None:
    if action.name == "publish_task":
        await endpoints["/api/tasks/weather/publish"](WeatherTaskRequest(target_date=action.target_date))
    elif action.name == "remind_task":
        await endpoints["/api/tasks/weather/remind"](WeatherTaskRequest(target_date=action.target_date))
    elif action.name == "publish_forecast":
        await endpoints["/api/weather/publish"](
            ForecastRequest(region="深圳", target_date=action.target_date, granularity="1h")
        )
    elif action.name == "close_task":
        await endpoints["/api/tasks/weather/close"](WeatherTaskRequest(target_date=action.target_date))


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=SHANGHAI_TZ)


if __name__ == "__main__":
    asyncio.run(run_community_rhythm_loop())
