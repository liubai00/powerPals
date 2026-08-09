import json
import sqlite3

from services.weather_bot.data_minimization import minimize_submission_for_storage
from services.weather_bot.feishu import bitable_fields
from services.weather_bot.models import (
    AggregatedForecast,
    ExplanationProfile,
    ForecastPoint,
    ForecastSummary,
    ProviderForecast,
    WeatherPayload,
    WeatherSubmission,
    SubmissionRecord,
)
from services.weather_bot.storage import JsonlRecorder
from services.weather_bot import memory as weather_memory


def make_submission(*, retention_policy: str) -> WeatherSubmission:
    point = ForecastPoint(
        time="2026-08-10T12:00:00+08:00",
        temperature=34.0,
        precipitation_probability=40.0,
        wind_speed=5.0,
        cloud_cover=60.0,
    )
    summary = ForecastSummary(
        max_temperature=35.0,
        min_temperature=27.0,
        rain_probability=40.0,
        wind_speed=5.0,
        cloud_cover=60.0,
        main_weather="多云",
        high_risk_period="17:00-20:00",
    )
    return WeatherSubmission(
        task_id="storage-minimization-run",
        region="山东省",
        target_date="2026-08-10",
        data_cutoff_time="2026-08-09T16:00:00+08:00",
        provider_results=[
            ProviderForecast(
                provider="licensed_weather",
                points=[point],
                daily={"temperature_2m_max": [35.0]},
                raw={"secret_raw_shape": [1, 2, 3]},
                retrieved_at="2026-08-09T08:00:00+00:00",
                source_url="https://weather.example.test/v1/forecast",
                content_sha256="a" * 64,
                retention_policy=retention_policy,
                retention_expires_at="2026-08-12T08:00:00+00:00",
            )
        ],
        aggregated_forecast=AggregatedForecast(
            providers_used=["licensed_weather"],
            points=[point],
            summary=summary,
        ),
        payload=WeatherPayload(values=[point], summary=summary.model_dump(mode="json")),
        confidence={"score": 0.8, "description": "高"},
        key_factors=["高温使负荷天气压力代理升高"],
        risk_notes=["晚峰持续高温"],
        retention_policy=retention_policy,
        retention_expires_at="2026-08-12T08:00:00+00:00",
        explanation=ExplanationProfile(
            key_factors=["高温使负荷天气压力代理升高"],
            risk_notes=["晚峰持续高温"],
            business_readable_summary="仅为气象侧代理",
        ),
    )


def test_derived_only_storage_keeps_summary_but_drops_all_point_series_and_raw_shapes():
    original = make_submission(retention_policy="derived_only")

    stored = minimize_submission_for_storage(original)

    assert stored.aggregated_forecast.summary.max_temperature == 35.0
    assert stored.aggregated_forecast.points == []
    assert stored.payload.values == []
    assert stored.provider_results[0].points == []
    assert stored.provider_results[0].daily == {}
    assert stored.provider_results[0].raw is None
    assert original.aggregated_forecast.points  # input is not mutated
    serialized = stored.model_dump_json()
    assert "2026-08-10T12:00:00+08:00" not in serialized
    assert "secret_raw_shape" not in serialized


def test_metadata_only_storage_keeps_provenance_but_drops_all_weather_derivatives():
    stored = minimize_submission_for_storage(
        make_submission(retention_policy="metadata_only")
    )

    provider = stored.provider_results[0]
    assert provider.source_url == "https://weather.example.test/v1/forecast"
    assert provider.content_sha256 == "a" * 64
    assert provider.retention_policy == "metadata_only"
    assert stored.aggregated_forecast.points == []
    assert stored.aggregated_forecast.summary.max_temperature is None
    assert stored.aggregated_forecast.summary.main_weather == "未持久化（来源策略仅允许元数据）"
    assert stored.payload.values == []
    assert stored.payload.summary == {}
    assert stored.confidence == {}
    assert stored.key_factors == []
    assert stored.risk_notes == []
    assert stored.explanation.key_factors == []
    assert "35.0" not in stored.model_dump_json()


def test_metadata_only_external_geocoding_drops_persisted_coordinates_and_admin_derivatives():
    submission = make_submission(retention_policy="derived_only")
    submission.task_id = "WEATHER-CN-COORD-31_2345-121_4567-20260810-DAYAHEAD-001"
    submission.scope.location = {
        "name": "测试市",
        "code": "external-code",
        "latitude": 31.2345,
        "longitude": 121.4567,
        "province": "测试省",
        "city": "测试市",
        "source": "external_geo",
        "source_provider": "licensed_geocoder",
        "source_url": "https://geo.example.test/v1/search",
        "retrieved_at": "2026-08-09T08:00:00+00:00",
        "content_sha256": "b" * 64,
        "attribution": "Example Geo",
        "retention_policy": "metadata_only",
    }

    stored = minimize_submission_for_storage(submission)

    assert stored.scope.location == {
        "source": "external_geo",
        "source_provider": "licensed_geocoder",
        "source_url": "https://geo.example.test/v1/search",
        "retrieved_at": "2026-08-09T08:00:00+00:00",
        "content_sha256": "b" * 64,
        "attribution": "Example Geo",
        "retention_policy": "metadata_only",
    }
    serialized = stored.model_dump_json()
    assert "31.2345" not in serialized
    assert "121.4567" not in serialized
    assert "external-code" not in serialized
    assert "31_2345" not in serialized
    assert stored.task_id == "WEATHER-CN-EXTERNAL-GEO-bbbbbbbbbbbb-20260810-DAYAHEAD-001"


def test_bitable_json_payload_never_contains_point_series_or_raw_provider_shapes():
    fields = bitable_fields(make_submission(retention_policy="derived_only"))

    payload = json.loads(fields["json_payload"])
    assert payload["provider_results"][0]["points"] == []
    assert payload["provider_results"][0]["daily"] == {}
    assert payload["aggregated_forecast"]["points"] == []
    assert payload["payload"]["values"] == []
    assert "secret_raw_shape" not in fields["json_payload"]


def test_jsonl_submission_recorder_persists_only_minimized_submission(tmp_path):
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(str(path))

    recorder.append(
        SubmissionRecord(
            submission=make_submission(retention_policy="derived_only")
        )
    )

    payload = recorder.read_json_objects()[0]["submission"]
    assert payload["provider_results"][0]["points"] == []
    assert payload["aggregated_forecast"]["points"] == []
    assert payload["payload"]["values"] == []


def test_event_idempotency_ledger_stores_only_result_metadata(monkeypatch, tmp_path):
    db_path = tmp_path / "event-ledger.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(db_path))
    submission = make_submission(retention_policy="derived_only")
    assert weather_memory.claim_event("weather", "event-storage-min") is True

    weather_memory.complete_event(
        "weather",
        "event-storage-min",
        {
            "status": "handled",
            "bot_role": "weather_forecast_bot",
            "mode": "weather_query",
            "text": "用户原始回复不应进入幂等账本",
            "submissions": [submission.model_dump(mode="json")],
        },
    )

    with sqlite3.connect(db_path) as conn:
        stored_scope, stored_event_id, encoded = conn.execute(
            "SELECT bot_scope,event_id,response FROM event_ledger"
        ).fetchone()
    assert stored_scope.startswith("id-sha256:v1:")
    assert stored_event_id.startswith("id-sha256:v1:")
    assert stored_scope != "weather"
    assert stored_event_id != "event-storage-min"
    assert json.loads(encoded) == {
        "status": "handled",
        "bot_role": "weather_forecast_bot",
        "mode": "weather_query",
    }
    assert "用户原始回复" not in encoded
    assert "provider_results" not in encoded
