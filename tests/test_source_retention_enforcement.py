from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from services.weather_bot.config import Settings
from services.weather_bot.models import (
    ForecastPoint,
    ForecastRequest,
    ProviderForecast,
    SubmissionRecord,
)
from services.weather_bot.service import ForecastService
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry
from services.weather_bot.storage import JsonlRecorder


class RetainedExternalProvider:
    name = "test_weather"
    source_endpoints = ("https://weather.example.test/v1/forecast",)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=28.0,
                )
                for hour in range(24)
            ],
            retrieved_at="2026-08-09T00:00:00+00:00",
            provider_issued_at="2026-08-08T23:00:00+00:00",
            source_url=self.source_endpoints[0],
            content_sha256="f" * 64,
        )


def _verified_policy_values() -> dict[str, object]:
    return {
        "provider": "test_weather",
        "environment": "production",
        "profile": "retention-contract-test",
        "license_status": "verified",
        "allowed_uses": {"calculation", "derived_storage"},
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": ("https://weather.example.test/v1/forecast",),
        "unit_manifest": "temperature:degC",
        "required_metrics": ("temperature",),
        "coverage_model": "point",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 3600,
        "retention_policy": "derived_only",
    }


def test_verified_source_policy_requires_an_explicit_finite_retention() -> None:
    with pytest.raises(ValidationError, match="retention_seconds"):
        SourcePolicy(**_verified_policy_values())


def test_verified_source_policy_requires_an_explicit_retention_mode() -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 86_400
    values.pop("retention_policy")

    with pytest.raises(ValidationError, match="retention_policy"):
        SourcePolicy(**values)


async def test_forecast_propagates_the_policy_expiry_to_provider_and_submission() -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert submission.provider_results[0].retention_expires_at == (
        "2026-08-11T00:00:00+00:00"
    )
    assert submission.retention_policy == "derived_only"
    assert submission.retention_expires_at == "2026-08-11T00:00:00+00:00"


async def test_jsonl_submission_records_are_automatically_removed_at_expiry(
    tmp_path,
) -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )
    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )
    now = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(str(path), clock=lambda: now)

    recorder.append(SubmissionRecord(submission=submission))

    assert len(recorder.read_json_objects()) == 1
    now = datetime(2026, 8, 11, 0, 0, 1, tzinfo=timezone.utc)
    assert recorder.read_json_objects() == []
    assert path.read_text(encoding="utf-8") == ""


def test_jsonl_drops_legacy_submission_rows_without_an_expiry(tmp_path) -> None:
    path = tmp_path / "legacy-submissions.jsonl"
    legacy_external_row = {
        "submission": {
            "task_id": "legacy-unbounded-row",
            "provider_results": [
                {
                    "provider": "legacy_weather",
                    "points": [{"time": "2026-08-10T00:00:00+08:00"}],
                }
            ],
        }
    }
    internal_row = {"task_id": "internal-task-without-external-submission"}
    path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (legacy_external_row, internal_row)
        )
        + "\n",
        encoding="utf-8",
    )
    recorder = JsonlRecorder(
        str(path),
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    assert recorder.read_json_objects() == [internal_row]
    assert "legacy-unbounded-row" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "provider_expiry",
    [None, "not-a-time", "2026-08-12T00:00:00"],
)
async def test_jsonl_rejects_new_submission_when_used_provider_has_no_valid_expiry(
    tmp_path,
    provider_expiry,
) -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )
    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )
    submission.provider_results[0].retention_expires_at = provider_expiry
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(
        str(path),
        clock=lambda: datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="provider retention_expires_at"):
        recorder.append(SubmissionRecord(submission=submission))

    assert not path.exists()


async def test_jsonl_uses_the_earliest_used_provider_expiry_as_the_record_limit(
    tmp_path,
) -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )
    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )
    submission.provider_results[0].retention_expires_at = (
        "2026-08-10T00:00:00+00:00"
    )
    now = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(str(path), clock=lambda: now)

    recorder.append(SubmissionRecord(submission=submission))

    stored = recorder.read_json_objects()[0]["submission"]
    assert stored["retention_expires_at"] == "2026-08-10T00:00:00+00:00"
    now = datetime(2026, 8, 10, 0, 0, 1, tzinfo=timezone.utc)
    assert recorder.read_json_objects() == []


async def test_jsonl_rejects_a_new_submission_without_its_own_expiry(
    tmp_path,
) -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )
    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )
    submission.retention_expires_at = None
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(
        str(path),
        clock=lambda: datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="submission retention_expires_at"):
        recorder.append(SubmissionRecord(submission=submission))

    assert not path.exists()


@pytest.mark.parametrize(
    ("submission_mode", "provider_mode"),
    [
        ("metadata_only", "derived_only"),
        ("derived_only", "metadata_only"),
    ],
)
async def test_jsonl_honors_metadata_only_as_the_stricter_persistence_mode(
    tmp_path,
    submission_mode,
    provider_mode,
) -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )
    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )
    submission.retention_policy = submission_mode
    submission.provider_results[0].retention_policy = provider_mode
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(
        str(path),
        clock=lambda: datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc),
    )

    recorder.append(SubmissionRecord(submission=submission))

    stored = recorder.read_json_objects()[0]["submission"]
    assert stored["retention_policy"] == "metadata_only"
    assert stored["aggregated_forecast"]["summary"]["max_temperature"] is None
    assert stored["payload"]["summary"] == {}
    assert stored["confidence"] == {}


async def test_jsonl_rejects_a_new_submission_missing_used_provider_provenance(
    tmp_path,
) -> None:
    values = _verified_policy_values()
    values["retention_seconds"] = 172_800
    policy = SourcePolicy(**values)
    service = ForecastService(
        providers={"test_weather": RetainedExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )
    submission = await service.forecast(
        ForecastRequest(
            region="深圳",
            latitude=22.5431,
            longitude=114.0579,
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )
    submission.provider_results = []
    path = tmp_path / "submissions.jsonl"
    recorder = JsonlRecorder(
        str(path),
        clock=lambda: datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="used provider provenance"):
        recorder.append(SubmissionRecord(submission=submission))

    assert not path.exists()
