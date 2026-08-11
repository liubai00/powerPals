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

    def __init__(self, source_metadata: dict[str, str]):
        self.source_metadata = source_metadata
        self.source_endpoints = (source_metadata["source_url"],)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=20.0 if hour < 12 else 30.0,
                    precipitation_probability=20.0 if hour < 12 else 70.0,
                    wind_speed=2.0 if hour < 12 else 5.0,
                    cloud_cover=30.0 if hour < 12 else 70.0,
                )
                for hour in range(24)
            ],
            **self.source_metadata,
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
            dependent_provider_ids=[],
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
async def test_same_provider_historical_product_is_not_used_as_independent_truth(tmp_path: Path):
    class SameProviderTruthClient:
        async def fetch(self, latitude: float, longitude: float, target_date: str) -> ObservedWeather:
            return ObservedWeather(
                target_date=target_date,
                max_temperature=29.0,
                min_temperature=19.0,
                precipitation_sum=2.0,
                rain_observed=True,
                wind_speed=4.0,
                fetched_at="2026-08-09T00:00:00+00:00",
                source="open_meteo_historical_weather_grid",
                dependent_provider_ids=["open_meteo"],
            )

    store = ControlledLearningStore(str(tmp_path / "learning.db"))
    store.record_forecast_snapshot(
        make_submission(target_date="2026-08-01"),
        captured_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )

    summary = await verify_due_snapshots(
        store,
        SameProviderTruthClient(),
        today=date(2026, 8, 3),
        truth_delay_days=1,
    )

    assert summary == {"due": 1, "evaluated": 0, "deferred": 0, "skipped": 1}
    assert store.provider_summary() == []


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
    assert not any("synthetic_matrix" in case.tags for case in cases)

    _run_id, results = run_deterministic_replay(store, today=date(2026, 8, 9))
    failures = [item for item in results if not item.passed]

    assert failures == []


def test_core_replay_manifest_maps_each_document_case_exactly_once():
    from services.weather_bot.core_replay_gate import core_replay_manifest

    items = core_replay_manifest()

    assert [item.case_number for item in items] == list(range(1, 97))
    assert all(item.input_text and item.expected for item in items)
    assert all(item.status in {"implemented", "not_implemented", "blocked"} for item in items)
    assert not any("generated-location-" in (item.executor_id or "") for item in items)


def test_core_replay_gate_executes_all_96_documented_cases():
    from services.weather_bot.core_replay_gate import run_core_replay_gate

    report = run_core_replay_gate(today=date(2026, 8, 9))

    assert report["total"] == 96
    assert report["passed"] == 96
    assert report["passed"] + report["failed"] + report["not_implemented"] + report["blocked"] == 96
    assert report["gate_passed"] is True
    assert report["safety"] == {
        "external_calls": 0,
        "feishu_sends": 0,
        "runtime_mutations": 0,
    }
    outcomes = {item["case_number"]: item for item in report["items"]}
    assert outcomes[1]["outcome"] == "passed"
    assert outcomes[51]["outcome"] == "passed"
    assert outcomes[69]["outcome"] == "passed"
    assert outcomes[74]["outcome"] == "passed"
    assert outcomes[80]["outcome"] == "passed"
    assert outcomes[96]["outcome"] == "passed"
    assert report["failed_cases"] == []
    assert report["not_implemented_cases"] == []
    assert report["unresolved_case_numbers"] == []


def test_core_replay_gate_uses_the_public_admin_api_for_p0_send_cases() -> None:
    from services.weather_bot.core_replay_gate import run_core_replay_gate

    report = run_core_replay_gate(today=date(2026, 8, 9))
    outcomes = {item["case_number"]: item for item in report["items"]}

    for number in (85, 86):
        assert outcomes[number]["outcome"] == "passed"
        assert outcomes[number]["evidence"]["executor"] == "public_admin_api"
        assert outcomes[number]["evidence"]["forecast_calls"] in {0, 1}
        assert outcomes[number]["evidence"]["feishu_sends"] == 0
    assert outcomes[85]["evidence"]["status_code"] in {401, 403}
    assert outcomes[85]["evidence"]["forecast_calls"] == 0
    assert outcomes[86]["evidence"]["delivery_reason"] == "global_send_disabled"


def test_core_replay_gate_executes_documented_electricity_entity_cases():
    from services.weather_bot.core_replay_gate import run_core_replay_gate

    report = run_core_replay_gate(today=date(2026, 8, 9))
    outcomes = {item["case_number"]: item for item in report["items"]}
    entity_case_numbers = {
        13,
        17,
        18,
        19,
        20,
        21,
        22,
        25,
        26,
        27,
        29,
        31,
        34,
        35,
        37,
        38,
        51,
        53,
        54,
    }

    assert {number: outcomes[number]["outcome"] for number in entity_case_numbers} == {
        number: "passed" for number in entity_case_numbers
    }
    assert all(outcomes[number]["evidence"]["executor"] == "electricity_entities" for number in entity_case_numbers)


def test_core_replay_gate_executes_existing_entity_boundary_briefing_and_source_seams():
    from services.weather_bot.core_replay_gate import run_core_replay_gate

    report = run_core_replay_gate(today=date(2026, 8, 9))
    outcomes = {item["case_number"]: item for item in report["items"]}
    expected_executors = {
        23: "public_feishu_event",
        24: "public_feishu_event",
        28: "electricity_entities",
        30: "electricity_entities",
        33: "electricity_entities",
        36: "electricity_entities",
        39: "briefing_card",
        40: "briefing_card",
        41: "briefing_card",
        42: "public_feishu_event",
        43: "weather_risk_evidence",
        44: "public_feishu_event",
        45: "public_feishu_event",
        46: "weather_risk_evidence",
        47: "weather_risk_evidence",
        48: "weather_risk_evidence",
        49: "weather_risk_evidence",
        50: "public_feishu_event",
        52: "decision_boundary",
        69: "public_feishu_event",
        70: "public_feishu_event",
        71: "public_feishu_event",
        72: "briefing_card",
        73: "briefing_card",
        74: "subscription_coordinator",
        75: "subscription_coordinator",
        76: "subscription_coordinator",
        77: "subscription_coordinator",
        78: "subscription_coordinator",
        79: "subscription_coordinator",
        80: "alert_engine",
        81: "alert_engine",
        82: "alert_engine",
        83: "alert_engine",
        84: "alert_engine",
        89: "data_availability_gate",
        90: "briefing_risk_order",
        91: "data_availability_gate",
        92: "data_availability_gate",
        93: "data_availability_gate",
        94: "data_availability_gate",
        96: "source_retention_policy",
    }

    assert {number: outcomes[number]["outcome"] for number in expected_executors} == {
        number: "passed" for number in expected_executors
    }
    assert {
        number: outcomes[number]["evidence"]["executor"]
        for number in expected_executors
    } == expected_executors
    for number in (43, 46, 47, 48, 49):
        assert outcomes[number]["evidence"]["external_calls"] == 0
        assert outcomes[number]["evidence"]["source_run_ids"]
    assert all(
        outcomes[number]["evidence"]["external_calls"] == 0
        and outcomes[number]["evidence"]["real_feishu_sends"] == 0
        for number in (23, 24, 42, 44, 45, 50)
    )
    assert all(outcomes[number]["evidence"]["forecast_provider_calls"] == 0 for number in (23, 24, 50))
    assert outcomes[50]["evidence"]["warning_adapter_calls"] == 1
    assert all(
        outcomes[number]["evidence"]["send_performed"] is False
        for number in range(74, 80)
    )
    assert all(outcomes[number]["evidence"]["real_sends"] == 0 for number in range(80, 85))


def test_core_replay_gate_runs_existing_group_context_and_p0_safety_seams():
    from services.weather_bot.core_replay_gate import run_core_replay_gate

    report = run_core_replay_gate(today=date(2026, 8, 9))
    outcomes = {item["case_number"]: item for item in report["items"]}
    expected_passes = {
        1,
        2,
        3,
        6,
        7,
        8,
        9,
        10,
        12,
        14,
        15,
        16,
        32,
        55,
        56,
        57,
        58,
        59,
        60,
        63,
        64,
        65,
        67,
        68,
        85,
        86,
        87,
        88,
        95,
    }

    assert {number: outcomes[number]["outcome"] for number in expected_passes} == {
        number: "passed" for number in expected_passes
    }
    assert outcomes[66]["status"] == "implemented"
    assert outcomes[66]["outcome"] == "passed"


def test_core_replay_gate_executes_the_five_public_event_entry_cases():
    from services.weather_bot.core_replay_gate import run_core_replay_gate

    report = run_core_replay_gate(today=date(2026, 8, 9))
    outcomes = {item["case_number"]: item for item in report["items"]}

    for number in (4, 5, 11, 61, 62):
        assert outcomes[number]["status"] == "implemented"
        assert outcomes[number]["outcome"] == "passed"
        assert outcomes[number]["evidence"]["executor"] == "public_feishu_event"
        assert outcomes[number]["evidence"]["external_calls"] == 0
        assert outcomes[number]["evidence"]["real_feishu_sends"] == 0
    assert outcomes[5]["evidence"]["mocked_feishu_boundary_calls"] == 0


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
async def test_forecast_service_archives_snapshot_best_effort(
    tmp_path: Path,
    external_source_metadata,
    verified_test_source_registry,
    test_source_clock,
):
    settings = Settings(
        _env_file=None,
        app_env="test",
        controlled_learning_enabled=True,
        controlled_learning_db=str(tmp_path / "learning.db"),
    )
    service = ForecastService(
        providers={"open_meteo": FakeProvider(external_source_metadata("open_meteo"))},
        settings=settings,
        source_registry=verified_test_source_registry(
            {"open_meteo": "https://open_meteo.weather.test/v1/forecast"}
        ),
        clock=test_source_clock,
    )

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

    assert "CRON_TZ=Asia/Shanghai" in cron
    assert "30 10 * * *" in cron
    assert "controlled_learning_cli run" in cron
    command = cron.splitlines()[-1].lower()
    assert "feishu" not in command
    assert "send" not in command


def test_strict_cli_succeeds_when_core_96_gate_is_fully_resolved(
    tmp_path: Path,
    monkeypatch,
):
    from services.weather_bot import controlled_learning_cli

    settings = Settings(
        _env_file=None,
        controlled_learning_enabled=True,
        controlled_learning_db=str(tmp_path / "learning.db"),
        controlled_learning_report_dir=str(tmp_path / "reports"),
    )
    monkeypatch.setattr(controlled_learning_cli, "Settings", lambda: settings)

    exit_code = controlled_learning_cli.main(["run", "--skip-truth", "--strict"])

    assert exit_code == 0


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
    assert report["core_gate"]["total"] == 96
    assert report["core_gate"]["passed"] == 96
    assert report["core_gate"]["failed"] == 0
    assert report["core_gate"]["gate_passed"] is True
    assert report["core_gate"]["safety"] == {
        "external_calls": 0,
        "feishu_sends": 0,
        "runtime_mutations": 0,
    }
    assert report["core_gate"]["not_implemented_cases"] == []
    assert report["core_gate"]["unresolved_case_numbers"] == []
    assert report["core_gate"]["blocked_cases"] == []
    assert report["safety"] == {
        "feishu_send": False,
        "runtime_mutation": False,
        "automatic_deploy": False,
        "candidate_auto_apply": False,
    }
    assert Path(report["report_files"]["json"]).exists()
    markdown = Path(report["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "96" in markdown
    assert "不发飞书、不改规则、不部署" in markdown
    assert "参考数据：未启用（来源策略未通过）" in markdown
    assert "本轮实际用于评分：否" in markdown
    assert "参考口径：Open-Meteo 历史格点" not in markdown
