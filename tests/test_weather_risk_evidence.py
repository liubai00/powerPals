from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ForecastWindow,
    ProviderForecast,
    TimeInfo,
    WeatherSubmission,
)
from services.weather_bot.weather_risk_evidence import (
    analyze_provider_disagreement,
    compare_forecast_versions,
    detect_wind_ramp_windows,
    explain_forecast_confidence,
    rank_renewable_forecast_complexity,
)


SHANGHAI = timezone(timedelta(hours=8))


def _point(
    hour: int,
    *,
    day: str = "2026-08-10",
    wind: float = 4.0,
    direction: float = 90.0,
    cloud: float = 30.0,
    rain: float = 10.0,
    temperature: float = 30.0,
) -> ForecastPoint:
    return ForecastPoint(
        time=f"{day}T{hour:02d}:00:00+08:00",
        temperature=temperature,
        apparent_temperature=temperature + 1,
        precipitation_probability=rain,
        cloud_cover=cloud,
        wind_speed=wind,
        wind_direction=direction,
    )


def _submission(
    region: str,
    run_id: str,
    *,
    provider_points: dict[str, list[ForecastPoint]],
    retrieved_at: str = "2026-08-09T08:00:00+08:00",
) -> WeatherSubmission:
    providers = [
        ProviderForecast(
            provider=name,
            status="ok",
            points=points,
            retrieved_at=retrieved_at,
            provider_issued_at=retrieved_at,
            source_url=f"https://official.example.test/{name}/forecast",
            content_sha256=(str(index + 1) * 64)[:64],
        )
        for index, (name, points) in enumerate(provider_points.items())
    ]
    aggregated = list(provider_points.values())[0]
    return WeatherSubmission(
        task_id=f"task-{run_id}",
        region=region,
        target_date="2026-08-10",
        data_cutoff_time="2026-08-09T16:00:00+08:00",
        time_info=TimeInfo(
            retrieved_at=retrieved_at,
            provider_issued_at={name: retrieved_at for name in provider_points},
            aggregation_completed_at="2026-08-09T08:00:01+08:00",
            valid_time=ForecastWindow(
                start="2026-08-10T00:00:00+08:00",
                end="2026-08-10T23:00:00+08:00",
                timezone="Asia/Shanghai",
            ),
            forecast_run_id=run_id,
            business_submission_deadline="2026-08-09T16:00:00+08:00",
        ),
        provider_results=providers,
        aggregated_forecast=AggregatedForecast(
            providers_used=list(provider_points),
            points=aggregated,
            summary=ForecastSummary(
                main_weather="测试",
                high_risk_period="无",
            ),
        ),
        confidence={"score": 0.7, "description": "中等"},
        key_factors=[],
        risk_notes=[],
    )


def test_wind_ramp_requires_a_continuous_window_and_labels_ten_meter_proxy() -> None:
    points = [
        _point(13, wind=4),
        _point(14, wind=4),
        _point(15, wind=8),
        _point(16, wind=12),
        _point(17, wind=12),
    ]

    windows = detect_wind_ramp_windows(points, minimum_speed_change=3.0)

    assert len(windows) == 1
    assert windows[0].start == "2026-08-10T14:00:00+08:00"
    assert windows[0].end == "2026-08-10T16:00:00+08:00"
    assert windows[0].peak_at == "2026-08-10T16:00:00+08:00"
    assert windows[0].max_speed_change == 4.0
    assert windows[0].metric_label == "10米地面风快速变化代理"
    assert "实际风电爬坡" in windows[0].boundary


def test_wind_ramp_does_not_promote_an_isolated_one_hour_jump() -> None:
    points = [_point(13, wind=4), _point(14, wind=9), _point(15, wind=9)]

    assert detect_wind_ramp_windows(points, minimum_speed_change=3.0) == ()


def test_provider_disagreement_names_variable_and_valid_hour() -> None:
    submission = _submission(
        "浙江省杭州市",
        "run-current",
        provider_points={
            "source_a": [_point(11, cloud=20, rain=10), _point(12, cloud=25, rain=10)],
            "source_b": [_point(11, cloud=80, rain=70), _point(12, cloud=30, rain=15)],
        },
    )

    result = analyze_provider_disagreement(submission)

    assert result.status == "available"
    assert result.items[0].variable == "cloud_cover"
    assert result.items[0].valid_time == "2026-08-10T11:00:00+08:00"
    assert result.items[0].spread == 60.0
    assert result.items[0].providers == ("source_a", "source_b")
    assert result.source_run_id == "run-current"
    assert "多数源" in result.boundary


def test_disagreement_fails_closed_when_provider_provenance_is_missing() -> None:
    submission = _submission(
        "浙江省杭州市",
        "run-current",
        provider_points={
            "source_a": [_point(11, cloud=20)],
            "source_b": [_point(11, cloud=80)],
        },
    )
    submission.provider_results[1].source_url = None

    result = analyze_provider_disagreement(submission)

    assert result.status == "unavailable"
    assert result.reason == "untraceable_provider_input"
    assert result.items == ()


def test_version_comparison_aligns_the_same_valid_time() -> None:
    previous = _submission(
        "山东省济南市",
        "run-previous",
        provider_points={"source_a": [_point(17, temperature=34, wind=4), _point(18, temperature=35, wind=5)]},
        retrieved_at="2026-08-08T08:00:00+08:00",
    )
    current = _submission(
        "山东省济南市",
        "run-current",
        provider_points={"source_a": [_point(17, temperature=37, wind=6), _point(18, temperature=36, wind=5)]},
    )

    result = compare_forecast_versions(current, previous)

    assert result.status == "available"
    assert result.current_run_id == "run-current"
    assert result.previous_run_id == "run-previous"
    assert result.comparable_valid_times == 2
    assert result.changes[0].variable == "temperature"
    assert result.changes[0].valid_time == "2026-08-10T17:00:00+08:00"
    assert result.changes[0].delta == 3.0
    assert "同一有效时刻" in result.boundary


def test_version_comparison_refuses_different_region_or_valid_window() -> None:
    previous = _submission(
        "河南省郑州市",
        "run-previous",
        provider_points={"source_a": [_point(17)]},
    )
    current = _submission(
        "山东省济南市",
        "run-current",
        provider_points={"source_a": [_point(17)]},
    )

    result = compare_forecast_versions(current, previous)

    assert result.status == "unavailable"
    assert result.reason == "scope_mismatch"


def test_confidence_explanation_is_objective_and_exposes_missing_history_skill() -> None:
    points_a = [_point(hour, cloud=20) for hour in range(24)]
    points_b = [_point(hour, cloud=22) for hour in range(24)]
    submission = _submission(
        "山东省济南市",
        "run-current",
        provider_points={"source_a": points_a, "source_b": points_b},
    )

    result = explain_forecast_confidence(
        submission,
        now=datetime(2026, 8, 9, 9, tzinfo=SHANGHAI),
    )

    assert result.level in {"较高", "中等", "偏低"}
    assert result.factors["coverage"].status == "good"
    assert result.factors["freshness"].status == "good"
    assert result.factors["source_consistency"].status == "good"
    assert result.factors["historical_skill"].status == "unavailable"
    assert "覆盖" in result.explanation
    assert "分歧" in result.explanation
    assert "时效" in result.explanation
    assert "大模型" in result.boundary


def test_renewable_complexity_ranking_is_a_weather_proxy_not_bias_mw() -> None:
    stable = _submission(
        "山东",
        "run-shandong",
        provider_points={
            "source_a": [_point(14, cloud=30, rain=10, wind=4), _point(15, cloud=32, rain=10, wind=4)],
            "source_b": [_point(14, cloud=32, rain=15, wind=4), _point(15, cloud=34, rain=15, wind=5)],
        },
    )
    complex_weather = _submission(
        "广东",
        "run-guangdong",
        provider_points={
            "source_a": [_point(14, cloud=10, rain=10, wind=3), _point(15, cloud=20, rain=10, wind=3)],
            "source_b": [_point(14, cloud=95, rain=90, wind=9), _point(15, cloud=90, rain=85, wind=12)],
        },
    )

    result = rank_renewable_forecast_complexity(
        {"山东": stable, "广东": complex_weather}
    )

    assert result.status == "available"
    assert result.entries[0].region == "广东"
    assert result.entries[0].score > result.entries[1].score
    assert result.metric_label == "新能源预测复杂度气象代理"
    serialized = result.model_dump_json()
    assert "偏差MW" not in serialized
    assert "实际新能源预测偏差" in result.boundary
