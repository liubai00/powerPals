from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import create_app
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ForecastWindow,
    ProviderForecast,
    ScopeProfile,
    TimeInfo,
    WeatherSubmission,
)


class _VersionedTraceableForecastService:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.region_runs: dict[str, int] = {}

    async def forecast(self, request) -> WeatherSubmission:
        self.calls.append(request)
        run_number = self.region_runs.get(request.region, 0) + 1
        self.region_runs[request.region] = run_number
        is_guangdong = "广东" in request.region
        points_a = []
        points_b = []
        for hour in range(24):
            ramp = 4.0
            if hour == 15:
                ramp = 8.0
            elif hour >= 16:
                ramp = 12.0
            cloud_a = 10.0 if is_guangdong and hour == 11 else 30.0
            cloud_b = 95.0 if is_guangdong and hour == 11 else 34.0
            rain_a = 10.0
            rain_b = 90.0 if is_guangdong and hour == 11 else 15.0
            version_delta = 3.0 if run_number >= 2 and hour == 17 else 0.0
            base = 32.0 + version_delta
            points_a.append(
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=base,
                    apparent_temperature=base + 1,
                    precipitation_probability=rain_a,
                    cloud_cover=cloud_a,
                    wind_speed=ramp,
                    wind_direction=90,
                )
            )
            points_b.append(
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=base + 1,
                    apparent_temperature=base + 2,
                    precipitation_probability=rain_b,
                    cloud_cover=cloud_b,
                    wind_speed=ramp + (5 if is_guangdong and hour == 11 else 0.5),
                    wind_direction=100,
                )
            )
        retrieved = f"2026-08-0{7 + run_number}T08:00:00+08:00"
        providers = (
            ProviderForecast(
                provider="source_a",
                status="ok",
                points=points_a,
                retrieved_at=retrieved,
                provider_issued_at=retrieved,
                source_url="https://official.example.test/source-a/forecast",
                content_sha256="a" * 64,
            ),
            ProviderForecast(
                provider="source_b",
                status="ok",
                points=points_b,
                retrieved_at=retrieved,
                provider_issued_at=retrieved,
                source_url="https://official.example.test/source-b/forecast",
                content_sha256="b" * 64,
            ),
        )
        return WeatherSubmission(
            task_id=f"task-{request.region}-{run_number}",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time="2026-08-09T16:00:00+08:00",
            scope=ScopeProfile(
                region=request.region,
                target_date=request.target_date,
                location={"representation": "province_representative_point"},
            ),
            time_info=TimeInfo(
                retrieved_at=retrieved,
                provider_issued_at={"source_a": retrieved, "source_b": retrieved},
                aggregation_completed_at="2026-08-09T08:00:01+08:00",
                valid_time=ForecastWindow(
                    start=f"{request.target_date}T00:00:00+08:00",
                    end=f"{request.target_date}T23:00:00+08:00",
                    timezone="Asia/Shanghai",
                ),
                forecast_run_id=f"run-{request.region}-{run_number}",
                business_submission_deadline="2026-08-09T16:00:00+08:00",
            ),
            provider_results=list(providers),
            aggregated_forecast=AggregatedForecast(
                providers_used=["source_a", "source_b"],
                points=points_a,
                summary=ForecastSummary(
                    max_temperature=max(point.temperature or 0 for point in points_a),
                    min_temperature=min(point.temperature or 0 for point in points_a),
                    main_weather="测试",
                    high_risk_period="测试",
                ),
            ),
            confidence={"score": 0.7, "description": "中等"},
            key_factors=[],
            risk_notes=[],
        )


def _event(text: str) -> dict:
    token = uuid4().hex
    return {
        "header": {"event_id": token, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "risk-user"}},
            "message": {
                "message_id": f"message-{token}",
                "chat_id": "risk-private",
                "chat_type": "p2p",
                "message_type": "text",
                "content": {"text": text},
            },
        },
    }


def _client(monkeypatch, tmp_path, service):
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "memory.db"))

    async def fake_send(*_args, **_kwargs) -> str:
        return "direct-reply"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", fake_send)
    return TestClient(
        create_app(
            settings=Settings(
                app_env="test",
                feishu_weather_bot_open_id="ou_weather_bot",
                subscriptions_db=str(tmp_path / "subscriptions.db"),
                power_briefing_cache_db=str(tmp_path / "briefing.db"),
            ),
            forecast_service=service,
        )
    )


def _post(client: TestClient, text: str) -> dict:
    response = client.post("/feishu/events/weather", json=_event(text))
    assert response.status_code == 200
    return response.json()


def test_wind_ramp_query_returns_a_continuous_ten_meter_proxy_window(
    monkeypatch,
    tmp_path,
) -> None:
    service = _VersionedTraceableForecastService()
    with _client(monkeypatch, tmp_path, service) as client:
        result = _post(client, "甘肃明天风电爬坡风险")

    assert result["status"] == "handled"
    assert result["mode"] == "wind_ramp_weather_proxy"
    assert "14:00" in result["text"] and "16:00" in result["text"]
    assert "10米地面风快速变化代理" in result["text"]
    assert "实际风电爬坡" in result["text"]
    assert len(service.calls) == 1


def test_disagreement_and_confidence_queries_explain_objective_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    service = _VersionedTraceableForecastService()
    with _client(monkeypatch, tmp_path, service) as client:
        disagreement = _post(client, "广东明天两个数据源分歧在哪里")
        confidence = _post(client, "山东明天为什么置信度中等")

    assert disagreement["mode"] == "weather_source_disagreement"
    assert "cloud_cover" in disagreement["text"]
    assert "11:00" in disagreement["text"]
    assert "source_a" in disagreement["text"] and "source_b" in disagreement["text"]
    assert "多数源必然正确" in disagreement["text"]
    assert confidence["mode"] == "weather_confidence_explanation"
    assert all(word in confidence["text"] for word in ("覆盖", "分歧", "时效", "代表点"))
    assert "大模型主观补分" in confidence["text"]


def test_version_query_compares_two_distinct_runs_at_the_same_valid_hour(
    monkeypatch,
    tmp_path,
) -> None:
    service = _VersionedTraceableForecastService()
    with _client(monkeypatch, tmp_path, service) as client:
        _post(client, "山东明日晚峰负荷压力")
        comparison = _post(client, "山东明日晚峰和昨天8点预报相比变了什么")

    assert comparison["mode"] == "weather_forecast_version_change", comparison
    assert comparison["current_run_id"] != comparison["previous_run_id"]
    assert "17:00" in comparison["text"]
    assert "temperature" in comparison["text"]
    assert "+3" in comparison["text"]
    assert "同一有效时刻" in comparison["text"]


def test_national_complexity_query_ranks_only_available_traceable_runs(
    monkeypatch,
    tmp_path,
) -> None:
    service = _VersionedTraceableForecastService()
    with _client(monkeypatch, tmp_path, service) as client:
        _post(client, "山东明天天气")
        _post(client, "广东明天天气")
        ranking = _post(client, "哪个省明天新能源预测偏差风险最大")

    assert ranking["mode"] == "renewable_forecast_complexity_weather_proxy"
    assert ranking["ranking"][0]["region"].startswith("广东")
    assert "新能源预测复杂度气象代理" in ranking["text"]
    assert "实际新能源预测偏差" in ranking["text"]
    assert len(service.calls) == 2
