from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

import httpx
import pytest

from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning import (
    OPEN_METEO_ARCHIVE_TRUTH_PROVIDER,
    OpenMeteoTruthClient,
)
from services.weather_bot import controlled_learning_cli
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


ARCHIVE_ENDPOINT = "https://archive-api.open-meteo.test/v1/archive"


def _policy(**overrides: object) -> SourcePolicy:
    values: dict[str, object] = {
        "provider": OPEN_METEO_ARCHIVE_TRUTH_PROVIDER,
        "environment": "test",
        "profile": "verified-open-meteo-archive-test",
        "license_status": "verified",
        "allowed_uses": {"calculation", "derived_storage"},
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": (ARCHIVE_ENDPOINT,),
        "unit_manifest": (
            "temperature_2m_max:degC;temperature_2m_min:degC;"
            "precipitation_sum:mm;wind_speed_10m_max:m/s"
        ),
        "required_metrics": (
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "wind_speed_10m_max",
        ),
        "coverage_model": "historical-grid-point-day",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 86400,
        "retention_policy": "derived_only",
        "retention_seconds": 86_400,
        "attribution_required": True,
        "attribution_text": "Open-Meteo",
    }
    values.update(overrides)
    return SourcePolicy(**values)


def _client(
    policy: SourcePolicy,
    *,
    registry: SourceRegistry | None = None,
    api_url: str = ARCHIVE_ENDPOINT,
) -> OpenMeteoTruthClient:
    return OpenMeteoTruthClient(
        api_url,
        source_registry=registry or SourceRegistry([policy], environment="test"),
        source_policy=policy,
        clock=lambda: datetime(2026, 8, 9, 1, 5, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_unconfigured_truth_source_is_disabled_before_any_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("unconfigured truth source must be zero HTTP")

    monkeypatch.setattr(
        "services.weather_bot.controlled_learning.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    client = OpenMeteoTruthClient(ARCHIVE_ENDPOINT)

    assert client.enabled is False
    with pytest.raises(RuntimeError, match="truth_source_policy_rejected"):
        await client.fetch(23.1, 113.2, "2026-08-01")


@pytest.mark.asyncio
async def test_admitted_truth_is_minimal_traceable_and_raw_is_not_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = json.dumps(
        {
            "daily": {
                "time": ["2026-08-01"],
                "temperature_2m_max": [31.0],
                "temperature_2m_min": [22.0],
                "precipitation_sum": [1.2],
                "wind_speed_10m_max": [6.0],
                "rawHourlySeries": ["must not escape"],
            },
            "rawProviderPayload": "must not escape",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=raw_body)

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        "services.weather_bot.controlled_learning.httpx.AsyncClient",
        fake_client,
    )

    truth = await _client(_policy()).fetch(23.1, 113.2, "2026-08-01")

    assert len(requests) == 1
    assert truth.source == "open_meteo_historical_weather_grid"
    assert truth.source_provider == OPEN_METEO_ARCHIVE_TRUTH_PROVIDER
    assert truth.source_url == str(requests[0].url)
    assert truth.retrieved_at == "2026-08-09T01:05:00+00:00"
    assert truth.content_sha256 == sha256(raw_body).hexdigest()
    assert truth.attribution == "Open-Meteo"
    assert truth.retention_policy == "derived_only"
    serialized = truth.model_dump_json()
    assert "rawHourlySeries" not in serialized
    assert "rawProviderPayload" not in serialized


@pytest.mark.asyncio
async def test_controlled_learning_cycle_wires_explicit_truth_source_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    observed_enabled: list[bool] = []

    async def capture_truth_client(store, truth_client, **kwargs):
        observed_enabled.append(truth_client.enabled)
        return {"due": 0, "evaluated": 0, "deferred": 0, "skipped": 0}

    monkeypatch.setattr(
        controlled_learning_cli,
        "verify_due_snapshots",
        capture_truth_client,
    )
    policy = _policy()
    settings = Settings(
        _env_file=None,
        app_env="test",
        controlled_learning_db=str(tmp_path / "learning.db"),
        controlled_learning_report_dir=str(tmp_path / "reports"),
        controlled_learning_archive_api_url=ARCHIVE_ENDPOINT,
        weather_source_policies_json=json.dumps(
            [policy.model_dump(mode="json")],
            ensure_ascii=False,
        ),
    )

    await controlled_learning_cli.run_cycle(
        settings,
        skip_truth=False,
        truth_limit=1,
    )

    assert observed_enabled == [True]


@pytest.mark.asyncio
async def test_cycle_report_does_not_claim_truth_source_when_policy_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    observed_enabled: list[bool] = []

    async def capture_truth_client(store, truth_client, **kwargs):
        observed_enabled.append(truth_client.enabled)
        return {"due": 0, "evaluated": 0, "deferred": 0, "skipped": 0}

    monkeypatch.setattr(
        controlled_learning_cli,
        "verify_due_snapshots",
        capture_truth_client,
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        controlled_learning_db=str(tmp_path / "learning.db"),
        controlled_learning_report_dir=str(tmp_path / "reports"),
        controlled_learning_archive_api_url=ARCHIVE_ENDPOINT,
        weather_source_policies_json="[]",
    )

    report = await controlled_learning_cli.run_cycle(
        settings,
        skip_truth=False,
        truth_limit=1,
    )

    assert observed_enabled == [False]
    assert report["reference_data"]["availability"] == "policy_rejected"
    assert report["reference_data"]["used_for_scoring"] is False
    assert report["reference_data"]["source"] == "未启用"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "empty_registry",
        "unknown_license",
        "wrong_endpoint",
        "overbroad_endpoint",
        "wrong_environment",
        "calculation_not_allowed",
        "attribution_missing",
        "malformed_endpoint",
    ],
)
async def test_invalid_truth_source_policy_fails_closed_with_zero_http(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    policy = _policy()
    registry = SourceRegistry([policy], environment="test")
    api_url = ARCHIVE_ENDPOINT
    if case == "empty_registry":
        registry = SourceRegistry([], environment="test")
    elif case == "unknown_license":
        policy = SourcePolicy.unconfigured(OPEN_METEO_ARCHIVE_TRUTH_PROVIDER, "test")
        registry = SourceRegistry([policy], environment="test")
    elif case == "wrong_endpoint":
        policy = _policy(
            source_url_prefixes=("https://wrong.example.test/v1/archive",),
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "overbroad_endpoint":
        policy = _policy(
            source_url_prefixes=("https://archive-api.open-meteo.test/v1/",),
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "wrong_environment":
        registry = SourceRegistry([], environment="production")
    elif case == "calculation_not_allowed":
        policy = _policy(allowed_uses={"text_reference", "derived_storage"})
        registry = SourceRegistry([policy], environment="test")
    elif case == "attribution_missing":
        policy = _policy(attribution_required=False, attribution_text=None)
        registry = SourceRegistry([policy], environment="test")
    elif case == "malformed_endpoint":
        api_url = "https://reader@archive-api.open-meteo.test/v1/archive"

    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("rejected truth source must be zero HTTP")

    monkeypatch.setattr(
        "services.weather_bot.controlled_learning.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    client = _client(policy, registry=registry, api_url=api_url)

    assert client.enabled is False
    with pytest.raises(RuntimeError, match="truth_source_policy_rejected"):
        await client.fetch(23.1, 113.2, "2026-08-01")
