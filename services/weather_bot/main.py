from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException

from services.weather_bot.cards import build_feishu_card
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient, verify_feishu_token
from services.weather_bot.location import BUILTIN_LOCATIONS, LocationResolver, location_slug
from services.weather_bot.models import ForecastRequest, SubmissionRecord, WeatherSubmission
from services.weather_bot.service import ForecastService
from services.weather_bot.storage import JsonlRecorder
from services.weather_bot.task_cards import build_task_card, build_task_text
from services.weather_bot.tasks import WeatherTask, WeatherTaskRequest, WeatherTaskService


def create_app(
    forecast_service: ForecastService | Any | None = None,
    feishu_verification_token: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    service = forecast_service or ForecastService()
    task_service = WeatherTaskService()
    location_resolver = LocationResolver(settings)
    recorder = JsonlRecorder(settings.local_jsonl_path)
    task_recorder = JsonlRecorder(settings.local_task_jsonl_path)
    feishu = FeishuClient(settings)
    expected_token = feishu_verification_token if feishu_verification_token is not None else settings.feishu_verification_token
    task_index: dict[str, WeatherTask] = {}

    app = FastAPI(title="PowerPals Weather Bot", version="0.3.0")

    def _cache_task(task: WeatherTask) -> WeatherTask:
        task_index[task.task_id] = task
        return task

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/weather/forecast", response_model=WeatherSubmission)
    async def forecast(request: ForecastRequest) -> WeatherSubmission:
        return await service.forecast(request)

    @app.post("/api/weather/forecast/range")
    async def forecast_range(request: ForecastRequest) -> dict[str, Any]:
        submissions = []
        start = date.fromisoformat(request.target_date)
        for offset in range(request.days):
            current = request.model_copy(update={"target_date": (start + timedelta(days=offset)).isoformat()})
            submissions.append((await service.forecast(current)).model_dump(mode="json"))
        return {
            "status": "ok",
            "region": submissions[0]["region"] if submissions else request.region,
            "start_date": request.target_date,
            "days": request.days,
            "submissions": submissions,
        }

    @app.post("/api/weather/submission")
    async def submission(submission: WeatherSubmission) -> dict[str, str]:
        recorder.append(SubmissionRecord(submission=submission))
        await feishu.write_bitable_record(submission)
        return {"status": "accepted", "task_id": submission.task_id}

    @app.post("/api/weather/publish")
    async def publish(request: ForecastRequest | None = None) -> dict[str, Any]:
        request = request or _tomorrow_request()
        result = await service.forecast(request)
        card = build_feishu_card(result)
        card_message_id = None
        if settings.feishu_default_chat_id:
            card_message_id = await feishu.send_interactive_card(settings.feishu_default_chat_id, card)
        recorder.append(SubmissionRecord(submission=result, card_message_id=card_message_id))
        await feishu.write_bitable_record(result, card_message_id)
        return {"status": "published", "submission": result.model_dump(mode="json"), "card": card}

    @app.post("/api/tasks/weather/create")
    async def create_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = _cache_task(task_service.create_dayahead_task(request.target_date, location))
        return task.model_dump(mode="json")

    @app.post("/api/tasks/weather/publish")
    async def publish_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = task_service.publish(task_service.create_dayahead_task(request.target_date, location))
        card = build_task_card(task)
        text = build_task_text(task)
        card_message_id = None
        if settings.feishu_default_chat_id:
            card_message_id = await feishu.send_interactive_card(settings.feishu_default_chat_id, card)
        task = _cache_task(task.model_copy(update={"task_card_message_id": card_message_id}))
        task_recorder.append(task)
        await feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json"), "card": card, "text": text}

    @app.post("/api/tasks/weather/remind")
    async def remind_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = task_service.remind(task_service.publish(task_service.create_dayahead_task(request.target_date, location)))
        card = build_task_card(task)
        if settings.feishu_default_chat_id:
            card_message_id = await feishu.send_interactive_card(settings.feishu_default_chat_id, card)
            task = task.model_copy(update={"task_card_message_id": card_message_id})
        task = _cache_task(task)
        task_recorder.append(task)
        await feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json"), "card": card, "text": build_task_text(task)}

    @app.post("/api/tasks/weather/close")
    async def close_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = _cache_task(task_service.close(task_service.publish(task_service.create_dayahead_task(request.target_date, location))))
        task_recorder.append(task)
        await feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json")}

    @app.get("/api/tasks/weather/{task_id}")
    async def get_weather_task(task_id: str) -> dict[str, Any]:
        import re

        cached = task_index.get(task_id)
        if cached:
            return cached.model_dump(mode="json")

        match = re.match(r"^WEATHER-CN-(.+)-(\d{4})(\d{2})(\d{2})-DAYAHEAD-001$", task_id)
        if not match:
            raise HTTPException(status_code=404, detail="Unknown weather task")
        location_token = match.group(1)
        target_date = f"{match.group(2)}-{match.group(3)}-{match.group(4)}"
        location = next((item for item in BUILTIN_LOCATIONS.values() if location_slug(item) == location_token), None)
        if not location:
            raise HTTPException(status_code=404, detail="Unknown weather task")
        task = task_service.create_dayahead_task(target_date, location)
        return task.model_dump(mode="json")

    @app.post("/feishu/events")
    async def feishu_events(payload: dict[str, Any]) -> dict[str, Any]:
        if not verify_feishu_token(payload, expected_token):
            raise HTTPException(status_code=403, detail="Invalid Feishu verification token")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        event = payload.get("event", {})
        text = _event_text(event)
        if _is_help_command(text):
            return {"status": "handled", "text": _help_text()}
        if _is_task_command(text):
            task_request = _task_request_from_text(text)
            location = await _resolve_task_location(location_resolver, task_request)
            task = _cache_task(task_service.publish(task_service.create_dayahead_task(task_request.target_date, location)))
            card = build_task_card(task)
            task_recorder.append(task)
            await feishu.write_task_bitable_record(task)
            return {"status": "handled", "task": task.model_dump(mode="json"), "card": card, "text": build_task_text(task)}
        if _is_weather_command(text):
            request = _request_from_text(text)
            if request.days > 1:
                submissions = []
                start = date.fromisoformat(request.target_date)
                for offset in range(request.days):
                    current = request.model_copy(update={"target_date": (start + timedelta(days=offset)).isoformat()})
                    submissions.append((await service.forecast(current)).model_dump(mode="json"))
                return {
                    "status": "handled",
                    "region": submissions[0]["region"] if submissions else request.region,
                    "days": request.days,
                    "submissions": submissions,
                }
            result = await service.forecast(request)
            return {"status": "handled", "card": build_feishu_card(result)}
        return {"status": "ignored"}

    return app


def _tomorrow_request() -> ForecastRequest:
    target = date.today() + timedelta(days=1)
    return ForecastRequest(target_date=target.isoformat(), granularity="1h")


def _event_text(event: dict[str, Any]) -> str:
    message = event.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _is_weather_command(text: str) -> bool:
    return any(keyword in text for keyword in ["天气", "气象预测", "预测"])


def _is_task_command(text: str) -> bool:
    return "气象任务" in text or ("气象" in text and "任务" in text)


def _is_help_command(text: str) -> bool:
    return "帮助" in text


def _request_from_text(text: str) -> ForecastRequest:
    target_date = _target_date_from_text(text)
    return ForecastRequest(
        region=_region_from_text(text),
        target_date=target_date,
        days=_days_from_text(text),
        granularity="1h",
    )


def _task_request_from_text(text: str) -> WeatherTaskRequest:
    return WeatherTaskRequest(region=_region_from_text(text), target_date=_target_date_from_text(text))


def _target_date_from_text(text: str) -> str:
    import re

    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    return (date.today() + timedelta(days=1)).isoformat()


def _days_from_text(text: str) -> int:
    import re

    digit_match = re.search(r"未来\s*(\d+)\s*天", text)
    if digit_match:
        return min(7, max(1, int(digit_match.group(1))))
    chinese_digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
    chinese_match = re.search(r"未来\s*([一二两三四五六七])\s*天", text)
    if chinese_match:
        return chinese_digits[chinese_match.group(1)]
    return 1


def _region_from_text(text: str) -> str:
    known_regions = sorted(BUILTIN_LOCATIONS.keys(), key=len, reverse=True)
    for region in known_regions:
        if region in text:
            return region
    return "广东省深圳市"


async def _resolve_task_location(location_resolver: LocationResolver, request: WeatherTaskRequest):
    return await location_resolver.resolve(
        ForecastRequest(
            region=request.region,
            latitude=request.latitude,
            longitude=request.longitude,
            location_code=request.location_code,
            location_source=request.location_source,
            target_date=request.target_date,
            granularity="1h",
        )
    )


def _help_text() -> str:
    return "\n".join(
        [
            "支持命令：",
            "@机器人 明天深圳天气",
            "@机器人 广州明天天气",
            "@机器人 北京气象预测 2026-06-10",
            "@机器人 广州未来三天天气",
            "@机器人 今日广州气象任务",
            "@机器人 帮助",
        ]
    )


app = create_app()
