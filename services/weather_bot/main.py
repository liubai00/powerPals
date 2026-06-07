from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException

from services.weather_bot.cards import build_feishu_card
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient, verify_feishu_token
from services.weather_bot.models import ForecastRequest, SubmissionRecord, WeatherSubmission
from services.weather_bot.service import ForecastService
from services.weather_bot.storage import JsonlRecorder
from services.weather_bot.task_cards import build_task_card, build_task_text
from services.weather_bot.tasks import WeatherTaskRequest, WeatherTaskService


def create_app(
    forecast_service: ForecastService | Any | None = None,
    feishu_verification_token: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    service = forecast_service or ForecastService()
    task_service = WeatherTaskService()
    recorder = JsonlRecorder(settings.local_jsonl_path)
    task_recorder = JsonlRecorder(settings.local_task_jsonl_path)
    feishu = FeishuClient(settings)
    expected_token = feishu_verification_token if feishu_verification_token is not None else settings.feishu_verification_token

    app = FastAPI(title="PowerPals Shenzhen Weather Bot", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/weather/forecast", response_model=WeatherSubmission)
    async def forecast(request: ForecastRequest) -> WeatherSubmission:
        return await service.forecast(request)

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
        task = task_service.create_dayahead_task(request.target_date)
        return task.model_dump(mode="json")

    @app.post("/api/tasks/weather/publish")
    async def publish_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        task = task_service.publish(task_service.create_dayahead_task(request.target_date))
        card = build_task_card(task)
        text = build_task_text(task)
        card_message_id = None
        if settings.feishu_default_chat_id:
            card_message_id = await feishu.send_interactive_card(settings.feishu_default_chat_id, card)
        task = task.model_copy(update={"task_card_message_id": card_message_id})
        task_recorder.append(task)
        await feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json"), "card": card, "text": text}

    @app.post("/api/tasks/weather/remind")
    async def remind_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        task = task_service.remind(task_service.publish(task_service.create_dayahead_task(request.target_date)))
        card = build_task_card(task)
        if settings.feishu_default_chat_id:
            card_message_id = await feishu.send_interactive_card(settings.feishu_default_chat_id, card)
            task = task.model_copy(update={"task_card_message_id": card_message_id})
        task_recorder.append(task)
        await feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json"), "card": card, "text": build_task_text(task)}

    @app.post("/api/tasks/weather/close")
    async def close_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        task = task_service.close(task_service.publish(task_service.create_dayahead_task(request.target_date)))
        task_recorder.append(task)
        await feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json")}

    @app.get("/api/tasks/weather/{task_id}")
    async def get_weather_task(task_id: str) -> dict[str, Any]:
        import re

        match = re.match(r"^WEATHER-SZ-(\d{4})(\d{2})(\d{2})-DAYAHEAD-001$", task_id)
        if not match:
            raise HTTPException(status_code=404, detail="Unknown weather task")
        target_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        task = task_service.create_dayahead_task(target_date)
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
            task = task_service.publish(task_service.create_dayahead_task(_target_date_from_text(text)))
            card = build_task_card(task)
            task_recorder.append(task)
            await feishu.write_task_bitable_record(task)
            return {"status": "handled", "task": task.model_dump(mode="json"), "card": card, "text": build_task_text(task)}
        if _is_weather_command(text):
            result = await service.forecast(_request_from_text(text))
            return {"status": "handled", "card": build_feishu_card(result)}
        return {"status": "ignored"}

    return app


def _tomorrow_request() -> ForecastRequest:
    target = date.today() + timedelta(days=1)
    return ForecastRequest(region="深圳", target_date=target.isoformat(), granularity="1h")


def _event_text(event: dict[str, Any]) -> str:
    message = event.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _is_weather_command(text: str) -> bool:
    return any(command in text for command in ["明天深圳天气", "深圳气象预测", "深圳天气"])


def _is_task_command(text: str) -> bool:
    return any(command in text for command in ["今日气象任务", "发布深圳气象任务", "深圳气象任务"])


def _is_help_command(text: str) -> bool:
    return "帮助" in text


def _request_from_text(text: str) -> ForecastRequest:
    target_date = _target_date_from_text(text)
    return ForecastRequest(region="深圳", target_date=target_date, granularity="1h")


def _target_date_from_text(text: str) -> str:
    import re

    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    return (date.today() + timedelta(days=1)).isoformat()


def _help_text() -> str:
    return "\n".join(
        [
            "支持命令：",
            "@机器人 明天深圳天气",
            "@机器人 深圳气象预测 2026-06-10",
            "@机器人 今日气象任务",
            "@机器人 帮助",
        ]
    )


app = create_app()
