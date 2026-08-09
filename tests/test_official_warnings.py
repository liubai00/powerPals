from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

import httpx
import pytest

from services.weather_bot.official_warnings import (
    QWeatherWarningClientConfig,
    fetch_official_warnings,
)
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


NOW = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)
API_HOST = "warning-api.qweather.test"
SOURCE_PREFIX = f"https://{API_HOST}/weatheralert/v1/current/"
SOURCE_URL = f"{SOURCE_PREFIX}39.9/116.4"
SECRET = "qweather-secret-that-must-not-leak"
SOURCE_TAG = "qweather-alert-run-20260809-0855"


def _message_type(code: str, supersedes: list[str] | None = None) -> dict[str, object]:
    return {
        "code": code,
        "supersedes": supersedes,
    }


def _verified_policy(**overrides: object) -> SourcePolicy:
    values: dict[str, object] = {
        "provider": "qweather_official_warning",
        "environment": "test",
        "profile": "official-warning-current-api-test",
        "license_status": "verified",
        "allowed_uses": {"text_reference", "derived_storage"},
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": (SOURCE_PREFIX,),
        "unit_manifest": (
            "warning_id:text;headline:text;original_issuer:text;published_at:iso8601;"
            "effective_at:iso8601;expires_at:iso8601;message_type:text;source_tag:text"
        ),
        "required_metrics": (
            "warning_id",
            "headline",
            "original_issuer",
            "published_at",
            "effective_at",
            "expires_at",
            "message_type",
            "source_tag",
        ),
        "coverage_model": "latitude-longitude-point",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 600,
        "min_completeness": 1.0,
        "retention_policy": "metadata_only",
        "attribution_required": True,
        "attribution_text": "QWeather",
    }
    values.update(overrides)
    return SourcePolicy(**values)


def _registry(policy: SourcePolicy) -> SourceRegistry:
    return SourceRegistry([policy], environment="test")


def _active_alert(**overrides: object) -> dict[str, object]:
    alert: dict[str, object] = {
        "id": "101010100202608090001",
        "senderName": "北京市气象台",
        "issuedTime": "2026-08-09T08:30+08:00",
        "effectiveTime": "2026-08-09T08:30+08:00",
        "expireTime": "2026-08-09T18:30+08:00",
        "headline": "北京市发布暴雨黄色预警",
        "messageType": _message_type("alert"),
        "description": "这段原始正文不得被适配器持久化",
        "instruction": "这段处置说明也不得被适配器持久化",
        "searchSummary": "搜索摘要不得进入结构化预警",
    }
    alert.update(overrides)
    return alert


def _body(
    alerts: list[object] | None = None,
    *,
    tag: object = SOURCE_TAG,
    zero_result: object = False,
    attributions: object = None,
) -> dict[str, object]:
    return {
        "metadata": {
            "tag": tag,
            "zeroResult": zero_result,
            "attributions": ["QWeather"] if attributions is None else attributions,
        },
        "alerts": [_active_alert()] if alerts is None else alerts,
    }


async def _fetch_body(
    body: object,
    *,
    latitude: float | Decimal | str = 39.9,
    longitude: float | Decimal | str = 116.4,
    config: QWeatherWarningClientConfig | None = None,
    policy: SourcePolicy | None = None,
    registry: SourceRegistry | None = None,
    status_code: int = 200,
) -> tuple[Any, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body)

    resolved_policy = policy or _verified_policy()
    resolved_registry = registry if registry is not None else _registry(resolved_policy)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_official_warnings(
            latitude,
            longitude,
            config or QWeatherWarningClientConfig(api_key=SECRET, api_host=API_HOST),
            source_registry=resolved_registry,
            source_policy=resolved_policy,
            http_client=client,
            clock=lambda: NOW,
        )
    return result, requests


@pytest.mark.asyncio
async def test_active_official_alert_exposes_only_normalized_traceable_metadata() -> None:
    body = _body(attributions=["QWeather", "国家预警信息发布中心"])
    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == SOURCE_URL
        assert request.headers["X-QW-Api-Key"] == SECRET
        assert not request.url.query
        return httpx.Response(200, content=raw_body, headers={"content-type": "application/json"})

    policy = _verified_policy()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_official_warnings(
            39.9,
            116.4,
            QWeatherWarningClientConfig(api_key=SECRET, api_host=API_HOST),
            source_registry=_registry(policy),
            source_policy=policy,
            http_client=client,
            clock=lambda: NOW,
        )

    assert result.status == "ok"
    assert result.reason == "active_warnings"
    assert result.source_tag == SOURCE_TAG
    assert result.zero_result is False
    assert result.attribution == "QWeather；国家预警信息发布中心"
    assert result.retrieved_at == NOW
    assert result.source_url == SOURCE_URL
    assert result.content_sha256 == sha256(raw_body).hexdigest()
    assert len(result.warnings) == 1

    alert = result.warnings[0]
    assert alert.warning_id == "101010100202608090001"
    assert alert.headline == "北京市发布暴雨黄色预警"
    assert alert.original_issuer == "北京市气象台"
    assert alert.published_at.isoformat() == "2026-08-09T08:30:00+08:00"
    assert alert.retrieved_at == NOW
    assert alert.effective_at.isoformat() == "2026-08-09T08:30:00+08:00"
    assert alert.expires_at.isoformat() == "2026-08-09T18:30:00+08:00"
    assert alert.message_type == "Alert"
    assert alert.source_tag == SOURCE_TAG
    assert alert.source_url == SOURCE_URL
    assert alert.content_sha256 == sha256(raw_body).hexdigest()
    assert alert.attribution == result.attribution

    serialized = result.model_dump_json()
    assert SECRET not in serialized
    assert "这段原始正文" not in serialized
    assert "这段处置说明" not in serialized
    assert "搜索摘要" not in serialized
    assert "description" not in serialized
    assert "instruction" not in serialized
    assert "raw" not in type(alert).model_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (39.1234567, 116.4),
        (39.9, 116.1234567),
        (90.01, 116.4),
        (39.9, 180.01),
        ("NaN", 116.4),
        (39.9, "Infinity"),
        ("39.9/path", 116.4),
        (True, 116.4),
    ],
)
async def test_coordinates_outside_range_or_over_six_decimals_fail_before_http(
    latitude: float | str | bool,
    longitude: float | str,
) -> None:
    result, requests = await _fetch_body(
        _body([], zero_result=True),
        latitude=latitude,  # type: ignore[arg-type]
        longitude=longitude,
    )

    assert result.status == "unavailable"
    assert result.reason == "configuration_rejected"
    assert result.warnings == ()
    assert requests == []


@pytest.mark.asyncio
async def test_builtin_location_precision_is_preserved_in_current_path_endpoint() -> None:
    policy = _verified_policy(
        source_url_prefixes=(f"https://{API_HOST}/weatheralert/v1/current/",)
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_body(alerts=[], zero_result=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_official_warnings(
            36.6512,
            117.1201,
            QWeatherWarningClientConfig(api_key=SECRET, api_host=API_HOST),
            source_registry=_registry(policy),
            source_policy=policy,
            http_client=client,
            clock=lambda: NOW,
        )

    assert result.status == "ok"
    assert str(requests[0].url).endswith(
        "/weatheralert/v1/current/36.6512/117.1201"
    )


@pytest.mark.asyncio
async def test_coordinates_are_canonicalized_into_the_current_path_endpoint() -> None:
    expected_url = f"{SOURCE_PREFIX}-33.87/151.21"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == expected_url
        return httpx.Response(200, json=_body([], zero_result=True))

    policy = _verified_policy()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_official_warnings(
            Decimal("-33.870"),
            Decimal("151.210"),
            QWeatherWarningClientConfig(api_key=SECRET, api_host=API_HOST),
            source_registry=_registry(policy),
            source_policy=policy,
            http_client=client,
            clock=lambda: NOW,
        )

    assert result.status == "ok"
    assert result.reason == "no_active_warnings"
    assert result.source_url == expected_url


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", [None, "", "   "])
async def test_missing_or_blank_api_key_fails_before_http(api_key: str | None) -> None:
    result, requests = await _fetch_body(
        _body([], zero_result=True),
        config=QWeatherWarningClientConfig(api_key=api_key, api_host=API_HOST),
    )

    assert result.status == "unavailable"
    assert result.reason == "configuration_rejected"
    assert result.warnings == ()
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy_case",
    [
        "missing_registry",
        "missing_policy",
        "unverified",
        "not_registered",
        "old_endpoint",
        "overbroad_endpoint",
        "text_use_not_permitted",
        "missing_attribution",
        "attribution_not_required",
        "wrong_policy_schema",
    ],
)
async def test_source_requires_an_exact_verified_current_endpoint_policy(
    policy_case: str,
) -> None:
    policy: SourcePolicy | None = _verified_policy()
    registry: SourceRegistry | None = _registry(policy)
    if policy_case == "missing_registry":
        registry = SourceRegistry([], environment="different-environment")
    elif policy_case == "missing_policy":
        policy = SourcePolicy.unconfigured("qweather_official_warning", "test")
        registry = _registry(policy)
    elif policy_case == "unverified":
        policy = SourcePolicy.unconfigured("qweather_official_warning", "test")
        registry = _registry(policy)
    elif policy_case == "not_registered":
        registry = SourceRegistry([], environment="test")
    elif policy_case == "old_endpoint":
        policy = _verified_policy(
            source_url_prefixes=(f"https://{API_HOST}/v7/warning/now",)
        )
        registry = _registry(policy)
    elif policy_case == "overbroad_endpoint":
        policy = _verified_policy(source_url_prefixes=(f"https://{API_HOST}/",))
        registry = _registry(policy)
    elif policy_case == "text_use_not_permitted":
        policy = _verified_policy(allowed_uses={"calculation", "derived_storage"})
        registry = _registry(policy)
    elif policy_case == "missing_attribution":
        policy = _verified_policy(attribution_required=False, attribution_text=None)
        registry = _registry(policy)
    elif policy_case == "attribution_not_required":
        policy = _verified_policy(attribution_required=False)
        registry = _registry(policy)
    elif policy_case == "wrong_policy_schema":
        policy = _verified_policy(
            unit_manifest="temperature:degC",
            required_metrics=("temperature",),
        )
        registry = _registry(policy)

    result, requests = await _fetch_body(
        _body([], zero_result=True),
        policy=policy,
        registry=registry,
    )

    assert result.status == "unavailable"
    assert result.reason == "source_policy_rejected"
    assert result.warnings == ()
    assert requests == []


@pytest.mark.asyncio
async def test_response_from_a_different_endpoint_is_rejected() -> None:
    class MismatchedEndpointClient:
        async def get(self, *args: object, **kwargs: object) -> httpx.Response:
            request = httpx.Request(
                "GET",
                "https://unexpected.example/weatheralert/v1/current/39.9/116.4",
            )
            return httpx.Response(200, json=_body(), request=request)

    policy = _verified_policy()
    result = await fetch_official_warnings(
        39.9,
        116.4,
        QWeatherWarningClientConfig(api_key=SECRET, api_host=API_HOST),
        source_registry=_registry(policy),
        source_policy=policy,
        http_client=MismatchedEndpointClient(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    assert result.status == "unavailable"
    assert result.reason == "endpoint_mismatch"
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_part",
    [
        "missing_metadata",
        "metadata_not_object",
        "missing_tag",
        "empty_tag",
        "missing_zero_result",
        "non_boolean_zero_result",
        "missing_attributions",
        "empty_attributions",
        "malformed_attributions",
        "missing_alerts",
        "alerts_not_array",
    ],
)
async def test_current_response_metadata_and_alert_array_are_required(
    invalid_part: str,
) -> None:
    body = _body()
    metadata = body["metadata"]
    assert isinstance(metadata, dict)
    if invalid_part == "missing_metadata":
        body.pop("metadata")
    elif invalid_part == "metadata_not_object":
        body["metadata"] = []
    elif invalid_part == "missing_tag":
        metadata.pop("tag")
    elif invalid_part == "empty_tag":
        metadata["tag"] = "  "
    elif invalid_part == "missing_zero_result":
        metadata.pop("zeroResult")
    elif invalid_part == "non_boolean_zero_result":
        metadata["zeroResult"] = "false"
    elif invalid_part == "missing_attributions":
        metadata.pop("attributions")
    elif invalid_part == "empty_attributions":
        metadata["attributions"] = []
    elif invalid_part == "malformed_attributions":
        metadata["attributions"] = ["QWeather", {"summary": "搜索摘要"}]
    elif invalid_part == "missing_alerts":
        body.pop("alerts")
    elif invalid_part == "alerts_not_array":
        body["alerts"] = {}

    result, _ = await _fetch_body(body)

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_response_attribution_must_satisfy_the_verified_policy() -> None:
    result, _ = await _fetch_body(
        _body(attributions=["Unverified weather mirror"]),
    )

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.attribution is None
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    [
        "id",
        "senderName",
        "issuedTime",
        "effectiveTime",
        "expireTime",
        "headline",
        "messageType",
    ],
)
async def test_alert_missing_required_official_or_lifecycle_metadata_is_rejected(
    missing_field: str,
) -> None:
    alert = _active_alert()
    alert.pop(missing_field)
    result, _ = await _fetch_body(_body([alert]))

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("senderName", "  "),
        ("headline", ""),
        ("id", ""),
        ("issuedTime", "not-a-time"),
        ("issuedTime", "2026-08-09 08:30"),
        ("effectiveTime", "2026-08-09 08:30"),
        ("expireTime", "2026-08-09 18:30"),
    ],
)
async def test_alert_text_and_times_must_be_nonempty_and_timezone_aware(
    field: str,
    bad_value: str,
) -> None:
    result, _ = await _fetch_body(_body([_active_alert(**{field: bad_value})]))

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_effective_time_must_precede_expire_time() -> None:
    result, _ = await _fetch_body(
        _body(
            [
                _active_alert(
                    effectiveTime="2026-08-09T20:00+08:00",
                    expireTime="2026-08-09T19:00+08:00",
                )
            ]
        )
    )

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"


@pytest.mark.asyncio
async def test_issued_time_must_precede_expire_time() -> None:
    result, _ = await _fetch_body(
        _body(
            [
                _active_alert(
                    issuedTime="2026-08-09T20:00+08:00",
                    expireTime="2026-08-09T19:00+08:00",
                )
            ]
        )
    )

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["Cancel", "cancel"])
async def test_cancelled_only_response_is_not_reported_as_no_active(
    message_type: str,
) -> None:
    result, _ = await _fetch_body(
        _body([_active_alert(messageType=_message_type(message_type, ["previous-alert"]))]),
    )

    assert result.status == "unavailable"
    assert result.reason == "no_verified_active_alert"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_expired_only_response_is_not_reported_as_no_active() -> None:
    result, _ = await _fetch_body(
        _body([_active_alert(expireTime="2026-08-09T08:59+08:00")]),
    )

    assert result.status == "unavailable"
    assert result.reason == "no_verified_active_alert"
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_type",
    [None, "", "Unknown", "Expired", {"code": "unknown", "supersedes": None}],
)
async def test_missing_or_unknown_message_type_is_rejected(
    message_type: object,
) -> None:
    alert = _active_alert(messageType=message_type)
    if message_type is None:
        alert.pop("messageType")
    result, _ = await _fetch_body(_body([alert]))

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_update_is_an_active_lifecycle_message() -> None:
    result, _ = await _fetch_body(
        _body(
            [
                _active_alert(
                    messageType=_message_type("update", ["previous-alert"])
                )
            ]
        )
    )

    assert result.status == "ok"
    assert result.reason == "active_warnings"
    assert result.warnings[0].message_type == "Update"


@pytest.mark.asyncio
async def test_inactive_alerts_are_filtered_when_an_active_alert_is_present() -> None:
    result, _ = await _fetch_body(
        _body(
            [
                _active_alert(
                    id="cancelled",
                    messageType=_message_type("cancel", ["previous-alert"]),
                ),
                _active_alert(id="expired", expireTime="2026-08-09T08:59+08:00"),
                _active_alert(id="active"),
            ]
        )
    )

    assert result.status == "ok"
    assert [alert.warning_id for alert in result.warnings] == ["active"]


@pytest.mark.asyncio
async def test_malformed_cancel_record_rejects_a_mixed_response() -> None:
    cancelled = _active_alert(
        id="cancelled",
        messageType=_message_type("cancel", ["previous-alert"]),
    )
    cancelled.pop("senderName")
    result, _ = await _fetch_body(
        _body([cancelled, _active_alert(id="active")]),
    )

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_only_zero_result_true_with_empty_alerts_means_no_active() -> None:
    result, _ = await _fetch_body(_body([], zero_result=True))

    assert result.status == "ok"
    assert result.reason == "no_active_warnings"
    assert result.zero_result is True
    assert result.source_tag == SOURCE_TAG
    assert result.attribution == "QWeather"
    assert result.retrieved_at == NOW
    assert result.source_url == SOURCE_URL
    assert result.content_sha256 is not None
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _body([], zero_result=False),
        _body([_active_alert()], zero_result=True),
    ],
)
async def test_inconsistent_zero_result_and_alerts_are_rejected(
    body: dict[str, object],
) -> None:
    result, _ = await _fetch_body(body)

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_one_malformed_active_alert_rejects_the_entire_fact_set() -> None:
    result, _ = await _fetch_body(
        _body([_active_alert(id="valid"), _active_alert(id="")]),
    )

    assert result.status == "unavailable"
    assert result.reason == "provider_response_rejected"
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_status", "timeout", "invalid_json"])
async def test_http_or_decode_failure_returns_no_alert_facts(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http_status":
            return httpx.Response(503, text=f"upstream unavailable {SECRET}")
        if failure == "timeout":
            raise httpx.ReadTimeout(f"upstream timeout {SECRET}", request=request)
        return httpx.Response(200, content=b"not-json")

    policy = _verified_policy()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_official_warnings(
            39.9,
            116.4,
            QWeatherWarningClientConfig(api_key=SECRET, api_host=API_HOST),
            source_registry=_registry(policy),
            source_policy=policy,
            http_client=client,
            clock=lambda: NOW,
        )

    assert result.status == "unavailable"
    assert result.reason == "provider_unavailable"
    assert result.warnings == ()
    assert SECRET not in result.model_dump_json()
