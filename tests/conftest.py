from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


TEST_SOURCE_NOW = datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def explicit_unsigned_feishu_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy event fixtures explicit without weakening runtime defaults.

    Individual authentication tests remove or override these variables to prove
    the fail-closed production contract at the public HTTP seam.
    """

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("FEISHU_ALLOW_UNSIGNED_EVENTS", "true")


@pytest.fixture
def external_source_metadata() -> Callable[..., dict[str, str]]:
    """Build explicit provenance for deterministic external-provider fakes."""

    def build(
        provider: str,
        *,
        retrieved_at: str = "2026-08-09T00:00:00+00:00",
        source_url: str | None = None,
    ) -> dict[str, str]:
        resolved_url = source_url or f"https://{provider}.weather.test/v1/forecast"
        return {
            "source_url": resolved_url,
            "content_sha256": sha256(
                f"{provider}|{resolved_url}|{retrieved_at}".encode("utf-8")
            ).hexdigest(),
            "retrieved_at": retrieved_at,
        }

    return build


@pytest.fixture
def verified_test_source_registry() -> Callable[..., SourceRegistry]:
    """Create an environment-scoped registry used only by explicit test services."""

    def build(
        provider_url_prefixes: dict[str, str],
        *,
        required_metrics: Iterable[str] = (
            "temperature",
            "precipitation_probability",
            "wind_speed",
            "cloud_cover",
        ),
    ) -> SourceRegistry:
        metrics = tuple(required_metrics)
        units = {
            "temperature": "degC",
            "precipitation_probability": "percent",
            "wind_speed": "m/s",
            "cloud_cover": "percent",
        }
        unit_manifest = ";".join(f"{metric}:{units[metric]}" for metric in metrics)
        policies = [
            SourcePolicy(
                provider=provider,
                environment="test",
                profile="verified-external-test-fixture",
                license_status="verified",
                allowed_uses={"calculation", "derived_storage"},
                terms_version="test-fixture-2026-08-09",
                source_url_prefixes=(url_prefix,),
                unit_manifest=unit_manifest,
                required_metrics=metrics,
                coverage_model="point",
                timezone="Asia/Shanghai",
                max_age_seconds=3600,
                min_completeness=0.95,
                retention_policy="derived_only",
            )
            for provider, url_prefix in provider_url_prefixes.items()
        ]
        return SourceRegistry(policies, environment="test")

    return build


@pytest.fixture
def test_source_clock() -> Callable[[], datetime]:
    return lambda: TEST_SOURCE_NOW
