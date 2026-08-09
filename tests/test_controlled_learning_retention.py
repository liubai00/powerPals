from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from services.weather_bot.controlled_learning import (
    ControlledLearningStore,
    ObservedWeather,
    ProviderScore,
    verify_due_snapshots,
)
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ProviderForecast,
    ScopeProfile,
    TimeInfo,
    WeatherSubmission,
)


def _submission_with_hourly_external_data() -> WeatherSubmission:
    points = [
        ForecastPoint(
            time=f"2026-08-10T{hour:02d}:00:00+08:00",
            temperature=18.0 + hour,
            precipitation_probability=70.0 if hour == 17 else 10.0,
            wind_speed=2.0 + (hour / 10),
            cloud_cover=float(hour),
        )
        for hour in range(24)
    ]
    provider = ProviderForecast(
        provider="open_meteo",
        points=points,
        daily={"third_party_normalized_series": [f"external-{hour}" for hour in range(24)]},
        retrieved_at="2026-08-09T00:05:00+00:00",
        provider_issued_at="2026-08-09T00:00:00+00:00",
        source_url="https://api.open-meteo.com/v1/forecast",
        content_sha256="a" * 64,
        retention_policy="derived_only",
    )
    return WeatherSubmission(
        task_id="snapshot-minimization",
        region="广东省广州市",
        target_date="2026-08-10",
        data_cutoff_time="2026-08-09T08:00:00+08:00",
        scope=ScopeProfile(
            region="广东省广州市",
            target_date="2026-08-10",
            location={"latitude": 23.1291, "longitude": 113.2644},
        ),
        time_info=TimeInfo(
            forecast_run_id="run-open-meteo-20260809-0800",
            valid_time={
                "start": "2026-08-10T00:00:00+08:00",
                "end": "2026-08-10T23:00:00+08:00",
            },
        ),
        provider_results=[provider],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo"],
            points=points,
            summary=ForecastSummary(
                max_temperature=41.0,
                min_temperature=18.0,
                rain_probability=70.0,
                wind_speed=4.3,
                cloud_cover=23.0,
                main_weather="演示",
                high_risk_period="演示",
            ),
        ),
        confidence={"score": 0.8},
        key_factors=[],
        risk_notes=[],
    )


def test_record_snapshot_persists_only_daily_features_and_provenance(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))

    snapshot_id = store.record_forecast_snapshot(
        _submission_with_hourly_external_data(),
        captured_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    rows = store.pending_snapshots(datetime(2026, 8, 10).date())
    assert [row["id"] for row in rows] == [snapshot_id]
    payload = json.loads(rows[0]["submission_json"])
    assert payload == {
        "schema_version": "forecast_daily_features_v2",
        "task_id": "snapshot-minimization",
        "region": "广东省广州市",
        "target_date": "2026-08-10",
        "forecast_run_id": "run-open-meteo-20260809-0800",
        "valid_time": {
            "start": "2026-08-10T00:00:00+08:00",
            "end": "2026-08-10T23:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "provider_daily_features": [
            {
                "provider": "open_meteo",
                "status": "ok",
                "max_temperature": 41.0,
                "min_temperature": 18.0,
                "rain_forecast": True,
                "wind_speed_max": 4.3,
                "retrieved_at": "2026-08-09T00:05:00+00:00",
                "provider_issued_at": "2026-08-09T00:00:00+00:00",
                "source_url": "https://api.open-meteo.com/v1/forecast",
                "content_sha256": "a" * 64,
                "retention_policy": "derived_only",
            }
        ],
    }
    database_bytes = (tmp_path / "learning.db").read_bytes()
    assert b"third_party_normalized_series" not in database_bytes
    assert b"external-23" not in database_bytes
    assert b"2026-08-10T17:00:00+08:00" not in database_bytes


@pytest.mark.asyncio
async def test_minimized_snapshot_remains_objectively_verifiable(tmp_path: Path):
    class IndependentTruthClient:
        async def fetch(
            self,
            latitude: float,
            longitude: float,
            target_date: str,
        ) -> ObservedWeather:
            return ObservedWeather(
                target_date=target_date,
                max_temperature=40.0,
                min_temperature=19.0,
                precipitation_sum=1.0,
                rain_observed=True,
                wind_speed=4.0,
                source="independent_official_reference",
                fetched_at="2026-08-12T00:00:00+00:00",
                dependent_provider_ids=[],
            )

    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    store.record_forecast_snapshot(
        _submission_with_hourly_external_data(),
        captured_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    result = await verify_due_snapshots(
        store,
        IndependentTruthClient(),
        today=date(2026, 8, 12),
        truth_delay_days=1,
    )

    assert result == {"due": 1, "evaluated": 1, "deferred": 0, "skipped": 0}
    assert store.provider_summary() == [
        {
            "provider": "open_meteo",
            "region": "广东省广州市",
            "horizon_days": 1,
            "sample_count": 1,
            "temperature_mae": 1.0,
            "rain_accuracy": 1.0,
            "wind_speed_mae": 0.3,
            "total_score": 94.6,
        }
    ]


def test_snapshot_retention_purge_is_bounded_by_default_and_removes_scores(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    old_snapshot_id = store.record_forecast_snapshot(
        _submission_with_hourly_external_data(),
        captured_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    recent_snapshot_id = store.record_forecast_snapshot(
        _submission_with_hourly_external_data(),
        captured_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert old_snapshot_id is not None
    assert recent_snapshot_id is not None
    store.mark_snapshot_evaluated(
        old_snapshot_id,
        [
            ProviderScore(
                provider="open_meteo",
                region="广东省广州市",
                target_date="2026-08-10",
                horizon_days=101,
                temperature_mae=1.0,
                rain_hit=True,
                wind_speed_error=0.3,
                total_score=94.6,
                truth_source="independent_official_reference",
            )
        ],
        "independent_official_reference",
    )

    result = store.purge_expired_snapshots(
        as_of=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    assert result == {
        "retention_days": 90,
        "snapshots_deleted": 1,
        "provider_scores_deleted": 1,
    }
    assert store.snapshot_summary() == {"pending": 1, "evaluated": 0, "skipped": 0}
    assert store.provider_summary() == []


@pytest.mark.asyncio
async def test_metadata_only_source_persists_no_derived_weather_values(tmp_path: Path):
    submission = _submission_with_hourly_external_data()
    submission.provider_results = [
        submission.provider_results[0].model_copy(
            update={"retention_policy": "metadata_only"},
        )
    ]
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    store.record_forecast_snapshot(
        submission,
        captured_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    payload = json.loads(store.pending_snapshots(date(2026, 8, 10))[0]["submission_json"])
    metadata = payload["provider_daily_features"][0]
    assert metadata["retention_policy"] == "metadata_only"
    assert metadata["max_temperature"] is None
    assert metadata["min_temperature"] is None
    assert metadata["rain_forecast"] is None
    assert metadata["wind_speed_max"] is None

    class IndependentTruthClient:
        async def fetch(
            self,
            latitude: float,
            longitude: float,
            target_date: str,
        ) -> ObservedWeather:
            return ObservedWeather(
                target_date=target_date,
                max_temperature=40.0,
                min_temperature=19.0,
                precipitation_sum=1.0,
                rain_observed=True,
                wind_speed=4.0,
                source="independent_official_reference",
                fetched_at="2026-08-12T00:00:00+00:00",
                dependent_provider_ids=[],
            )

    result = await verify_due_snapshots(
        store,
        IndependentTruthClient(),
        today=date(2026, 8, 12),
        truth_delay_days=1,
    )
    assert result == {"due": 1, "evaluated": 0, "deferred": 0, "skipped": 1}
    assert store.provider_summary() == []


@pytest.mark.asyncio
async def test_legacy_full_submission_row_remains_verifiable(tmp_path: Path):
    database_path = tmp_path / "learning.db"
    store = ControlledLearningStore(str(database_path))
    legacy_submission = _submission_with_hourly_external_data()
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO forecast_snapshots("
            "fingerprint,captured_at,target_date,region,latitude,longitude,submission_json,status"
            ") VALUES(?,?,?,?,?,?,?,'pending')",
            (
                "legacy-v1-row",
                "2026-08-09T00:00:00+00:00",
                "2026-08-10",
                "广东省广州市",
                23.1291,
                113.2644,
                legacy_submission.model_dump_json(),
            ),
        )

    class IndependentTruthClient:
        async def fetch(
            self,
            latitude: float,
            longitude: float,
            target_date: str,
        ) -> ObservedWeather:
            return ObservedWeather(
                target_date=target_date,
                max_temperature=40.0,
                min_temperature=19.0,
                precipitation_sum=1.0,
                rain_observed=True,
                wind_speed=4.0,
                source="independent_official_reference",
                fetched_at="2026-08-12T00:00:00+00:00",
                dependent_provider_ids=[],
            )

    result = await verify_due_snapshots(
        store,
        IndependentTruthClient(),
        today=date(2026, 8, 12),
        truth_delay_days=1,
    )

    assert result == {"due": 1, "evaluated": 1, "deferred": 0, "skipped": 0}
    assert store.provider_summary()[0]["sample_count"] == 1


@pytest.mark.asyncio
async def test_verification_cycle_enforces_snapshot_retention_before_truth_fetch(tmp_path: Path):
    class TruthMustNotBeFetched:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(
            self,
            latitude: float,
            longitude: float,
            target_date: str,
        ) -> ObservedWeather:
            self.calls += 1
            raise AssertionError("expired third-party snapshot must be purged before truth fetch")

    store = ControlledLearningStore(
        str(tmp_path / "learning.db"),
        snapshot_retention_days=30,
    )
    store.record_forecast_snapshot(
        _submission_with_hourly_external_data().model_copy(
            update={"target_date": "2026-06-02"},
        ),
        captured_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    truth_client = TruthMustNotBeFetched()

    result = await verify_due_snapshots(
        store,
        truth_client,
        today=date(2026, 8, 12),
        truth_delay_days=1,
    )

    assert result == {"due": 0, "evaluated": 0, "deferred": 0, "skipped": 0}
    assert truth_client.calls == 0
    assert store.snapshot_summary() == {"pending": 0, "evaluated": 0, "skipped": 0}
