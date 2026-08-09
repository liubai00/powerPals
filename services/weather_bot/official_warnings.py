from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


PROVIDER_ID = "qweather_official_warning"
WARNING_ENDPOINT_PREFIX = "/weatheralert/v1/current/"
REQUIRED_POLICY_METRICS = frozenset(
    {
        "warning_id",
        "headline",
        "original_issuer",
        "published_at",
        "effective_at",
        "expires_at",
        "message_type",
        "source_tag",
    }
)


class QWeatherWarningClientConfig(BaseModel):
    """Credentials and endpoint selection for the QWeather warning API."""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = None
    api_host: str = "devapi.qweather.com"
    timeout_seconds: float = Field(default=8.0, gt=0.0, le=30.0)


class OfficialWarning(BaseModel):
    """Minimal, traceable metadata for one currently effective official warning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    warning_id: str
    headline: str
    original_issuer: str
    published_at: datetime
    retrieved_at: datetime
    effective_at: datetime
    expires_at: datetime
    source_url: str
    content_sha256: str
    source_tag: str
    message_type: Literal["Alert", "Update"]
    attribution: str


class OfficialWarningFetchResult(BaseModel):
    """Fail-closed result: unavailable responses never contain warning facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "unavailable"]
    reason: str
    source_tag: str | None = None
    zero_result: bool | None = None
    attribution: str | None = None
    retrieved_at: datetime | None = None
    source_url: str | None = None
    content_sha256: str | None = None
    warnings: tuple[OfficialWarning, ...] = ()


@dataclass(frozen=True)
class _ParsedAlert:
    warning_id: str
    headline: str
    original_issuer: str
    published_at: datetime
    effective_at: datetime
    expires_at: datetime
    message_type: str


async def fetch_official_warnings(
    latitude: float | Decimal | str,
    longitude: float | Decimal | str,
    config: QWeatherWarningClientConfig,
    *,
    source_registry: SourceRegistry | None,
    source_policy: SourcePolicy | None,
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> OfficialWarningFetchResult:
    """Fetch current alerts for finite coordinates of at most six decimal places.

    Provider bodies are used only in memory for schema validation and hashing; no
    description, instruction, search summary, credential, or raw body is returned.
    """

    endpoint_prefix = _endpoint_prefix(config.api_host)
    latitude_path = _coordinate(latitude, minimum=-90, maximum=90)
    longitude_path = _coordinate(longitude, minimum=-180, maximum=180)
    endpoint = (
        f"{endpoint_prefix}{latitude_path}/{longitude_path}"
        if endpoint_prefix and latitude_path is not None and longitude_path is not None
        else None
    )
    api_key = (
        config.api_key.get_secret_value().strip() if config.api_key is not None else ""
    )
    if not endpoint or not api_key:
        return _unavailable("configuration_rejected")
    if not _verified_policy(source_registry, source_policy, endpoint):
        return _unavailable("source_policy_rejected")

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=config.timeout_seconds,
        follow_redirects=False,
    )
    try:
        response = await client.get(
            endpoint,
            headers={"X-QW-Api-Key": api_key},
        )
        response.raise_for_status()
        if not _response_matches_endpoint(response, endpoint):
            return _unavailable("endpoint_mismatch")
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return _unavailable("provider_unavailable")
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(body, dict):
        return _unavailable("provider_response_rejected")
    metadata = body.get("metadata")
    raw_warnings = body.get("alerts")
    if not isinstance(metadata, dict) or not isinstance(raw_warnings, list):
        return _unavailable("provider_response_rejected")

    retrieved_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if not _timezone_aware(retrieved_at):
        return _unavailable("clock_rejected")
    content_hash = sha256(response.content).hexdigest()
    source_tag = _required_text(metadata.get("tag"))
    zero_result = metadata.get("zeroResult")
    attributions = _normalized_attributions(metadata.get("attributions"))
    required_attribution = (source_policy.attribution_text or "").strip()
    if (
        source_tag is None
        or not isinstance(zero_result, bool)
        or attributions is None
        or required_attribution.casefold() not in {item.casefold() for item in attributions}
    ):
        return _unavailable("provider_response_rejected")
    attribution = "；".join(attributions)
    if zero_result:
        if raw_warnings:
            return _unavailable("provider_response_rejected")
        return OfficialWarningFetchResult(
            status="ok",
            reason="no_active_warnings",
            source_tag=source_tag,
            zero_result=True,
            attribution=attribution,
            retrieved_at=retrieved_at,
            source_url=endpoint,
            content_sha256=content_hash,
            warnings=(),
        )
    if not raw_warnings:
        return _unavailable("provider_response_rejected")

    warnings: list[OfficialWarning] = []
    for item in raw_warnings:
        lifecycle = _warning_lifecycle(item)
        if lifecycle == "inactive":
            parsed_alert = _parse_alert_metadata(item)
            if parsed_alert is None:
                return _unavailable("provider_response_rejected")
            continue
        if lifecycle != "active":
            return _unavailable("provider_response_rejected")
        parsed_alert = _parse_alert_metadata(item)
        if parsed_alert is None:
            return _unavailable("provider_response_rejected")
        warning = _build_warning(
            parsed_alert,
            retrieved_at=retrieved_at,
            endpoint=endpoint,
            content_hash=content_hash,
            source_tag=source_tag,
            attribution=attribution,
        )
        if warning.expires_at <= retrieved_at:
            continue
        warnings.append(warning)
    if not warnings:
        return _unavailable("no_verified_active_alert")
    return OfficialWarningFetchResult(
        status="ok",
        reason="active_warnings",
        source_tag=source_tag,
        zero_result=zero_result,
        attribution=attribution,
        retrieved_at=retrieved_at,
        source_url=endpoint,
        content_sha256=content_hash,
        warnings=tuple(warnings),
    )


def _parse_alert_metadata(item: object) -> _ParsedAlert | None:
    if not isinstance(item, dict):
        return None
    required = {
        "id": item.get("id"),
        "headline": item.get("headline"),
        "senderName": item.get("senderName"),
    }
    if any(not isinstance(value, str) or not value.strip() for value in required.values()):
        return None
    message_type = _message_type_code(item.get("messageType"))
    if message_type is None:
        return None
    published_at = _parse_datetime(item.get("issuedTime"))
    effective_at = _parse_datetime(item.get("effectiveTime"))
    expires_at = _parse_datetime(item.get("expireTime"))
    if published_at is None or effective_at is None or expires_at is None:
        return None
    if effective_at >= expires_at or published_at >= expires_at:
        return None
    return _ParsedAlert(
        warning_id=str(required["id"]).strip(),
        headline=str(required["headline"]).strip(),
        original_issuer=str(required["senderName"]).strip(),
        published_at=published_at,
        effective_at=effective_at,
        expires_at=expires_at,
        message_type=message_type.title(),
    )


def _build_warning(
    parsed: _ParsedAlert,
    *,
    retrieved_at: datetime,
    endpoint: str,
    content_hash: str,
    source_tag: str,
    attribution: str,
) -> OfficialWarning:
    return OfficialWarning(
        warning_id=parsed.warning_id,
        headline=parsed.headline,
        original_issuer=parsed.original_issuer,
        published_at=parsed.published_at,
        retrieved_at=retrieved_at,
        effective_at=parsed.effective_at,
        expires_at=parsed.expires_at,
        source_url=endpoint,
        content_sha256=content_hash,
        source_tag=source_tag,
        message_type=parsed.message_type,
        attribution=attribution,
    )


def _warning_lifecycle(item: object) -> Literal["active", "inactive", "invalid"]:
    if not isinstance(item, dict):
        return "invalid"
    message_type = _message_type_code(item.get("messageType"))
    if message_type is None:
        return "invalid"
    if message_type == "cancel":
        return "inactive"
    if message_type in {"alert", "update"}:
        return "active"
    return "invalid"


def _message_type_code(value: object) -> str | None:
    """Validate the current API's structured messageType object.

    QWeather documents ``messageType.code`` and ``messageType.supersedes``;
    accepting the legacy flat string here would silently misread the current
    response schema.
    """

    if not isinstance(value, dict):
        return None
    code_value = value.get("code")
    if not isinstance(code_value, str):
        return None
    code = code_value.strip().casefold()
    if code not in {"alert", "update", "cancel"}:
        return None
    supersedes = value.get("supersedes")
    if supersedes is not None and not (
        isinstance(supersedes, list)
        and all(isinstance(item, str) and item.strip() for item in supersedes)
    ):
        return None
    if code in {"update", "cancel"} and not supersedes:
        return None
    return code


def _endpoint_prefix(api_host: str) -> str | None:
    host = api_host.strip().lower()
    if not host or "://" in host:
        return None
    parsed = urlsplit(f"https://{host}")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"https://{parsed.netloc}{WARNING_ENDPOINT_PREFIX}"


def _coordinate(
    value: float | Decimal | str,
    *,
    minimum: int,
    maximum: int,
) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        coordinate = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not coordinate.is_finite() or coordinate < minimum or coordinate > maximum:
        return None
    # QWeather documents decimal-degree numbers and does not impose a two-digit
    # precision limit.  Six decimals preserves configured points without silently
    # moving them, while rejecting unbounded path precision.
    if coordinate.normalize().as_tuple().exponent < -6:
        return None
    normalized = format(coordinate, "f").rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _required_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized_attributions(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    normalized = tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )
    return normalized if len(normalized) == len(value) else None


def _verified_policy(
    registry: SourceRegistry | None,
    policy: SourcePolicy | None,
    endpoint: str,
) -> bool:
    if registry is None or policy is None:
        return False
    if policy.provider != PROVIDER_ID or policy.environment != registry.environment:
        return False
    resolved = registry.resolve(PROVIDER_ID, endpoint)
    parsed_endpoint = urlsplit(endpoint)
    required_prefix = (
        f"{parsed_endpoint.scheme}://{parsed_endpoint.netloc}{WARNING_ENDPOINT_PREFIX}"
    )
    return bool(
        resolved == policy
        and policy.license_status == "verified"
        and "text_reference" in policy.allowed_uses
        and required_prefix in policy.source_url_prefixes
        and REQUIRED_POLICY_METRICS.issubset(policy.required_metrics)
        and policy.retention_policy in {"derived_only", "metadata_only"}
        and policy.attribution_required
        and (policy.attribution_text or "").strip()
    )


def _response_matches_endpoint(response: httpx.Response, endpoint: str) -> bool:
    actual = response.request.url
    expected = httpx.URL(endpoint)
    return bool(
        not response.history
        and actual.scheme == expected.scheme
        and actual.host == expected.host
        and actual.port == expected.port
        and actual.path == expected.path
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if _timezone_aware(parsed) else None


def _timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _unavailable(reason: str) -> OfficialWarningFetchResult:
    return OfficialWarningFetchResult(status="unavailable", reason=reason, warnings=())
