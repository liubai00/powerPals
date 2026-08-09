from datetime import datetime, timezone
import json

import httpx
import pytest

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.providers import CaiyunProvider, OpenMeteoProvider
from services.weather_bot.service import ForecastService
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


class CompleteExternalProvider:
    name = "test_weather"
    source_endpoints = ("https://weather.example.test/v1/forecast",)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=28.0 + hour / 10,
                )
                for hour in range(24)
            ],
            retrieved_at="2026-08-09T00:00:00+00:00",
            provider_issued_at="2026-08-08T23:00:00+00:00",
            source_url="https://weather.example.test/v1/forecast",
            content_sha256="f" * 64,
            retention_policy="derived_only",
        )


class IncompleteExternalProvider(CompleteExternalProvider):
    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        result = await super().fetch(request)
        result.points = result.points[:1]
        return result


class MissingRuntimeMetadataProvider(CompleteExternalProvider):
    def __init__(self, missing: str):
        self.missing = missing

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        result = await super().fetch(request)
        if self.missing == "valid_time":
            result.points = [point.model_copy(update={"time": ""}) for point in result.points]
        elif self.missing == "content_sha256":
            result.content_sha256 = None
        elif self.missing == "freshness":
            result.retrieved_at = "2026-08-08T22:00:00+00:00"
        return result


class RawPayloadProvider(CompleteExternalProvider):
    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        result = await super().fetch(request)
        result.raw = {"provider_payload": "must-not-survive-admission"}
        return result


class UnlicensedExternalProvider(CompleteExternalProvider):
    name = "unlicensed_weather"

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        result = await super().fetch(request)
        result.provider = self.name
        result.source_url = "https://unlicensed.example.test/v1/forecast"
        return result


async def test_production_without_an_explicit_source_policy_fails_closed():
    class MustNotFetchWithoutPolicy(CompleteExternalProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, request: ForecastRequest) -> ProviderForecast:
            self.calls += 1
            raise AssertionError("missing source policy must be zero provider HTTP")

    provider = MustNotFetchWithoutPolicy()
    service = ForecastService(
        providers={"test_weather": provider},
        settings=Settings(app_env="production", weather_source_policies_json="{}"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )
    assert provider.calls == 0


async def test_verified_policy_without_a_declared_provider_endpoint_is_zero_fetch():
    class UndeclaredEndpointProvider:
        name = "test_weather"

        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, request: ForecastRequest) -> ProviderForecast:
            self.calls += 1
            raise AssertionError("undeclared provider endpoint must be zero fetch")

    provider = UndeclaredEndpointProvider()
    service = ForecastService(
        providers={"test_weather": provider},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )

    assert provider.calls == 0


async def test_builtin_provider_declared_endpoint_must_match_policy_before_fetch() -> None:
    provider = OpenMeteoProvider()
    calls = 0

    async def forbidden_fetch(request: ForecastRequest) -> ProviderForecast:
        nonlocal calls
        calls += 1
        raise AssertionError("wrong endpoint policy must be zero provider HTTP")

    provider.fetch = forbidden_fetch  # type: ignore[method-assign]
    wrong_endpoint_policy = SourcePolicy(
        provider="open_meteo",
        environment="production",
        profile="wrong-open-meteo-endpoint-test",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="test-terms-2026-08-09",
        source_url_prefixes=("https://wrong.example.test/v1/forecast",),
        unit_manifest="temperature:degC",
        required_metrics=("temperature",),
        coverage_model="point",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
        retention_policy="derived_only",
    )
    service = ForecastService(
        providers={"open_meteo": provider},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry(
            [wrong_endpoint_policy],
            environment="production",
        ),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["open_meteo"],
            )
        )

    assert calls == 0


async def test_overbroad_caiyun_policy_is_zero_fetch_before_path_credential_use() -> None:
    provider = CaiyunProvider("cy-secret-must-not-be-used")
    calls = 0

    async def forbidden_fetch(request: ForecastRequest) -> ProviderForecast:
        nonlocal calls
        calls += 1
        raise AssertionError("overbroad Caiyun policy must be zero provider HTTP")

    provider.fetch = forbidden_fetch  # type: ignore[method-assign]
    overbroad_policy = SourcePolicy(
        provider="caiyun",
        environment="production",
        profile="overbroad-caiyun-endpoint-test",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="test-terms-2026-08-09",
        source_url_prefixes=("https://api.caiyunapp.com/v2.6/",),
        unit_manifest="temperature:degC",
        required_metrics=("temperature",),
        coverage_model="point",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
        retention_policy="derived_only",
    )
    service = ForecastService(
        providers={"caiyun": provider},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry(
            [overbroad_policy],
            environment="production",
        ),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["caiyun"],
            )
        )

    assert calls == 0


async def test_builtin_provider_rejects_response_from_unapproved_actual_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_date = "2026-08-10"
    times = [f"{target_date}T{hour:02d}:00" for hour in range(24)]
    body = {
        "hourly": {
            "time": times,
            "temperature_2m": [28.0] * 24,
            "precipitation_probability": [20.0] * 24,
            "wind_speed_10m": [3.0] * 24,
            "cloud_cover": [40.0] * 24,
            "apparent_temperature": [29.0] * 24,
            "wind_direction_10m": [90.0] * 24,
            "uv_index": [1.0] * 24,
            "shortwave_radiation": [100.0] * 24,
        },
        "daily": {"sunrise": [f"{target_date}T06:00"], "sunset": [f"{target_date}T19:00"]},
    }

    async def mismatched_response(*args: object, **kwargs: object):
        return httpx.Response(
            200,
            json=body,
            request=httpx.Request(
                "GET",
                "https://unexpected.example.test/v1/forecast",
            ),
        )

    monkeypatch.setattr(
        "services.weather_bot.providers._get_with_retry",
        mismatched_response,
    )
    policy = SourcePolicy(
        provider="open_meteo",
        environment="production",
        profile="verified-open-meteo-endpoint-test",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="test-terms-2026-08-09",
        source_url_prefixes=("https://api.open-meteo.com/v1/forecast",),
        unit_manifest="temperature:degC",
        required_metrics=("temperature",),
        coverage_model="point",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
        retention_policy="derived_only",
    )
    service = ForecastService(
        providers={"open_meteo": OpenMeteoProvider()},
        settings=Settings(app_env="production"),
        source_registry=SourceRegistry([policy], environment="production"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date=target_date,
                providers=["open_meteo"],
            )
        )


def verified_registry(*, environment: str = "production", **overrides) -> SourceRegistry:
    values = {
        "provider": "test_weather",
        "environment": environment,
        "profile": "contracted-api-test-profile",
        "license_status": "verified",
        "allowed_uses": {"calculation", "derived_storage"},
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": ("https://weather.example.test/v1/forecast",),
        "unit_manifest": "temperature:degC",
        "required_metrics": ("temperature",),
        "coverage_model": "point",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 3600,
        "min_completeness": 0.95,
        "retention_policy": "derived_only",
    }
    values.update(overrides)
    return SourceRegistry([SourcePolicy(**values)], environment=environment)


def test_verified_policy_requires_a_unit_for_every_required_metric():
    with pytest.raises(ValueError, match="unit_manifest missing metrics: wind_speed"):
        verified_registry(
            required_metrics=("temperature", "wind_speed"),
            unit_manifest="temperature:degC",
        )


def test_source_policy_cannot_authorize_raw_storage():
    with pytest.raises(ValueError, match="raw_storage is not supported"):
        verified_registry(
            allowed_uses={"calculation", "raw_storage"},
        )


async def test_explicit_verified_policy_allows_complete_provider_data_before_aggregation():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert result.aggregated_forecast.providers_used == ["test_weather"]
    assert result.provider_results[0].status == "ok"


async def test_admission_propagates_metadata_only_retention_from_policy():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(
            allowed_uses={"calculation"},
            retention_policy="metadata_only",
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert result.provider_results[0].retention_policy == "metadata_only"


async def test_calculation_only_policy_cannot_mark_forecast_as_derived_storable():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(
            allowed_uses={"calculation"},
            retention_policy="derived_only",
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert result.provider_results[0].retention_policy == "metadata_only"


async def test_policy_for_a_different_endpoint_cannot_authorize_provider_data():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(
            source_url_prefixes=("https://customer.weather.example.test/v1/",),
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )


async def test_environment_json_can_explicitly_configure_a_verified_source_profile():
    policy_json = json.dumps(
        [
            {
                "provider": "test_weather",
                "environment": "production",
                "profile": "contracted-api-test-profile",
                "license_status": "verified",
                "allowed_uses": ["calculation", "derived_storage"],
                "terms_version": "test-terms-2026-08-09",
                "source_url_prefixes": ["https://weather.example.test/v1/forecast"],
                "unit_manifest": "temperature:degC",
                "required_metrics": ["temperature"],
                "coverage_model": "point",
                "timezone": "Asia/Shanghai",
                "max_age_seconds": 3600,
                "min_completeness": 0.95,
                "retention_policy": "derived_only",
            }
        ]
    )
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(
            app_env="production",
            weather_source_policies_json=policy_json,
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert result.aggregated_forecast.providers_used == ["test_weather"]


@pytest.mark.parametrize(
    "missing_policy_field",
    [
        "terms_version",
        "unit_manifest",
        "required_metrics",
        "coverage_model",
        "timezone",
        "max_age_seconds",
    ],
)
async def test_incomplete_verified_policy_configuration_fails_closed(missing_policy_field):
    policy = {
        "provider": "test_weather",
        "environment": "production",
        "profile": "contracted-api-test-profile",
        "license_status": "verified",
        "allowed_uses": ["calculation", "derived_storage"],
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": ["https://weather.example.test/v1/forecast"],
        "unit_manifest": "temperature:degC",
        "required_metrics": ["temperature"],
        "coverage_model": "point",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 3600,
    }
    policy.pop(missing_policy_field)
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(
            app_env="production",
            weather_source_policies_json=json.dumps([policy]),
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )


async def test_runtime_completeness_below_policy_threshold_is_not_aggregated():
    service = ForecastService(
        providers={"test_weather": IncompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(min_completeness=0.95),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )


async def test_completeness_covers_each_metric_required_by_the_source_profile():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(
            required_metrics=("temperature", "wind_speed"),
            unit_manifest="temperature:degC;wind_speed:m/s",
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )


@pytest.mark.parametrize("missing_runtime_metadata", ["valid_time", "content_sha256", "freshness"])
async def test_missing_or_stale_runtime_provenance_is_not_aggregated(missing_runtime_metadata):
    service = ForecastService(
        providers={
            "test_weather": MissingRuntimeMetadataProvider(missing_runtime_metadata),
        },
        settings=Settings(app_env="production"),
        source_registry=verified_registry(),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )


async def test_admitted_provider_data_does_not_retain_raw_payloads():
    service = ForecastService(
        providers={"test_weather": RawPayloadProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert result.provider_results[0].raw is None
    assert "must-not-survive-admission" not in result.model_dump_json()


async def test_required_attribution_is_carried_into_the_submission_data_profile():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(
            attribution_required=True,
            attribution_text="Test Weather Data; terms test-terms-2026-08-09",
        ),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather"],
        )
    )

    assert result.data_profile.data_sources_summary == [
        "test_weather — Test Weather Data; terms test-terms-2026-08-09"
    ]


async def test_unlicensed_provider_is_removed_before_aggregation_when_an_allowed_source_remains():
    service = ForecastService(
        providers={
            "test_weather": CompleteExternalProvider(),
            "unlicensed_weather": UnlicensedExternalProvider(),
        },
        settings=Settings(app_env="production"),
        source_registry=verified_registry(),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["test_weather", "unlicensed_weather"],
        )
    )

    assert result.aggregated_forecast.providers_used == ["test_weather"]
    rejected = next(item for item in result.provider_results if item.provider == "unlicensed_weather")
    assert rejected.status == "error"
    assert rejected.points == []
    assert rejected.error_message == "Data availability rejected: license_unknown"


async def test_non_production_registry_cannot_be_injected_into_production_service():
    service = ForecastService(
        providers={"test_weather": CompleteExternalProvider()},
        settings=Settings(app_env="production"),
        source_registry=verified_registry(environment="development"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="No usable provider forecasts"):
        await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["test_weather"],
            )
        )
