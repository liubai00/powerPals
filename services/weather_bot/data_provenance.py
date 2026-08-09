from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field


SourceKind = Literal[
    "structured_api",
    "official_dataset",
    "official_document",
    "search_discovery",
    "cached_snapshot",
]
LicenseStatus = Literal["unknown", "verified", "forbidden"]
AllowedUse = Literal["calculation", "text_reference", "derived_storage", "raw_storage"]
AvailabilityStatus = Literal["allowed_for_calculation", "text_only", "rejected"]
AvailabilityReason = Literal[
    "eligible",
    "eligible_degraded",
    "search_without_original_source",
    "invalid_original_source_url",
    "search_discovery_only",
    "official_text_only",
    "license_unknown",
    "license_forbidden",
    "license_text_only",
    "text_reference_not_permitted",
    "calculation_not_permitted",
    "missing_source_url",
    "invalid_source_url",
    "no_structured_values",
    "missing_required_metadata",
    "invalid_content_sha256",
    "invalid_timestamp_timezone",
    "stale",
    "insufficient_completeness",
    "unusable_quality",
    "missing_degradation_reason",
]


class ExternalDataRecord(BaseModel):
    """Provenance supplied by an external adapter; this is not locally owned data."""

    source_id: str | None = None
    source_kind: SourceKind
    source_url: str | None = None
    original_source_url: str | None = None
    retrieved_at: datetime | None = None
    provider_issued_at: datetime | None = None
    valid_time: str | None = None
    unit: str | None = None
    granularity: str | None = None
    coverage: str | None = None
    timezone: str | None = None
    license_status: LicenseStatus = "unknown"
    allowed_uses: set[AllowedUse] = Field(default_factory=set)
    completeness: float | None = Field(default=None, ge=0.0, le=1.0)
    quality_status: Literal["good", "degraded", "unusable"] | None = None
    degradation_reason: str | None = None
    fresh_until: datetime | None = None
    content_sha256: str | None = None
    replay_request_id: str | None = None
    structured_values: bool = False
    retention_policy: Literal["derived_only", "metadata_only", "raw_until_expiry"] = "derived_only"


class AvailabilityDecision(BaseModel):
    status: AvailabilityStatus
    reason: AvailabilityReason
    missing_fields: tuple[str, ...] = ()
    stale: bool = False
    raw_storage_allowed: bool = False
    derived_storage_allowed: bool = False


class DataAvailabilityGate:
    """Fail-closed admission gate for facts obtained from external sources."""

    def __init__(self, *, min_completeness: float = 0.95) -> None:
        if not 0.0 <= min_completeness <= 1.0:
            raise ValueError("min_completeness must be between 0 and 1")
        self.min_completeness = min_completeness

    def evaluate(self, record: ExternalDataRecord, *, now: datetime) -> AvailabilityDecision:
        if record.source_kind == "search_discovery" and not _present(record.original_source_url):
            return AvailabilityDecision(status="rejected", reason="search_without_original_source")
        if record.source_kind == "search_discovery" and not _valid_web_url(record.original_source_url):
            return AvailabilityDecision(status="rejected", reason="invalid_original_source_url")
        if record.license_status == "unknown":
            return AvailabilityDecision(status="rejected", reason="license_unknown")
        if record.license_status == "forbidden":
            return AvailabilityDecision(status="rejected", reason="license_forbidden")
        if (
            record.source_kind in {"search_discovery", "official_document"}
            and "text_reference" not in record.allowed_uses
        ):
            return AvailabilityDecision(status="rejected", reason="text_reference_not_permitted")
        if record.source_kind == "official_document" and not _present(record.source_url):
            return AvailabilityDecision(status="rejected", reason="missing_source_url")
        if record.source_kind == "official_document" and not _valid_web_url(record.source_url):
            return AvailabilityDecision(status="rejected", reason="invalid_source_url")
        if record.source_kind in {"search_discovery", "official_document"}:
            missing_text_fields: list[str] = []
            if not _present(record.source_id):
                missing_text_fields.append("source_id")
            if record.retrieved_at is None:
                missing_text_fields.append("retrieved_at")
            if not _present(record.content_sha256) and not _present(record.replay_request_id):
                missing_text_fields.append("content_sha256")
            if missing_text_fields:
                return AvailabilityDecision(
                    status="rejected",
                    reason="missing_required_metadata",
                    missing_fields=tuple(missing_text_fields),
                )
            if _present(record.content_sha256) and not _valid_content_sha256(record.content_sha256):
                return AvailabilityDecision(status="rejected", reason="invalid_content_sha256")
            if any(
                value is not None and not _timezone_aware(value)
                for value in (now, record.retrieved_at, record.provider_issued_at)
            ):
                return AvailabilityDecision(status="rejected", reason="invalid_timestamp_timezone")
            reason = "search_discovery_only" if record.source_kind == "search_discovery" else "official_text_only"
            return AvailabilityDecision(status="text_only", reason=reason)
        if "calculation" not in record.allowed_uses and "text_reference" in record.allowed_uses:
            missing_text_fields: list[str] = []
            for field_name in ("source_id", "source_url"):
                if not _present(getattr(record, field_name)):
                    missing_text_fields.append(field_name)
            if record.retrieved_at is None:
                missing_text_fields.append("retrieved_at")
            if not _present(record.content_sha256) and not _present(record.replay_request_id):
                missing_text_fields.append("content_sha256")
            if missing_text_fields:
                return AvailabilityDecision(
                    status="rejected",
                    reason="missing_required_metadata",
                    missing_fields=tuple(missing_text_fields),
                )
            if not _valid_web_url(record.source_url):
                return AvailabilityDecision(status="rejected", reason="invalid_source_url")
            if _present(record.content_sha256) and not _valid_content_sha256(record.content_sha256):
                return AvailabilityDecision(status="rejected", reason="invalid_content_sha256")
            if any(
                value is not None and not _timezone_aware(value)
                for value in (now, record.retrieved_at, record.provider_issued_at)
            ):
                return AvailabilityDecision(status="rejected", reason="invalid_timestamp_timezone")
            return AvailabilityDecision(status="text_only", reason="license_text_only")
        if "calculation" not in record.allowed_uses:
            return AvailabilityDecision(status="rejected", reason="calculation_not_permitted")
        if not record.structured_values:
            return AvailabilityDecision(status="rejected", reason="no_structured_values")
        missing: list[str] = []
        for field_name in (
            "source_id",
            "source_url",
            "valid_time",
            "unit",
            "granularity",
            "coverage",
            "timezone",
            "quality_status",
        ):
            if not _present(getattr(record, field_name)):
                missing.append(field_name)
        for field_name in ("retrieved_at", "completeness", "fresh_until"):
            if getattr(record, field_name) is None:
                missing.append(field_name)
        if not _present(record.content_sha256) and not _present(record.replay_request_id):
            missing.append("content_sha256")
        missing_fields = tuple(missing)
        if missing_fields:
            return AvailabilityDecision(
                status="rejected",
                reason="missing_required_metadata",
                missing_fields=missing_fields,
            )
        if not _valid_web_url(record.source_url):
            return AvailabilityDecision(status="rejected", reason="invalid_source_url")
        if _present(record.content_sha256) and not _valid_content_sha256(record.content_sha256):
            return AvailabilityDecision(status="rejected", reason="invalid_content_sha256")
        timestamps = (now, record.retrieved_at, record.provider_issued_at, record.fresh_until)
        if any(value is not None and not _timezone_aware(value) for value in timestamps):
            return AvailabilityDecision(status="rejected", reason="invalid_timestamp_timezone")
        if record.fresh_until is not None and now >= record.fresh_until:
            return AvailabilityDecision(status="rejected", reason="stale", stale=True)
        if record.completeness is not None and record.completeness < self.min_completeness:
            return AvailabilityDecision(status="rejected", reason="insufficient_completeness")
        if record.quality_status == "unusable":
            return AvailabilityDecision(status="rejected", reason="unusable_quality")
        if record.quality_status == "degraded" and not _present(record.degradation_reason):
            return AvailabilityDecision(status="rejected", reason="missing_degradation_reason")
        return AvailabilityDecision(
            status="allowed_for_calculation",
            reason="eligible_degraded" if record.quality_status == "degraded" else "eligible",
            raw_storage_allowed=(
                "raw_storage" in record.allowed_uses and record.retention_policy == "raw_until_expiry"
            ),
            derived_storage_allowed="derived_storage" in record.allowed_uses,
        )


def external_record_from_provider_forecast(
    forecast: Any,
    *,
    valid_time: str | None,
    unit: str | None,
    granularity: str | None,
    coverage: str | None,
    timezone: str | None,
    completeness: float | None,
    quality_status: Literal["good", "degraded", "unusable"] | None,
    fresh_until: datetime | str | None,
    degradation_reason: str | None = None,
    license_status: LicenseStatus = "unknown",
    allowed_uses: set[AllowedUse] | None = None,
) -> ExternalDataRecord:
    """Map ProviderForecast provenance without inventing source permission or ownership."""

    return ExternalDataRecord(
        source_id=getattr(forecast, "provider", None),
        source_kind="structured_api",
        source_url=getattr(forecast, "source_url", None),
        retrieved_at=getattr(forecast, "retrieved_at", None),
        provider_issued_at=getattr(forecast, "provider_issued_at", None),
        valid_time=valid_time,
        unit=unit,
        granularity=granularity,
        coverage=coverage,
        timezone=timezone,
        license_status=license_status,
        allowed_uses=allowed_uses or set(),
        completeness=completeness,
        quality_status=quality_status,
        degradation_reason=degradation_reason,
        fresh_until=fresh_until,
        content_sha256=getattr(forecast, "content_sha256", None),
        structured_values=bool(getattr(forecast, "points", None) or getattr(forecast, "daily", None)),
        retention_policy=getattr(forecast, "retention_policy", "derived_only"),
    )


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _valid_web_url(value: str | None) -> bool:
    if not _present(value):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _valid_content_sha256(value: str | None) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value or ""))
