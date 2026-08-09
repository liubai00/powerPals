from __future__ import annotations

import pytest

from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


def _registry(prefix: str = "https://approved.example.com/v1") -> SourceRegistry:
    policy = SourcePolicy(
        provider="approved_weather",
        environment="test",
        profile="verified-url-boundary-test",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="test-terms-2026-08-09",
        source_url_prefixes=(prefix,),
        unit_manifest="temperature:degC",
        required_metrics=("temperature",),
        coverage_model="point",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
    )
    return SourceRegistry([policy], environment="test")


def test_similar_hostname_is_not_an_approved_source() -> None:
    registry = _registry("https://approved.example.com")

    resolved = registry.resolve(
        "approved_weather",
        "https://approved.example.com.evil/v1/forecast",
    )

    assert resolved.license_status == "unknown"
    assert resolved.profile == "unconfigured"


@pytest.mark.parametrize(
    ("prefix", "source_url"),
    [
        (
            "https://approved.example.com",
            "https://reader@approved.example.com/v1/forecast",
        ),
        (
            "https://approved.example.com",
            "https://approved.example.com:444/v1/forecast",
        ),
        (
            "https://approved.example.com/v1",
            "https://approved.example.com/v10/forecast",
        ),
        (
            "https://approved.example.com/v1/",
            "https://approved.example.com/v1%2f..%2fadmin",
        ),
        (
            "https://approved.example.com/v1",
            "https://approved.example.com/v1/%252e%252e/admin",
        ),
        (
            "https://approved.example.com/v1",
            "https://approved.example.com/v1%252fadmin",
        ),
        (
            "https://approved.example.com/v1",
            "https://approved.example.com/v1/%ZZ/admin",
        ),
    ],
)
def test_credentials_wrong_ports_and_path_confusion_are_not_approved(
    prefix: str,
    source_url: str,
) -> None:
    resolved = _registry(prefix).resolve("approved_weather", source_url)

    assert resolved.license_status == "unknown"


def test_scheme_host_default_port_and_trailing_slash_are_normalized() -> None:
    registry = _registry("HTTPS://APPROVED.EXAMPLE.COM:443/v1/")

    resolved = registry.resolve(
        "approved_weather",
        "https://approved.example.com/v1/forecast?run=latest",
    )

    assert resolved.license_status == "verified"


@pytest.mark.parametrize(
    "source_url",
    [
        "https://[::1/v1/forecast",
        "https://approved.example.com:not-a-port/v1/forecast",
        " https://approved.example.com/v1/forecast",
        "https://approved.example.com/v1/forecast#alternate",
    ],
)
def test_malformed_or_ambiguous_urls_fail_closed_without_raising(source_url: str) -> None:
    resolved = _registry().resolve("approved_weather", source_url)

    assert resolved.license_status == "unknown"
