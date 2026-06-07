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


def create_app(
    forecast_service: ForecastService | Any | None = None,
    feishu_verification_token: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    service = forecast_service or ForecastService()
    recorder = JsonlRecorder(settings.local_jsonl_path)
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

    @app.post("/feishu/events")
    async def feishu_events(payload: dict[str, Any]) -> dict[str, Any]:
        if not verify_feishu_token(payload, expected_token):
            raise HTTPException(status_code=403, detail="Invalid Feishu verification token")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        event = payload.get("event", {})
        text = _event_text(event)
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
    return any(command in text for command in ["明天深圳天气", "深圳气象预测", "今日气象任务", "帮助"])


def _request_from_text(text: str) -> ForecastRequest:
    import re

    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        target_date = match.group(0)
    else:
        target_date = (date.today() + timedelta(days=1)).isoformat()
    return ForecastRequest(region="深圳", target_date=target_date, granularity="1h")


app = create_app()
