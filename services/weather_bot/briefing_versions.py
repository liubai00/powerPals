"""Derived provenance and version comparison for power-weather briefings.

The project owns no weather or electricity facts.  This module only condenses
traceable metadata and derived briefing features from external provider
submissions; it never persists provider raw payloads.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlsplit


def _non_empty(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _valid_web_url(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _valid_sha256(value: str | None) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value or ""))


def _aware_timestamp(value: Any) -> str | None:
    text = _non_empty(value)
    if text is None:
        return None
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    return timestamp.isoformat()


def _valid_time_identity(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, dict):
        return None
    start = _aware_timestamp(value.get("start"))
    end = _aware_timestamp(value.get("end"))
    timezone_name = _non_empty(value.get("timezone"))
    if start is None or end is None or timezone_name is None:
        return None
    if datetime.fromisoformat(end) < datetime.fromisoformat(start):
        return None
    return start, end, timezone_name


def _latest(values: Iterable[str | None]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        text = _non_empty(value)
        if text is None:
            continue
        try:
            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            continue
        parsed.append((timestamp, text))
    return max(parsed, key=lambda item: item[0])[1] if parsed else None


def _earliest(values: Iterable[str | None]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        text = _non_empty(value)
        if text is None:
            continue
        try:
            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            continue
        parsed.append((timestamp, text))
    return min(parsed, key=lambda item: item[0])[1] if parsed else None


def build_run_provenance(
    rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    *,
    forecast_run_id: str,
    release_slot: str,
    proxy_method_version: str,
    weight_version: str,
) -> dict[str, Any]:
    """Build privacy-minimized, traceable metadata from provider submissions."""

    submissions = [
        submission
        for row in rows
        for submission in (row.get("submissions") or {}).values()
        if submission is not None
    ]
    source_run_ids = sorted(
        {
            value
            for submission in submissions
            if (value := _non_empty(getattr(submission.time_info, "forecast_run_id", None)))
        }
    )
    retrieved_values: list[str | None] = []
    aggregation_values: list[str | None] = []
    valid_starts: list[str | None] = []
    valid_ends: list[str | None] = []
    valid_timezones: list[str] = []
    providers: dict[str, dict[str, Any]] = {}
    used_providers: set[str] = set()

    for submission in submissions:
        time_info = submission.time_info
        retrieved_values.append(getattr(time_info, "retrieved_at", None))
        aggregation_values.append(getattr(time_info, "aggregation_completed_at", None))
        valid_time = getattr(time_info, "valid_time", None)
        valid_starts.append(getattr(valid_time, "start", None))
        valid_ends.append(getattr(valid_time, "end", None))
        timezone_name = _non_empty(getattr(valid_time, "timezone", None))
        if timezone_name:
            valid_timezones.append(timezone_name)

        for result in submission.provider_results:
            provider = str(result.provider)
            item = providers.setdefault(
                provider,
                {
                    "provider": provider,
                    "statuses": set(),
                    "retrieved_at_values": [],
                    "provider_issued_at_values": [],
                    "source_urls": set(),
                    "content_sha256s": set(),
                    "retention_policies": set(),
                    "ok_result_count": 0,
                    "source_url_count": 0,
                    "content_sha256_count": 0,
                },
            )
            item["statuses"].add(str(result.status))
            item["retrieved_at_values"].append(result.retrieved_at)
            item["provider_issued_at_values"].append(result.provider_issued_at)
            retrieved_values.append(result.retrieved_at)
            source_url = _non_empty(result.source_url)
            valid_source_url = _valid_web_url(source_url)
            content_hash = _non_empty(result.content_sha256)
            valid_content_hash = _valid_sha256(content_hash)
            if result.status == "ok":
                item["ok_result_count"] += 1
                item["source_url_count"] += int(valid_source_url)
                item["content_sha256_count"] += int(valid_content_hash)
            if valid_source_url:
                item["source_urls"].add(source_url)
            if valid_content_hash:
                item["content_sha256s"].add(content_hash)
            item["retention_policies"].add(str(result.retention_policy))

        for provider in submission.aggregated_forecast.providers_used:
            used_providers.add(str(provider))
            providers.setdefault(
                str(provider),
                {
                    "provider": str(provider),
                    "statuses": set(),
                    "retrieved_at_values": [],
                    "provider_issued_at_values": [],
                    "source_urls": set(),
                    "content_sha256s": set(),
                    "retention_policies": {"derived_only"},
                    "ok_result_count": 0,
                    "source_url_count": 0,
                    "content_sha256_count": 0,
                },
            )

    provider_metadata: list[dict[str, Any]] = []
    provider_issued_at: dict[str, str | None] = {}
    for provider, item in sorted(providers.items()):
        if provider not in used_providers:
            continue
        issued = _latest(item["provider_issued_at_values"])
        provider_issued_at[provider] = issued
        retention_policies = sorted(item["retention_policies"])
        provider_metadata.append(
            {
                "provider": provider,
                "statuses": sorted(item["statuses"]),
                "retrieved_at": _latest(item["retrieved_at_values"]),
                "provider_issued_at": issued,
                "source_urls": sorted(item["source_urls"]),
                "content_sha256s": sorted(item["content_sha256s"]),
                "retention_policy": (
                    retention_policies[0]
                    if len(retention_policies) == 1
                    else "derived_only"
                ),
                "record_coverage": {
                    "ok": int(item["ok_result_count"]),
                    "source_url": int(item["source_url_count"]),
                    "content_sha256": int(item["content_sha256_count"]),
                },
            }
        )

    points = coverage.get("points") or {}
    baseline = coverage.get("baseline_points") or {}
    point_total = int(points.get("total") or 0)
    baseline_total = int(baseline.get("total") or 0)
    point_ratio = (int(points.get("covered") or 0) / point_total) if point_total else 0.0
    baseline_ratio = (
        int(baseline.get("covered") or 0) / baseline_total if baseline_total else 0.0
    )
    retrieved_at = _latest(retrieved_values)
    valid_start = _earliest(valid_starts)
    valid_end = _latest(valid_ends)
    valid_timezone = valid_timezones[0] if valid_timezones else None
    missing_issued = sorted(
        provider for provider, issued_at in provider_issued_at.items() if issued_at is None
    )
    missing_source_urls = sorted(
        item["provider"]
        for item in provider_metadata
        if (
            not item.get("source_urls")
            or item["record_coverage"]["source_url"]
            != item["record_coverage"]["ok"]
        )
    )
    missing_content_hashes = sorted(
        item["provider"]
        for item in provider_metadata
        if (
            not item.get("content_sha256s")
            or item["record_coverage"]["content_sha256"]
            != item["record_coverage"]["ok"]
        )
    )
    reasons: list[str] = []
    if retrieved_at is None:
        reasons.append("retrieved_at_missing")
    if valid_start is None or valid_end is None or valid_timezone is None:
        reasons.append("valid_time_missing")
    reasons.extend(f"provider_issued_at_missing:{provider}" for provider in missing_issued)
    reasons.extend(f"source_url_missing:{provider}" for provider in missing_source_urls)
    reasons.extend(f"content_sha256_missing:{provider}" for provider in missing_content_hashes)
    if point_ratio < 1.0:
        reasons.append("representative_point_coverage_incomplete")
    if baseline_ratio < 1.0:
        reasons.append("today_baseline_coverage_incomplete")
    if not used_providers:
        reasons.append("external_provider_missing")
    elif len(used_providers) == 1:
        reasons.append("single_external_provider")

    provider_total = len(provider_issued_at)
    issued_covered = provider_total - len(missing_issued)
    metric_coverage = {
        "weather_points": {
            "covered": int(points.get("covered") or 0),
            "total": point_total,
            "ratio": round(point_ratio, 4),
        },
        "today_baseline_points": {
            "covered": int(baseline.get("covered") or 0),
            "total": baseline_total,
            "ratio": round(baseline_ratio, 4),
        },
        "provider_issued_at": {
            "covered": issued_covered,
            "total": provider_total,
            "ratio": round(issued_covered / provider_total, 4) if provider_total else 0.0,
        },
    }
    if (
        retrieved_at is None
        or point_ratio == 0.0
        or not used_providers
        or missing_source_urls
        or missing_content_hashes
        or valid_start is None
        or valid_end is None
        or valid_timezone is None
    ):
        quality_status = "unusable"
        confidence_level = "不可用"
    elif reasons:
        quality_status = "degraded"
        confidence_level = "中等" if point_ratio >= 0.9 else "偏低"
    else:
        quality_status = "good"
        confidence_level = "较高"

    return {
        "forecast_run_id": forecast_run_id,
        "release_slot": release_slot,
        "proxy_method_version": proxy_method_version,
        "weight_version": weight_version,
        "retrieved_at": retrieved_at,
        "provider_issued_at": provider_issued_at,
        "aggregation_completed_at": _latest(aggregation_values),
        "valid_time": {
            "start": valid_start,
            "end": valid_end,
            "timezone": valid_timezone or "unavailable",
        },
        "sources": sorted(used_providers),
        "source_forecast_run_ids": source_run_ids,
        "provider_run_metadata": provider_metadata,
        "metric_coverage": metric_coverage,
        "quality": {
            "status": quality_status,
            "reasons": reasons,
            "point_coverage": round(point_ratio, 4),
            "baseline_coverage": round(baseline_ratio, 4),
        },
        "confidence": {
            "level": confidence_level,
            "basis": "coverage_and_provenance",
        },
    }


def _unavailable_change(
    reason: str,
    *,
    previous_run_id: str | None,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "previous_run_id": previous_run_id,
    }


def _comparison_provenance_complete(payload: Any) -> bool:
    """Accept only traceable derived snapshots as a version-comparison input."""

    if not isinstance(payload, dict):
        return False
    if _aware_timestamp(payload.get("retrieved_at")) is None:
        return False
    if _valid_time_identity(payload.get("valid_time")) is None:
        return False
    if _non_empty(payload.get("proxy_method_version")) is None:
        return False
    if _non_empty(payload.get("weight_version")) is None:
        return False
    quality = payload.get("quality")
    if not isinstance(quality, dict) or quality.get("status") not in {"good", "degraded"}:
        return False
    sources = payload.get("sources")
    provider_metadata = payload.get("provider_run_metadata")
    if not isinstance(sources, list) or not sources or not isinstance(provider_metadata, list):
        return False
    traceable_providers: set[str] = set()
    for item in provider_metadata:
        if not isinstance(item, dict):
            return False
        provider = _non_empty(item.get("provider"))
        source_urls = item.get("source_urls")
        content_hashes = item.get("content_sha256s")
        record_coverage = item.get("record_coverage")
        if (
            provider is None
            or not isinstance(source_urls, list)
            or not source_urls
            or any(not _valid_web_url(_non_empty(value)) for value in source_urls)
            or not isinstance(content_hashes, list)
            or not content_hashes
            or any(not _valid_sha256(_non_empty(value)) for value in content_hashes)
            or not isinstance(record_coverage, dict)
            or not isinstance(record_coverage.get("ok"), int)
            or record_coverage["ok"] < 1
            or record_coverage.get("source_url") != record_coverage["ok"]
            or record_coverage.get("content_sha256") != record_coverage["ok"]
        ):
            return False
        traceable_providers.add(provider)
    return traceable_providers == {str(item) for item in sources}


def _risk_comparison_identity(
    item: Any,
) -> tuple[str, tuple[str, str, str], str, str] | None:
    if not isinstance(item, dict):
        return None
    market_id = _non_empty(item.get("market_id"))
    valid_time = _valid_time_identity(item.get("target_valid_time"))
    proxy_version = _non_empty(item.get("proxy_method_version"))
    weight_version = _non_empty(item.get("weight_version"))
    if (
        market_id is None
        or valid_time is None
        or proxy_version is None
        or weight_version is None
    ):
        return None
    return market_id, valid_time, proxy_version, weight_version


def compare_market_risk_versions(
    current: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    *,
    current_run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if previous is None:
        return _unavailable_change("no_previous_same_release", previous_run_id=None)

    previous_run_id = _non_empty(previous.get("forecast_run_id"))
    previous_risks = previous.get("market_risk_snapshots")
    if previous_run_id is None or not isinstance(previous_risks, list):
        return _unavailable_change(
            "previous_version_incomplete",
            previous_run_id=previous_run_id,
        )
    if current_run_metadata is not None and not _comparison_provenance_complete(
        current_run_metadata
    ):
        return _unavailable_change(
            "current_comparison_provenance_incomplete",
            previous_run_id=previous_run_id,
        )
    if not _comparison_provenance_complete(previous):
        return _unavailable_change(
            "previous_comparison_provenance_incomplete",
            previous_run_id=previous_run_id,
        )

    previous_by_market = {
        str(item.get("market_id")): item
        for item in previous_risks
        if isinstance(item, dict) and _non_empty(item.get("market_id"))
    }
    current_by_market = {
        str(item.get("market_id")): item
        for item in current
        if isinstance(item, dict) and _non_empty(item.get("market_id"))
    }
    common_markets = sorted(set(previous_by_market) & set(current_by_market))
    if not common_markets:
        return _unavailable_change(
            "no_comparable_markets",
            previous_run_id=previous_run_id,
        )

    aligned_markets: list[str] = []
    mismatched_valid_time = False
    mismatched_proxy_version = False
    mismatched_weight_version = False
    incomplete_identity = False
    current_methodology_conflict = False
    previous_methodology_conflict = False
    current_proxy_version = (
        _non_empty(current_run_metadata.get("proxy_method_version"))
        if current_run_metadata is not None
        else None
    )
    current_weight_version = (
        _non_empty(current_run_metadata.get("weight_version"))
        if current_run_metadata is not None
        else None
    )
    previous_proxy_version = _non_empty(previous.get("proxy_method_version"))
    previous_weight_version = _non_empty(previous.get("weight_version"))
    comparison_identity: tuple[tuple[str, str, str], str, str] | None = None
    for market_id in common_markets:
        current_identity = _risk_comparison_identity(current_by_market[market_id])
        previous_identity = _risk_comparison_identity(previous_by_market[market_id])
        if current_identity is None or previous_identity is None:
            incomplete_identity = True
            continue
        if current_run_metadata is not None and (
            current_identity[2] != current_proxy_version
            or current_identity[3] != current_weight_version
        ):
            current_methodology_conflict = True
            continue
        if (
            previous_identity[2] != previous_proxy_version
            or previous_identity[3] != previous_weight_version
        ):
            previous_methodology_conflict = True
            continue
        if current_identity[1] != previous_identity[1]:
            mismatched_valid_time = True
            continue
        if current_identity[2] != previous_identity[2]:
            mismatched_proxy_version = True
            continue
        if current_identity[3] != previous_identity[3]:
            mismatched_weight_version = True
            continue
        aligned_markets.append(market_id)
        comparison_identity = (
            current_identity[1],
            current_identity[2],
            current_identity[3],
        )

    if not aligned_markets:
        if current_methodology_conflict:
            reason = "current_methodology_metadata_conflict"
        elif previous_methodology_conflict:
            reason = "previous_methodology_metadata_conflict"
        elif mismatched_valid_time:
            reason = "target_valid_time_mismatch"
        elif mismatched_proxy_version:
            reason = "proxy_method_version_mismatch"
        elif mismatched_weight_version:
            reason = "weight_version_mismatch"
        elif incomplete_identity:
            reason = "market_comparison_metadata_incomplete"
        else:
            reason = "no_comparable_markets"
        return _unavailable_change(reason, previous_run_id=previous_run_id)

    upgraded = sum(
        int(current_by_market[market].get("severity") or 0)
        > int(previous_by_market[market].get("severity") or 0)
        for market in aligned_markets
    )
    downgraded = sum(
        int(current_by_market[market].get("severity") or 0)
        < int(previous_by_market[market].get("severity") or 0)
        for market in aligned_markets
    )
    unchanged = len(aligned_markets) - upgraded - downgraded
    assert comparison_identity is not None
    valid_time, proxy_version, weight_version = comparison_identity
    valid_time_payload = {
        "start": valid_time[0],
        "end": valid_time[1],
        "timezone": valid_time[2],
    }
    return {
        "status": "available",
        "reason": "aligned_region_valid_time_and_methodology",
        "previous_run_id": previous_run_id,
        "previous_report_date": previous.get("report_date"),
        "comparable_markets": len(aligned_markets),
        "upgraded_markets": upgraded,
        "downgraded_markets": downgraded,
        "unchanged_markets": unchanged,
        "excluded_markets": len(common_markets) - len(aligned_markets),
        "comparison_basis": {
            "current_target_valid_time": valid_time_payload,
            "previous_target_valid_time": valid_time_payload,
            "proxy_method_version": proxy_version,
            "weight_version": weight_version,
        },
    }
