from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field


SHANGHAI_TZ = timezone(timedelta(hours=8))
TRACK_WEATHER = "weather_forecast"
REGION_SHENZHEN = "广东省深圳市"


class WeatherTaskRequest(BaseModel):
    target_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class WeatherTask(BaseModel):
    task_id: str
    track: str
    region: str
    target_date: str
    forecast_start: str
    forecast_end: str
    publish_time: str
    data_cutoff_time: str
    reminder_time: str
    submission_deadline: str
    status: Literal["draft", "published", "closed", "reviewed"] = "draft"
    task_card_message_id: str | None = None
    submission_format_version: str = "weather_submission_v1"
    scoring_status: Literal["not_started", "waiting_truth", "scored"] = "not_started"
    notes: str = ""


class WeatherTaskService:
    def create_dayahead_task(self, target_date: str) -> WeatherTask:
        target = date.fromisoformat(target_date)
        previous = target - timedelta(days=1)
        return WeatherTask(
            task_id=f"WEATHER-SZ-{target.strftime('%Y%m%d')}-DAYAHEAD-001",
            track=TRACK_WEATHER,
            region=REGION_SHENZHEN,
            target_date=target.isoformat(),
            forecast_start=_iso(target, 0, 0),
            forecast_end=_iso(target, 23, 0),
            publish_time=_iso(previous, 9, 0),
            data_cutoff_time=_iso(previous, 16, 0),
            reminder_time=_iso(previous, 16, 30),
            submission_deadline=_iso(previous, 17, 0),
            notes="共建是宗旨，共测是机制，评分是工具，复盘是方法，成长是结果。",
        )

    def publish(self, task: WeatherTask) -> WeatherTask:
        return task.model_copy(update={"status": "published"})

    def remind(self, task: WeatherTask) -> WeatherTask:
        return task.model_copy(
            update={
                "notes": (
                    f"{task.notes} 请参评 Bot 在 D-1 17:00 前提交，"
                    f"数据截止时间为 D-1 16:00，格式为 {task.submission_format_version}。"
                )
            }
        )

    def close(self, task: WeatherTask) -> WeatherTask:
        return task.model_copy(update={"status": "closed", "scoring_status": "waiting_truth"})


def _iso(day: date, hour: int, minute: int) -> str:
    return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=SHANGHAI_TZ).isoformat()
