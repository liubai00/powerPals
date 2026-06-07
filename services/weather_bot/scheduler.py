from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone

from services.weather_bot.main import create_app
from services.weather_bot.models import ForecastRequest


SHANGHAI_TZ = timezone(timedelta(hours=8))


async def run_daily_publish_loop() -> None:
    app = create_app()
    publish = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/weather/publish")
    while True:
        await _sleep_until_next_publish()
        target = date.today() + timedelta(days=1)
        await publish(ForecastRequest(region="深圳", target_date=target.isoformat(), granularity="1h"))


async def _sleep_until_next_publish() -> None:
    now = datetime.now(SHANGHAI_TZ)
    next_run = datetime.combine(now.date(), time(hour=17), tzinfo=SHANGHAI_TZ)
    if now >= next_run:
        next_run += timedelta(days=1)
    await asyncio.sleep((next_run - now).total_seconds())


if __name__ == "__main__":
    asyncio.run(run_daily_publish_loop())
