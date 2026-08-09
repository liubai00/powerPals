from datetime import datetime, timezone

import pytest

from services.weather_bot.data_provenance import (
    DataAvailabilityGate,
    ExternalDataRecord,
    external_record_from_provider_forecast,
)
from services.weather_bot.models import ForecastPoint, ProviderForecast


NOW = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)


def make_structured_record(**overrides) -> ExternalDataRecord:
    values = {
        "source_id": "official-weather-api",
        "source_kind": "structured_api",
        "source_url": "https://data.example.gov.cn/api/weather",
        "retrieved_at": "2026-08-08T23:55:00+00:00",
        "provider_issued_at": "2026-08-08T23:00:00+00:00",
        "valid_time": "2026-08-09T00:00:00+00:00/2026-08-10T00:00:00+00:00",
        "unit": "degC",
        "granularity": "1h",
        "coverage": "station:54511",
        "timezone": "Asia/Shanghai",
        "license_status": "verified",
        "allowed_uses": {"calculation", "derived_storage"},
        "completeness": 1.0,
        "quality_status": "good",
        "fresh_until": "2026-08-09T01:00:00+00:00",
        "content_sha256": "a" * 64,
        "structured_values": True,
        "retention_policy": "derived_only",
    }
    values.update(overrides)
    return ExternalDataRecord(**values)


def test_structured_external_api_with_complete_provenance_is_allowed_for_calculation():
    decision = DataAvailabilityGate().evaluate(make_structured_record(), now=NOW)

    assert decision.status == "allowed_for_calculation"
    assert decision.reason == "eligible"
    assert decision.raw_storage_allowed is False
    assert decision.derived_storage_allowed is True


def test_search_summary_without_original_source_is_rejected():
    record = ExternalDataRecord(
        source_id="web-search",
        source_kind="search_discovery",
        source_url="https://search.example/result/123",
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"text_reference"},
        structured_values=False,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "rejected"
    assert decision.reason == "search_without_original_source"


def test_official_document_without_structured_values_is_text_only():
    record = ExternalDataRecord(
        source_id="official-notice",
        source_kind="official_document",
        source_url="https://www.example.gov.cn/notices/2026/123.html",
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"text_reference"},
        structured_values=False,
        content_sha256="b" * 64,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "text_only"
    assert decision.reason == "official_text_only"


def test_official_document_without_an_original_url_is_rejected():
    record = ExternalDataRecord(
        source_id="official-notice",
        source_kind="official_document",
        source_url=None,
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"text_reference"},
        structured_values=False,
        content_sha256="b" * 64,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "rejected"
    assert decision.reason == "missing_source_url"


def test_official_document_with_a_non_url_source_is_rejected():
    record = ExternalDataRecord(
        source_id="official-notice",
        source_kind="official_document",
        source_url="copied from a news summary",
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"text_reference"},
        structured_values=False,
        content_sha256="b" * 64,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "rejected"
    assert decision.reason == "invalid_source_url"


def test_search_discovery_with_original_link_remains_text_only():
    record = ExternalDataRecord(
        source_id="web-search",
        source_kind="search_discovery",
        source_url="https://search.example/result/456",
        original_source_url="https://www.example.gov.cn/disclosure/456.html",
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"text_reference"},
        structured_values=True,
        content_sha256="d" * 64,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "text_only"
    assert decision.reason == "search_discovery_only"


@pytest.mark.parametrize("source_kind", ["search_discovery", "official_document"])
def test_text_sources_without_text_reference_permission_are_rejected(source_kind):
    record = ExternalDataRecord(
        source_id="external-text",
        source_kind=source_kind,
        source_url="https://search.example/result/permission",
        original_source_url="https://www.example.gov.cn/disclosure/permission.html",
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"calculation"},
        structured_values=False,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "rejected"
    assert decision.reason == "text_reference_not_permitted"


def test_text_source_without_retrieval_provenance_is_rejected():
    record = ExternalDataRecord(
        source_id="official-notice",
        source_kind="official_document",
        source_url="https://www.example.gov.cn/notices/2026/789.html",
        retrieved_at=None,
        license_status="verified",
        allowed_uses={"text_reference"},
        content_sha256="e" * 64,
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "rejected"
    assert decision.reason == "missing_required_metadata"
    assert decision.missing_fields == ("retrieved_at",)


def test_unknown_license_fails_closed():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(license_status="unknown", allowed_uses=set()),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "license_unknown"


def test_forbidden_license_is_rejected():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(license_status="forbidden", allowed_uses=set()),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "license_forbidden"


def test_verified_source_without_calculation_scope_is_text_only():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(allowed_uses={"text_reference"}),
        now=NOW,
    )

    assert decision.status == "text_only"
    assert decision.reason == "license_text_only"


def test_text_only_api_still_requires_source_provenance():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(allowed_uses={"text_reference"}, source_url=None),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "missing_required_metadata"
    assert decision.missing_fields == ("source_url",)


def test_verified_source_without_any_permitted_use_is_rejected():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(allowed_uses={"derived_storage"}),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "calculation_not_permitted"


@pytest.mark.parametrize(
    "missing_field",
    [
        "source_id",
        "source_url",
        "retrieved_at",
        "valid_time",
        "unit",
        "granularity",
        "coverage",
        "timezone",
        "completeness",
        "quality_status",
        "fresh_until",
        "content_sha256",
    ],
)
def test_missing_key_calculation_metadata_is_rejected(missing_field):
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(**{missing_field: None}),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "missing_required_metadata"
    assert decision.missing_fields == (missing_field,)


def test_structured_source_requires_a_verifiable_url():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(source_url="provider name only"),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "invalid_source_url"


def test_invalid_content_hash_is_rejected():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(content_sha256="not-a-sha256"),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "invalid_content_sha256"


def test_replay_request_id_can_replace_a_content_hash():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(content_sha256=None, replay_request_id="weather-run-20260809-001"),
        now=NOW,
    )

    assert decision.status == "allowed_for_calculation"
    assert decision.reason == "eligible"


def test_expired_cached_snapshot_is_rejected_as_stale():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(
            source_kind="cached_snapshot",
            fresh_until="2026-08-08T23:59:59+00:00",
        ),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "stale"
    assert decision.stale is True


def test_incomplete_external_data_is_rejected():
    decision = DataAvailabilityGate(min_completeness=0.95).evaluate(
        make_structured_record(completeness=0.79),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "insufficient_completeness"


def test_unusable_quality_is_rejected():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(quality_status="unusable"),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "unusable_quality"


def test_degraded_quality_requires_a_reason():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(quality_status="degraded", degradation_reason=None),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "missing_degradation_reason"


def test_usable_degraded_data_is_explicitly_labeled():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(
            quality_status="degraded",
            degradation_reason="one provider unavailable; remaining coverage is complete",
        ),
        now=NOW,
    )

    assert decision.status == "allowed_for_calculation"
    assert decision.reason == "eligible_degraded"


def test_api_without_structured_values_is_rejected():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(structured_values=False),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "no_structured_values"


def test_timestamp_without_timezone_is_rejected_instead_of_compared_as_fresh():
    decision = DataAvailabilityGate().evaluate(
        make_structured_record(fresh_until="2026-08-09T01:00:00"),
        now=NOW,
    )

    assert decision.status == "rejected"
    assert decision.reason == "invalid_timestamp_timezone"


def test_search_discovery_requires_a_verifiable_original_url():
    record = ExternalDataRecord(
        source_id="web-search",
        source_kind="search_discovery",
        source_url="https://search.example/result/789",
        original_source_url="official notice mentioned in snippet",
        retrieved_at="2026-08-08T23:55:00+00:00",
        license_status="verified",
        allowed_uses={"text_reference"},
    )

    decision = DataAvailabilityGate().evaluate(record, now=NOW)

    assert decision.status == "rejected"
    assert decision.reason == "invalid_original_source_url"


def test_provider_forecast_bridge_copies_provenance_but_does_not_assume_a_license():
    forecast = ProviderForecast(
        provider="open_meteo",
        points=[ForecastPoint(time="2026-08-09T00:00:00+00:00", temperature=30.0)],
        retrieved_at="2026-08-08T23:55:00+00:00",
        provider_issued_at="2026-08-08T23:00:00+00:00",
        source_url="https://api.open-meteo.com/v1/forecast",
        content_sha256="c" * 64,
        retention_policy="derived_only",
    )

    record = external_record_from_provider_forecast(
        forecast,
        valid_time="2026-08-09T00:00:00+00:00/2026-08-09T01:00:00+00:00",
        unit="degC",
        granularity="1h",
        coverage="point:23.1,113.3",
        timezone="Asia/Shanghai",
        completeness=1.0,
        quality_status="good",
        fresh_until="2026-08-09T01:00:00+00:00",
    )

    assert record.source_id == "open_meteo"
    assert record.source_url == "https://api.open-meteo.com/v1/forecast"
    assert record.content_sha256 == "c" * 64
    assert record.retention_policy == "derived_only"
    assert record.license_status == "unknown"
    decision = DataAvailabilityGate().evaluate(record, now=NOW)
    assert decision.status == "rejected"
    assert decision.reason == "license_unknown"
