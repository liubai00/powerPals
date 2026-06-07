from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from services.weather_bot.cards import build_feishu_card
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient, verify_feishu_token
from services.weather_bot.judge import WeatherJudgeRequest, WeatherJudgeResult, score_weather_submission
from services.weather_bot.location import BUILTIN_LOCATIONS, FavoriteLocation, LocationBook, LocationResolver, location_slug
from services.weather_bot.models import ForecastRequest, SubmissionRecord, WeatherSubmission
from services.weather_bot.service import ForecastService
from services.weather_bot.storage import JsonlRecorder
from services.weather_bot.task_cards import build_task_card, build_task_text
from services.weather_bot.tasks import WeatherTask, WeatherTaskRequest, WeatherTaskService
from services.weather_bot.workbench import (
    HydrologyRecord,
    NewsItem,
    WeatherBatchRequest,
    collect_forecasts_with_errors,
    hydrology_csv,
    weather_csv,
    weather_report_html,
)


def create_app(
    forecast_service: ForecastService | Any | None = None,
    feishu_verification_token: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    service = forecast_service or ForecastService()
    task_service = WeatherTaskService()
    location_resolver = LocationResolver(settings)
    location_book = LocationBook(settings)
    recorder = JsonlRecorder(settings.local_jsonl_path)
    task_recorder = JsonlRecorder(settings.local_task_jsonl_path)
    news_recorder = JsonlRecorder(settings.local_news_jsonl_path)
    hydrology_recorder = JsonlRecorder(settings.local_hydrology_jsonl_path)
    feishu = FeishuClient(settings)
    expected_token = feishu_verification_token if feishu_verification_token is not None else settings.feishu_verification_token
    task_index: dict[str, WeatherTask] = {}

    app = FastAPI(title="PowerPals Weather Data Workbench", version="0.5.0")

    def _cache_task(task: WeatherTask) -> WeatherTask:
        task_index[task.task_id] = task
        return task

    def _load_task_from_local_log(task_id: str) -> WeatherTask | None:
        for payload in reversed(task_recorder.read_json_objects()):
            if payload.get("task_id") != task_id:
                continue
            try:
                return _cache_task(WeatherTask.model_validate(payload))
            except ValueError:
                continue
        return None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/weather/forecast", response_model=WeatherSubmission)
    async def forecast(request: ForecastRequest) -> WeatherSubmission:
        request = _apply_favorite_alias(request, location_book)
        return await service.forecast(request)

    @app.post("/api/weather/forecast/range")
    async def forecast_range(request: ForecastRequest) -> dict[str, Any]:
        request = _apply_favorite_alias(request, location_book)
        collected, errors = await collect_forecasts_with_errors(service, request)
        submissions = [submission.model_dump(mode="json") for submission in collected]
        return {
            "status": "partial" if errors else "ok",
            "region": submissions[0]["region"] if submissions else request.region,
            "start_date": request.target_date,
            "days": request.days,
            "submissions": submissions,
            "errors": errors,
        }

    @app.post("/api/weather/batch")
    async def forecast_batch(request: WeatherBatchRequest) -> dict[str, Any]:
        submissions = []
        for item in request.requests:
            current = _apply_favorite_alias(item, location_book)
            submissions.append((await service.forecast(current)).model_dump(mode="json"))
        return {"status": "ok", "count": len(submissions), "submissions": submissions}

    @app.post("/api/weather/export")
    async def export_weather(request: ForecastRequest) -> Response:
        request = _apply_favorite_alias(request, location_book)
        submissions, _errors = await collect_forecasts_with_errors(service, request)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        csv_text = weather_csv(submissions)
        filename = f"powerpals-weather-{request.target_date}-{request.days}d.csv"
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/weather/export")
    async def export_weather_get(region: str, target_date: str, days: int = 1) -> Response:
        return await export_weather(ForecastRequest(region=region, target_date=target_date, days=days))

    @app.get("/reports/weather", response_class=HTMLResponse)
    async def weather_report(region: str, target_date: str, days: int = 1) -> HTMLResponse:
        request = _apply_favorite_alias(ForecastRequest(region=region, target_date=target_date, days=days), location_book)
        submissions, errors = await collect_forecasts_with_errors(service, request)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        html = weather_report_html(
            submissions,
            {"region": region, "target_date": target_date, "days": days},
            errors,
        )
        return HTMLResponse(content=html)

    @app.post("/api/weather/submission")
    async def submission(submission: WeatherSubmission) -> dict[str, str]:
        recorder.append(SubmissionRecord(submission=submission))
        await feishu.write_bitable_record(submission)
        return {"status": "accepted", "task_id": submission.task_id}

    @app.get("/api/locations")
    async def list_locations() -> dict[str, Any]:
        locations = [item.model_dump(mode="json") for item in location_book.list()]
        return {"status": "ok", "count": len(locations), "locations": locations}

    @app.post("/api/locations")
    async def create_location(location: FavoriteLocation) -> dict[str, Any]:
        saved = location_book.upsert(location)
        return {"status": "saved", "location": saved.model_dump(mode="json")}

    @app.delete("/api/locations/{alias}")
    async def delete_location(alias: str) -> dict[str, Any]:
        deleted = location_book.delete(alias)
        return {"status": "deleted" if deleted else "not_found", "alias": alias}

    @app.post("/api/news/items")
    async def create_news_item(item: NewsItem) -> dict[str, Any]:
        news_recorder.append(item)
        return {"status": "accepted", "item": item.model_dump(mode="json")}

    @app.get("/api/news/digest")
    async def news_digest() -> dict[str, Any]:
        items = news_recorder.read_json_objects()
        return {"status": "ok", "count": len(items), "items": list(reversed(items))}

    @app.post("/api/hydrology/records")
    async def create_hydrology_record(record: HydrologyRecord) -> dict[str, Any]:
        hydrology_recorder.append(record)
        return {"status": "accepted", "record": record.model_dump(mode="json")}

    @app.get("/api/hydrology/records")
    async def list_hydrology_records() -> dict[str, Any]:
        records = hydrology_recorder.read_json_objects()
        return {"status": "ok", "count": len(records), "records": list(reversed(records))}

    @app.get("/api/hydrology/export")
    async def export_hydrology_records() -> Response:
        return Response(
            content="\ufeff" + hydrology_csv(hydrology_recorder.read_json_objects()),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="powerpals-hydrology.csv"'},
        )

    @app.get("/api/data/export/catalog")
    async def export_catalog() -> dict[str, Any]:
        return {
            "status": "ok",
            "exports": [
                {"name": "weather_csv", "path": "/api/weather/export", "format": "csv"},
                {"name": "hydrology_csv", "path": "/api/hydrology/export", "format": "csv"},
                {"name": "weather_report", "path": "/reports/weather", "format": "html"},
            ],
        }

    @app.post("/api/judge/weather/score", response_model=WeatherJudgeResult)
    async def judge_weather_score(request: WeatherJudgeRequest) -> WeatherJudgeResult:
        return score_weather_submission(request)

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
        stored = _load_task_from_local_log(task_id)
        if stored:
            return stored.model_dump(mode="json")

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
            return {
                "status": "handled",
                "bot_role": "weather_task_bot",
                "task": task.model_dump(mode="json"),
                "card": card,
                "text": build_task_text(task),
            }
        if _is_weather_command(text):
            request = _request_from_text(text)
            request = _apply_favorite_alias(request, location_book)
            report_url = _public_weather_report_url(settings, request)
            download_url = _public_weather_download_url(settings, request)
            if request.days > 1:
                submissions, errors = await collect_forecasts_with_errors(service, request)
                card = build_feishu_card(submissions[0], report_url=report_url, download_url=download_url) if submissions else None
                return {
                    "status": "partial" if errors else "handled",
                    "bot_role": "weather_forecast_bot",
                    "region": submissions[0].region if submissions else request.region,
                    "days": request.days,
                    "report_url": report_url,
                    "download_url": download_url,
                    "card": card,
                    "submissions": [submission.model_dump(mode="json") for submission in submissions],
                    "errors": errors,
                }
            result = await service.forecast(request)
            return {
                "status": "handled",
                "bot_role": "weather_forecast_bot",
                "report_url": report_url,
                "download_url": download_url,
                "card": build_feishu_card(result, report_url=report_url, download_url=download_url),
            }
        return {"status": "ignored"}

    return app


def _tomorrow_request() -> ForecastRequest:
    target = date.today() + timedelta(days=1)
    return ForecastRequest(target_date=target.isoformat(), granularity="1h")


def _apply_favorite_alias(request: ForecastRequest, location_book: LocationBook) -> ForecastRequest:
    if request.latitude is not None or request.longitude is not None:
        return request
    favorite = location_book.resolve(request.region)
    if not favorite:
        return request
    return request.model_copy(
        update={
            "region": favorite.name,
            "latitude": favorite.latitude,
            "longitude": favorite.longitude,
            "location_code": favorite.code,
            "location_source": favorite.source,
        }
    )


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
    coordinates = _coordinates_from_text(text)
    if coordinates:
        latitude, longitude = coordinates
        return ForecastRequest(
            region=_coordinate_region(latitude, longitude),
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            days=_days_from_text(text),
            granularity="1h",
        )
    return ForecastRequest(
        region=_region_from_text(text),
        target_date=target_date,
        days=_days_from_text(text),
        granularity="1h",
    )


def _task_request_from_text(text: str) -> WeatherTaskRequest:
    coordinates = _coordinates_from_text(text)
    if coordinates:
        latitude, longitude = coordinates
        return WeatherTaskRequest(
            region=_coordinate_region(latitude, longitude),
            latitude=latitude,
            longitude=longitude,
            target_date=_target_date_from_text(text),
        )
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
        return min(16, max(1, int(digit_match.group(1))))
    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
        "十三": 13,
        "十四": 14,
        "十五": 15,
        "十六": 16,
    }
    chinese_match = re.search(r"未来\s*(十六|十五|十四|十三|十二|十一|十|[一二两三四五六七八九])\s*天", text)
    if chinese_match:
        return chinese_digits[chinese_match.group(1)]
    return 1


def _region_from_text(text: str) -> str:
    known_regions = sorted(BUILTIN_LOCATIONS.keys(), key=len, reverse=True)
    for region in known_regions:
        if region in text:
            return region
    return "广东省深圳市"


def _coordinates_from_text(text: str) -> tuple[float, float] | None:
    import re

    pair_match = re.search(r"(-?\d{1,2}(?:\.\d+)?)\s*[,，]\s*(-?\d{1,3}(?:\.\d+)?)", text)
    if pair_match:
        return _validated_coordinates(float(pair_match.group(1)), float(pair_match.group(2)))

    named_match = re.search(r"北纬\s*(\d{1,2}(?:\.\d+)?)\D+东经\s*(\d{1,3}(?:\.\d+)?)", text)
    if named_match:
        return _validated_coordinates(float(named_match.group(1)), float(named_match.group(2)))

    return None


def _validated_coordinates(latitude: float, longitude: float) -> tuple[float, float] | None:
    if -90 <= latitude <= 90 and -180 <= longitude <= 180:
        return latitude, longitude
    return None


def _coordinate_region(latitude: float, longitude: float) -> str:
    return f"经纬度 {latitude:.4f},{longitude:.4f}"


def _public_weather_report_url(settings: Settings, request: ForecastRequest) -> str | None:
    if not settings.public_base_url:
        return None
    query = urlencode({"region": request.region, "target_date": request.target_date, "days": request.days})
    return f"{settings.public_base_url.rstrip('/')}/reports/weather?{query}"


def _public_weather_download_url(settings: Settings, request: ForecastRequest) -> str | None:
    if not settings.public_base_url:
        return None
    query = urlencode({"region": request.region, "target_date": request.target_date, "days": request.days})
    return f"{settings.public_base_url.rstrip('/')}/api/weather/export?{query}"


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
            "全国气象预测机器人：负责城市、地区、经纬度天气预测，不发布共测任务。",
            "@机器人 广州明天天气",
            "@机器人 北京气象预测 2026-06-10",
            "@机器人 广州未来三天天气",
            "@机器人 22.8016,113.5252 明天天气",
            "",
            "气象任务发布机器人：负责发布、提醒、关闭和记录气象共测任务，不计算天气。",
            "@机器人 今日广州气象任务",
            "@机器人 22.8016,113.5252 今日气象任务",
            "@机器人 帮助",
        ]
    )


app = create_app()
