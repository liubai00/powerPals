"""Deterministic, traceable weather evidence for power-market workflows.

The project owns no weather or electricity facts.  Every function in this
module derives a bounded weather-side proxy from an already-normalized
``WeatherSubmission``.  Missing provenance fails closed; none of the outputs
represent actual load, renewable generation, forecast error in MW, or price.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import math
import re
from typing import Any, Iterable, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from services.weather_bot.models import ForecastPoint, ProviderForecast, WeatherSubmission


_VARIABLES: tuple[tuple[str, str, float], ...] = (
    ("cloud_cover", "%", 100.0),
    ("precipitation_probability", "%", 100.0),
    ("temperature", "℃", 12.0),
    ("apparent_temperature", "℃", 12.0),
    ("wind_speed", "m/s", 15.0),
    ("wind_direction", "°", 180.0),
    ("shortwave_radiation", "W/m²", 1000.0),
)
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class WindRampWindow(BaseModel):
    start: str
    end: str
    peak_at: str
    max_speed_change: float
    max_direction_change: float
    metric_label: str = "10米地面风快速变化代理"
    boundary: str = (
        "仅反映10米地面风的连续快速变化；没有轮毂高度风、机组功率曲线和实际出力，"
        "不能解释为实际风电爬坡或爬坡MW。"
    )


class DisagreementItem(BaseModel):
    variable: str
    unit: str
    valid_time: str
    minimum: float
    maximum: float
    spread: float
    providers: tuple[str, ...]
    normalized_spread: float = Field(ge=0.0)


class ProviderDisagreement(BaseModel):
    status: Literal["available", "unavailable"]
    reason: str
    source_run_id: str | None = None
    items: tuple[DisagreementItem, ...] = ()
    boundary: str = (
        "分歧只说明不同外部数据源在同一有效时刻的差异，不表示多数源必然正确。"
    )


class VersionChange(BaseModel):
    variable: str
    unit: str
    valid_time: str
    previous_value: float
    current_value: float
    delta: float
    normalized_delta: float = Field(ge=0.0)


class ForecastVersionComparison(BaseModel):
    status: Literal["available", "unavailable"]
    reason: str
    current_run_id: str | None = None
    previous_run_id: str | None = None
    comparable_valid_times: int = 0
    changes: tuple[VersionChange, ...] = ()
    boundary: str = (
        "仅比较同一地点、同一有效时刻的两个可回溯预报版本；变化不代表实况必然改变。"
    )


class ConfidenceFactor(BaseModel):
    status: Literal["good", "degraded", "poor", "unavailable"]
    value: float | None = None
    reason: str


class ConfidenceExplanation(BaseModel):
    level: Literal["较高", "中等", "偏低", "不可用"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    factors: dict[str, ConfidenceFactor]
    explanation: str
    boundary: str = "置信度由可核查规则计算，不使用大模型主观补分，也不代表电力市场结论可信度。"


class RenewableComplexityEntry(BaseModel):
    region: str
    score: float = Field(ge=0.0, le=100.0)
    drivers: tuple[str, ...]
    source_run_id: str


class RenewableComplexityRanking(BaseModel):
    status: Literal["available", "unavailable"]
    reason: str
    metric_label: str = "新能源预测复杂度气象代理"
    entries: tuple[RenewableComplexityEntry, ...] = ()
    boundary: str = (
        "该排行只表示气象条件下的预测复杂程度；没有新能源预测值和实际出力，"
        "不能视为实际新能源预测偏差、以MW计量的误差或考核结果。"
    )


def _parse_aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _valid_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _provider_is_traceable(provider: ProviderForecast) -> bool:
    return bool(
        provider.status == "ok"
        and provider.points
        and _valid_url(provider.source_url)
        and _SHA256_RE.fullmatch(provider.content_sha256 or "")
        and _parse_aware(provider.retrieved_at)
    )


def _submission_is_traceable(submission: WeatherSubmission) -> bool:
    used = set(submission.aggregated_forecast.providers_used)
    providers = [item for item in submission.provider_results if item.provider in used]
    valid_time = submission.time_info.valid_time
    return bool(
        submission.time_info.forecast_run_id
        and _parse_aware(submission.time_info.retrieved_at)
        and _parse_aware(valid_time.start)
        and _parse_aware(valid_time.end)
        and valid_time.timezone
        and providers
        and all(_provider_is_traceable(item) for item in providers)
    )


def _number(point: Any, field: str) -> float | None:
    value = getattr(point, field, None)
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _circular_direction_delta(left: float | None, right: float | None) -> float:
    if left is None or right is None:
        return 0.0
    return min(abs(left - right) % 360.0, 360.0 - (abs(left - right) % 360.0))


def detect_wind_ramp_windows(
    points: Sequence[ForecastPoint],
    *,
    minimum_speed_change: float = 3.0,
    minimum_direction_change: float = 60.0,
    minimum_duration_hours: float = 2.0,
    merge_gap_hours: float = 1.0,
) -> tuple[WindRampWindow, ...]:
    """Find continuous 10 m wind-change windows without inferring generation."""

    ordered = sorted(
        ((timestamp, point) for point in points if (timestamp := _parse_aware(point.time))),
        key=lambda item: item[0],
    )
    transitions: list[dict[str, Any]] = []
    for (start_at, left), (end_at, right) in zip(ordered, ordered[1:]):
        elapsed = (end_at - start_at).total_seconds() / 3600.0
        if not 0 < elapsed <= 1.5:
            continue
        left_speed = _number(left, "wind_speed")
        right_speed = _number(right, "wind_speed")
        if left_speed is None or right_speed is None:
            continue
        speed_change = abs(right_speed - left_speed)
        direction_change = _circular_direction_delta(
            _number(left, "wind_direction"),
            _number(right, "wind_direction"),
        )
        direction_trigger = (
            direction_change >= minimum_direction_change
            and max(left_speed, right_speed) >= 5.0
        )
        if speed_change < minimum_speed_change and not direction_trigger:
            continue
        transitions.append(
            {
                "start": start_at,
                "end": end_at,
                "speed_change": speed_change,
                "direction_change": direction_change,
                "peak_at": end_at if right_speed >= left_speed else start_at,
                "peak_speed": max(left_speed, right_speed),
            }
        )

    if not transitions:
        return ()
    groups: list[list[dict[str, Any]]] = [[transitions[0]]]
    for transition in transitions[1:]:
        gap = (transition["start"] - groups[-1][-1]["end"]).total_seconds() / 3600.0
        if gap <= merge_gap_hours:
            groups[-1].append(transition)
        else:
            groups.append([transition])

    windows: list[WindRampWindow] = []
    for group in groups:
        start_at = group[0]["start"]
        end_at = group[-1]["end"]
        duration = (end_at - start_at).total_seconds() / 3600.0
        if duration < minimum_duration_hours:
            continue
        peak = max(group, key=lambda item: (item["peak_speed"], item["speed_change"]))
        windows.append(
            WindRampWindow(
                start=start_at.isoformat(),
                end=end_at.isoformat(),
                peak_at=peak["peak_at"].isoformat(),
                max_speed_change=round(max(item["speed_change"] for item in group), 2),
                max_direction_change=round(max(item["direction_change"] for item in group), 1),
            )
        )
    return tuple(windows)


def _provider_values(
    providers: Iterable[ProviderForecast],
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    values: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for provider in providers:
        for point in provider.points:
            if _parse_aware(point.time) is None:
                continue
            for field, _unit, _scale in _VARIABLES:
                value = _number(point, field)
                if value is not None:
                    values[(point.time, field)].append((provider.provider, value))
    return values


def analyze_provider_disagreement(
    submission: WeatherSubmission,
    *,
    limit: int = 8,
) -> ProviderDisagreement:
    providers = [
        item
        for item in submission.provider_results
        if item.provider in submission.aggregated_forecast.providers_used
    ]
    run_id = submission.time_info.forecast_run_id or None
    if len(providers) < 2:
        return ProviderDisagreement(
            status="unavailable",
            reason="insufficient_independent_providers",
            source_run_id=run_id,
        )
    if not _submission_is_traceable(submission):
        return ProviderDisagreement(
            status="unavailable",
            reason="untraceable_provider_input",
            source_run_id=run_id,
        )

    metadata = {field: (unit, scale) for field, unit, scale in _VARIABLES}
    items: list[DisagreementItem] = []
    for (valid_time, field), observed in _provider_values(providers).items():
        by_provider = dict(observed)
        if len(by_provider) < 2:
            continue
        values = list(by_provider.values())
        minimum, maximum = min(values), max(values)
        unit, scale = metadata[field]
        spread = (
            max(
                _circular_direction_delta(left, right)
                for left in values
                for right in values
            )
            if field == "wind_direction"
            else maximum - minimum
        )
        items.append(
            DisagreementItem(
                variable=field,
                unit=unit,
                valid_time=valid_time,
                minimum=round(minimum, 3),
                maximum=round(maximum, 3),
                spread=round(spread, 3),
                providers=tuple(sorted(by_provider)),
                normalized_spread=round(max(0.0, spread / scale), 4),
            )
        )
    items.sort(
        key=lambda item: (
            -item.normalized_spread,
            next(index for index, variable in enumerate(_VARIABLES) if variable[0] == item.variable),
            item.valid_time,
        )
    )
    return ProviderDisagreement(
        status="available" if items else "unavailable",
        reason="aligned_provider_values" if items else "no_comparable_provider_values",
        source_run_id=run_id,
        items=tuple(items[: max(0, limit)]),
    )


def compare_forecast_versions(
    current: WeatherSubmission,
    previous: WeatherSubmission,
    *,
    limit: int = 12,
) -> ForecastVersionComparison:
    current_run = current.time_info.forecast_run_id or None
    previous_run = previous.time_info.forecast_run_id or None
    if current.region != previous.region or current.target_date != previous.target_date:
        return ForecastVersionComparison(
            status="unavailable",
            reason="scope_mismatch",
            current_run_id=current_run,
            previous_run_id=previous_run,
        )
    if current_run == previous_run or not current_run or not previous_run:
        return ForecastVersionComparison(
            status="unavailable",
            reason="distinct_versions_required",
            current_run_id=current_run,
            previous_run_id=previous_run,
        )
    if not _submission_is_traceable(current) or not _submission_is_traceable(previous):
        return ForecastVersionComparison(
            status="unavailable",
            reason="untraceable_version_input",
            current_run_id=current_run,
            previous_run_id=previous_run,
        )

    current_points = {point.time: point for point in current.aggregated_forecast.points}
    previous_points = {point.time: point for point in previous.aggregated_forecast.points}
    common = sorted(set(current_points) & set(previous_points))
    metadata = {field: (unit, scale) for field, unit, scale in _VARIABLES}
    changes: list[VersionChange] = []
    for valid_time in common:
        for field, (unit, scale) in metadata.items():
            current_value = _number(current_points[valid_time], field)
            previous_value = _number(previous_points[valid_time], field)
            if current_value is None or previous_value is None:
                continue
            if field == "wind_direction":
                delta = _circular_direction_delta(previous_value, current_value)
            else:
                delta = current_value - previous_value
            if abs(delta) < 1e-9:
                continue
            changes.append(
                VersionChange(
                    variable=field,
                    unit=unit,
                    valid_time=valid_time,
                    previous_value=round(previous_value, 3),
                    current_value=round(current_value, 3),
                    delta=round(delta, 3),
                    normalized_delta=round(abs(delta) / scale, 4),
                )
            )
    variable_priority = {
        field: index for index, (field, _unit, _scale) in enumerate(_VARIABLES)
    }
    changes.sort(
        key=lambda item: (
            -item.normalized_delta,
            item.valid_time,
            variable_priority[item.variable],
        )
    )
    return ForecastVersionComparison(
        status="available" if common else "unavailable",
        reason="same_scope_same_valid_time" if common else "no_comparable_valid_times",
        current_run_id=current_run,
        previous_run_id=previous_run,
        comparable_valid_times=len(common),
        changes=tuple(changes[: max(0, limit)]),
    )


def _expected_hour_count(submission: WeatherSubmission) -> int:
    start = _parse_aware(submission.time_info.valid_time.start)
    end = _parse_aware(submission.time_info.valid_time.end)
    if start is None or end is None or end < start:
        return 0
    return max(1, min(24 * 16, int((end - start).total_seconds() // 3600) + 1))


def explain_forecast_confidence(
    submission: WeatherSubmission,
    *,
    now: datetime,
) -> ConfidenceExplanation:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not _submission_is_traceable(submission):
        return ConfidenceExplanation(
            level="不可用",
            factors={
                "coverage": ConfidenceFactor(status="unavailable", reason="来源或有效时间不可回溯"),
                "freshness": ConfidenceFactor(status="unavailable", reason="抓取时间不可回溯"),
                "source_consistency": ConfidenceFactor(status="unavailable", reason="来源不可回溯"),
                "representativeness": ConfidenceFactor(status="unavailable", reason="代表性未核验"),
                "historical_skill": ConfidenceFactor(status="unavailable", reason="尚无已接入的历史技巧评分"),
            },
            explanation="当前来源、覆盖或时间元数据不完整，无法生成可信置信度。",
        )

    expected = _expected_hour_count(submission)
    covered = len({point.time for point in submission.aggregated_forecast.points})
    coverage = min(1.0, covered / expected) if expected else 0.0
    coverage_factor = ConfidenceFactor(
        status="good" if coverage >= 0.95 else "degraded" if coverage >= 0.8 else "poor",
        value=round(coverage, 4),
        reason=f"有效时段覆盖 {covered}/{expected} 个小时",
    )

    retrieved = _parse_aware(submission.time_info.retrieved_at)
    age_hours = max(0.0, (now - retrieved.astimezone(now.tzinfo)).total_seconds() / 3600.0) if retrieved else math.inf
    freshness_score = 1.0 if age_hours <= 3 else 0.7 if age_hours <= 12 else 0.3
    freshness_factor = ConfidenceFactor(
        status="good" if age_hours <= 3 else "degraded" if age_hours <= 12 else "poor",
        value=round(freshness_score, 4),
        reason=f"距实际抓取时间 {age_hours:.1f} 小时",
    )

    disagreement = analyze_provider_disagreement(submission)
    if disagreement.status == "available":
        maximum_spread = max((item.normalized_spread for item in disagreement.items), default=0.0)
        consistency_score = max(0.0, 1.0 - min(1.0, maximum_spread))
        consistency_factor = ConfidenceFactor(
            status="good" if maximum_spread <= 0.2 else "degraded" if maximum_spread <= 0.5 else "poor",
            value=round(consistency_score, 4),
            reason=f"同有效时刻最大归一化分歧 {maximum_spread:.2f}",
        )
    else:
        consistency_score = 0.55
        consistency_factor = ConfidenceFactor(
            status="degraded",
            value=consistency_score,
            reason="独立完整数据源不足，无法充分核对分歧",
        )

    location = submission.scope.location if isinstance(submission.scope.location, dict) else {}
    representative_score = 1.0 if location.get("station_coverage_verified") is True else 0.7
    representativeness_factor = ConfidenceFactor(
        status="good" if representative_score == 1.0 else "degraded",
        value=representative_score,
        reason=(
            "站点覆盖已核验"
            if representative_score == 1.0
            else "当前为代表点口径，不能无条件外推整个市场"
        ),
    )
    score = (
        coverage * 0.35
        + freshness_score * 0.25
        + consistency_score * 0.25
        + representative_score * 0.15
    )
    level: Literal["较高", "中等", "偏低", "不可用"] = (
        "较高" if score >= 0.85 else "中等" if score >= 0.65 else "偏低"
    )
    factors = {
        "coverage": coverage_factor,
        "freshness": freshness_factor,
        "source_consistency": consistency_factor,
        "representativeness": representativeness_factor,
        "historical_skill": ConfidenceFactor(
            status="unavailable",
            reason="尚无经来源门禁接入的历史实况技巧评分，不参与加分",
        ),
    }
    explanation = (
        f"置信度{level}：覆盖 {covered}/{expected} 小时；"
        f"数据时效距抓取 {age_hours:.1f} 小时；"
        f"数据源分歧按同一有效时刻计算；{representativeness_factor.reason}。"
    )
    return ConfidenceExplanation(
        level=level,
        score=round(score, 4),
        factors=factors,
        explanation=explanation,
    )


def _complexity_entry(region: str, submission: WeatherSubmission) -> RenewableComplexityEntry | None:
    disagreement = analyze_provider_disagreement(submission, limit=200)
    if disagreement.status != "available":
        return None
    variable_max: dict[str, float] = defaultdict(float)
    for item in disagreement.items:
        variable_max[item.variable] = max(variable_max[item.variable], item.normalized_spread)

    ramps = [
        window
        for provider in submission.provider_results
        if provider.provider in submission.aggregated_forecast.providers_used
        for window in detect_wind_ramp_windows(provider.points)
    ]
    ramp_score = min(1.0, max((window.max_speed_change for window in ramps), default=0.0) / 6.0)
    cloud_score = min(1.0, variable_max.get("cloud_cover", 0.0))
    rain_score = min(1.0, variable_max.get("precipitation_probability", 0.0))
    wind_score = min(1.0, variable_max.get("wind_speed", 0.0))
    direction_score = min(1.0, variable_max.get("wind_direction", 0.0))
    score = 100.0 * (
        cloud_score * 0.28
        + rain_score * 0.28
        + wind_score * 0.18
        + direction_score * 0.11
        + ramp_score * 0.15
    )
    drivers: list[str] = []
    if cloud_score >= 0.3:
        drivers.append("云量源间分歧")
    if rain_score >= 0.3:
        drivers.append("降水概率源间分歧")
    if wind_score >= 0.3 or direction_score >= 0.3:
        drivers.append("近地风源间分歧")
    if ramp_score > 0:
        drivers.append("10米风连续快速变化")
    if not drivers:
        drivers.append("多源差异较小")
    return RenewableComplexityEntry(
        region=region,
        score=round(score, 2),
        drivers=tuple(drivers),
        source_run_id=submission.time_info.forecast_run_id,
    )


def rank_renewable_forecast_complexity(
    submissions: Mapping[str, WeatherSubmission],
) -> RenewableComplexityRanking:
    entries = [
        entry
        for region, submission in submissions.items()
        if (entry := _complexity_entry(region, submission)) is not None
    ]
    entries.sort(key=lambda item: (-item.score, item.region))
    return RenewableComplexityRanking(
        status="available" if entries else "unavailable",
        reason="traceable_weather_complexity_proxy" if entries else "insufficient_traceable_multi_source_data",
        entries=tuple(entries),
    )


__all__ = [
    "ConfidenceExplanation",
    "DisagreementItem",
    "ForecastVersionComparison",
    "ProviderDisagreement",
    "RenewableComplexityRanking",
    "VersionChange",
    "WindRampWindow",
    "analyze_provider_disagreement",
    "compare_forecast_versions",
    "detect_wind_ramp_windows",
    "explain_forecast_confidence",
    "rank_renewable_forecast_complexity",
]
