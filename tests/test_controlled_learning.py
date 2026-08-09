from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning import (
    ControlledLearningStore,
    ObservedWeather,
    ProviderScore,
    ReplayCase,
    ReplayExpectation,
    generate_improvement_candidates,
    sanitize_evidence,
    score_provider_forecast,
    verify_due_snapshots,
)
from services.weather_bot.controlled_learning_cli import run_cycle
from services.weather_bot.controlled_learning_replay import (
    generate_replay_cases,
    run_deterministic_replay,
)
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastRequest,
    ForecastSummary,
    ProviderForecast,
    ScopeProfile,
    WeatherSubmission,
)
from services.weather_bot.service import ForecastService


class FakeProvider:
    name = "open_meteo"

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T00:00:00+08:00",
                    temperature=20.0,
                    precipitation_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=30.0,
                ),
                ForecastPoint(
                    time=f"{request.target_date}T12:00:00+08:00",
                    temperature=30.0,
                    precipitation_probability=70.0,
                    wind_speed=5.0,
                    cloud_cover=70.0,
                ),
            ],
        )


class FakeTruthClient:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, latitude: float, longitude: float, target_date: str) -> ObservedWeather:
        self.calls += 1
        return ObservedWeather(
            target_date=target_date,
            max_temperature=29.0,
            min_temperature=19.0,
            precipitation_sum=2.0,
            rain_observed=True,
            wind_speed=4.0,
            fetched_at="2026-08-09T00:00:00+00:00",
        )


class FailingProvider:
    name = "open_meteo"

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        raise RuntimeError("secret provider detail must not be stored")


def make_submission(
    *,
    target_date: str = "2026-08-01",
    providers: list[ProviderForecast] | None = None,
    confidence_score: float = 0.7,
) -> WeatherSubmission:
    provider_results = providers or [
        ProviderForecast(
            provider="open_meteo",
            points=[
                ForecastPoint(
                    time=f"{target_date}T00:00:00+08:00",
                    temperature=20.0,
                    precipitation_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=30.0,
                ),
                ForecastPoint(
                    time=f"{target_date}T12:00:00+08:00",
                    temperature=30.0,
                    precipitation_probability=70.0,
                    wind_speed=5.0,
                    cloud_cover=70.0,
                ),
            ],
        )
    ]
    points = provider_results[0].points
    return WeatherSubmission(
        task_id=f"WEATHER-CN-440100-{target_date.replace('-', '')}-DAYAHEAD-001",
        region="广东省广州市",
        target_date=target_date,
        data_cutoff_time=f"{target_date}T00:00:00+08:00",
        scope=ScopeProfile(
            region="广东省广州市",
            target_date=target_date,
            location={"latitude": 23.1291, "longitude": 113.2644},
        ),
        provider_results=provider_results,
        aggregated_forecast=AggregatedForecast(
            providers_used=[item.provider for item in provider_results if item.status == "ok"],
            points=points,
            summary=ForecastSummary(
                max_temperature=30.0,
                min_temperature=20.0,
                rain_probability=70.0,
                wind_speed=5.0,
                cloud_cover=50.0,
                main_weather="多云有阵雨",
                high_risk_period="12:00 起存在局地天气不确定性",
            ),
        ),
        confidence={"score": confidence_score, "description": "测试"},
        key_factors=["测试"],
        risk_notes=[],
    )


def test_controlled_learning_is_opt_in_by_default():
    settings = Settings(_env_file=None)

    assert settings.controlled_learning_enabled is False
    assert settings.controlled_learning_db.endswith("controlled_learning.db")


def test_sanitize_evidence_removes_secret_values_and_keys():
    sanitized = sanitize_evidence(
        {
            "api_key": "very-secret",
            "message": "Bearer abcdefghijk",
            "nested": {"password": "123456"},
        }
    )

    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["password"] == "[REDACTED]"
    assert "abcdefghijk" not in sanitized["message"]


def test_store_records_minimized_snapshot_and_signals(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    submission = make_submission(
        confidence_score=0.5,
        providers=[
            ProviderForecast(
                provider="open_meteo",
                points=[
                    ForecastPoint(
                        time="2026-08-01T12:00:00+08:00",
                        temperature=30.0,
                        precipitation_probability=60.0,
                        wind_speed=5.0,
                        cloud_cover=70.0,
                    )
                ],
                raw={"api_key": "must-not-be-stored"},
            ),
            ProviderForecast(
                provider="qweather",
                status="error",
                error_message="https://api.example.test?token=must-not-be-stored",
            ),
        ],
    )

    first_id = store.record_forecast_snapshot(submission)
    second_id = store.record_forecast_snapshot(submission)

    assert first_id == second_id
    assert store.snapshot_summary() == {"pending": 1, "evaluated": 0, "skipped": 0}
    signal_types = {item["signal_type"] for item in store.signal_summary()}
    assert signal_types == {"low_forecast_confidence", "provider_unavailable"}
    database_bytes = (tmp_path / "learning.db").read_bytes()
    assert b"must-not-be-stored" not in database_bytes


def test_provider_score_uses_per_provider_values():
    provider_result = ProviderForecast(
        provider="open_meteo",
        points=[
            ForecastPoint(
                time="2026-08-01T00:00:00+08:00",
                temperature=20.0,
                precipitation_probability=10.0,
                wind_speed=2.0,
            ),
            ForecastPoint(
                time="2026-08-01T12:00:00+08:00",
                temperature=30.0,
                precipitation_probability=80.0,
                wind_speed=5.0,
            ),
        ],
    )
    truth = ObservedWeather(
        target_date="2026-08-01",
        max_temperature=29.0,
        min_temperature=19.0,
        precipitation_sum=1.2,
        rain_observed=True,
        wind_speed=4.0,
        fetched_at="2026-08-02T00:00:00+00:00",
    )

    score = score_provider_forecast(
        provider_result,
        truth,
        region="广东省广州市",
        target_date="2026-08-01",
        horizon_days=2,
    )

    assert score is not None
    assert score.temperature_mae == 1.0
    assert score.rain_hit is True
    assert score.wind_speed_error == 1.0
    assert score.horizon_days == 2
    assert score.total_score == 92.5


@pytest.mark.asyncio
async def test_due_snapshot_is_scored_without_any_message_send(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    store.record_forecast_snapshot(
        make_submission(target_date="2026-08-01"),
        captured_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )

    summary = await verify_due_snapshots(
        store,
        FakeTruthClient(),
        today=date(2026, 8, 3),
        truth_delay_days=1,
    )

    assert summary == {"due": 1, "evaluated": 1, "deferred": 0, "skipped": 0}
    assert store.snapshot_summary() == {"pending": 0, "evaluated": 1, "skipped": 0}
    provider_summary = store.provider_summary()
    assert provider_summary[0]["provider"] == "open_meteo"
    assert provider_summary[0]["sample_count"] == 1


@pytest.mark.asyncio
async def test_truth_fetch_is_reused_for_same_location_and_date(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    first = make_submission(target_date="2026-08-01")
    second = make_submission(
        target_date="2026-08-01",
        providers=[
            ProviderForecast(
                provider="qweather",
                points=[
                    ForecastPoint(
                        time="2026-08-01T12:00:00+08:00",
                        temperature=31.0,
                        precipitation_probability=60.0,
                        wind_speed=4.0,
                    )
                ],
            )
        ],
    )
    captured = datetime(2026, 7, 30, tzinfo=timezone.utc)
    store.record_forecast_snapshot(first, captured_at=captured)
    store.record_forecast_snapshot(second, captured_at=captured)
    truth_client = FakeTruthClient()

    result = await verify_due_snapshots(
        store,
        truth_client,
        today=date(2026, 8, 3),
        truth_delay_days=1,
    )

    assert result["evaluated"] == 2
    assert truth_client.calls == 1


@pytest.mark.asyncio
async def test_snapshot_without_coordinates_is_skipped_once(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    submission = make_submission(target_date="2026-08-01").model_copy(
        update={
            "scope": ScopeProfile(
                region="广东省广州市",
                target_date="2026-08-01",
                location={},
            )
        }
    )
    store.record_forecast_snapshot(
        submission,
        captured_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    truth_client = FakeTruthClient()

    first = await verify_due_snapshots(
        store,
        truth_client,
        today=date(2026, 8, 3),
        truth_delay_days=1,
    )
    second = await verify_due_snapshots(
        store,
        truth_client,
        today=date(2026, 8, 3),
        truth_delay_days=1,
    )

    assert first == {"due": 1, "evaluated": 0, "deferred": 0, "skipped": 1}
    assert second == {"due": 0, "evaluated": 0, "deferred": 0, "skipped": 0}
    assert truth_client.calls == 0
    assert store.snapshot_summary() == {"pending": 0, "evaluated": 0, "skipped": 1}


def test_generated_replay_covers_core_dimensions_and_passes(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    cases = generate_replay_cases(today=date(2026, 8, 9))

    assert {case.category for case in cases} >= {
        "intent",
        "task_routing",
        "location",
        "date",
        "metric",
        "context",
        "group_gate",
    }
    assert len(cases) >= 40

    _run_id, results = run_deterministic_replay(store, today=date(2026, 8, 9))
    failures = [item for item in results if not item.passed]

    assert failures == []


def test_admin_case_overrides_generated_case_by_id(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    store.upsert_replay_case(
        ReplayCase(
            case_id="admin-weather",
            category="intent",
            text="广州天气",
            source="admin",
            expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=1),
        )
    )

    _run_id, results = run_deterministic_replay(store, today=date(2026, 8, 9))

    assert next(item for item in results if item.case_id == "admin-weather").passed


def test_candidate_requires_audited_manual_state_transition(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    candidate = store.create_candidate(
        "intent_priority_review",
        {"category": "intent", "runtime_effect": "none"},
        {"case_ids": ["case-1"]},
    )

    approved = store.decide_candidate(
        candidate["candidate_id"],
        "approved",
        actor="admin",
        reason="全量回放通过",
    )
    rolled_back = store.decide_candidate(
        candidate["candidate_id"],
        "rolled_back",
        actor="admin",
        reason="撤销审批",
    )

    assert approved["status"] == "approved"
    assert approved["payload"]["runtime_effect"] == "none"
    assert rolled_back["status"] == "rolled_back"
    assert [item["to_status"] for item in store.candidate_audit(candidate["candidate_id"])] == [
        "approved",
        "rolled_back",
    ]


def test_provider_weight_candidate_waits_for_enough_comparable_samples(tmp_path: Path):
    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    truth_source = "test_truth"
    for index in range(2):
        target = f"2026-08-0{index + 1}"
        snapshot_id = store.record_forecast_snapshot(make_submission(target_date=target))
        assert snapshot_id is not None
        store.mark_snapshot_evaluated(
            snapshot_id,
            [
                ProviderScore(
                    provider="open_meteo",
                    region="广东省广州市",
                    target_date=target,
                    horizon_days=1,
                    temperature_mae=1.0,
                    rain_hit=True,
                    wind_speed_error=1.0,
                    total_score=90.0,
                    truth_source=truth_source,
                ),
                ProviderScore(
                    provider="qweather",
                    region="广东省广州市",
                    target_date=target,
                    horizon_days=1,
                    temperature_mae=2.0,
                    rain_hit=False,
                    wind_speed_error=2.0,
                    total_score=70.0,
                    truth_source=truth_source,
                ),
            ],
            truth_source,
        )

    none_yet = generate_improvement_candidates(store, [], min_provider_samples=3)
    enough = generate_improvement_candidates(store, [], min_provider_samples=2)

    assert not [item for item in none_yet if item["candidate_type"] == "provider_weight_review"]
    provider_candidates = [
        item for item in enough if item["candidate_type"] == "provider_weight_review"
    ]
    assert len(provider_candidates) == 1
    assert provider_candidates[0]["payload"]["runtime_effect"] == "none"


@pytest.mark.asyncio
async def test_forecast_service_archives_snapshot_best_effort(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        controlled_learning_enabled=True,
        controlled_learning_db=str(tmp_path / "learning.db"),
    )
    service = ForecastService(providers={"open_meteo": FakeProvider()}, settings=settings)

    submission = await service.forecast(
        ForecastRequest(
            region="广州",
            target_date="2026-08-10",
            providers=["open_meteo"],
        )
    )

    assert submission.region == "广东省广州市"
    assert ControlledLearningStore(str(tmp_path / "learning.db")).snapshot_summary()["pending"] == 1


@pytest.mark.asyncio
async def test_all_provider_failure_is_archived_without_secret_error_text(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        controlled_learning_enabled=True,
        controlled_learning_db=str(tmp_path / "learning.db"),
    )
    service = ForecastService(providers={"open_meteo": FailingProvider()}, settings=settings)

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="广州",
                target_date="2026-08-10",
                providers=["open_meteo"],
            )
        )

    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    assert store.signal_summary()[0]["signal_type"] == "forecast_unusable"
    assert b"secret provider detail" not in (tmp_path / "learning.db").read_bytes()


def test_controlled_learning_cron_has_no_feishu_or_send_command():
    cron = Path("deploy/controlled_learning.cron").read_text(encoding="utf-8")

    assert "30 2 * * *" in cron
    assert "controlled_learning_cli run" in cron
    command = cron.splitlines()[-1].lower()
    assert "feishu" not in command
    assert "send" not in command


@pytest.mark.asyncio
async def test_cycle_writes_json_and_markdown_without_truth_network(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        controlled_learning_enabled=True,
        controlled_learning_db=str(tmp_path / "learning.db"),
        controlled_learning_report_dir=str(tmp_path / "reports"),
    )

    report = await run_cycle(settings, skip_truth=True, truth_limit=10)

    assert report["replay"]["failed"] == 0
    assert report["safety"] == {
        "feishu_send": False,
        "runtime_mutation": False,
        "automatic_deploy": False,
        "candidate_auto_apply": False,
    }
    assert Path(report["report_files"]["json"]).exists()
    markdown = Path(report["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "不发飞书、不改规则、不部署" in markdown
    assert "不等同于官方站点实况" in markdown
