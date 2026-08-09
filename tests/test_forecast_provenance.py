from datetime import datetime, timezone

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.providers import QWeatherProvider
from services.weather_bot.service import ForecastService


class MetadataProvider:
    def __init__(
        self,
        name: str,
        provider_issued_at: str | None,
        source_metadata: dict[str, str],
    ):
        self.name = name
        self.provider_issued_at = provider_issued_at
        self.source_metadata = source_metadata
        self.source_endpoints = (source_metadata["source_url"],)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            status="ok",
            provider_issued_at=self.provider_issued_at,
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=30.0,
                    wind_speed=3.0,
                    cloud_cover=70.0,
                )
                for hour in range(24)
            ],
            **self.source_metadata,
        )


def test_provider_raw_payload_is_never_serialized_for_local_storage():
    result = ProviderForecast(
        provider="custom",
        raw={"api_key": "must-not-persist", "payload": [1, 2, 3]},
    )

    assert result.raw is not None
    assert "raw" not in result.model_dump(mode="json")
    assert "must-not-persist" not in result.model_dump_json()


async def test_forecast_exposes_real_run_timing_separately_from_business_deadline(
    external_source_metadata,
    verified_test_source_registry,
):
    observed_at = datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc)
    service = ForecastService(
        providers={
            "open_meteo": MetadataProvider(
                "open_meteo",
                "2026-08-08T12:00:00+00:00",
                external_source_metadata(
                    "open_meteo", retrieved_at=observed_at.isoformat()
                ),
            ),
            "qweather": MetadataProvider(
                "qweather",
                None,
                external_source_metadata("qweather", retrieved_at=observed_at.isoformat()),
            ),
        },
        settings=Settings(_env_file=None, app_env="test"),
        clock=lambda: observed_at,
        run_id_factory=lambda: "forecast-run-20260809-0005",
        source_registry=verified_test_source_registry(
            {
                "open_meteo": "https://open_meteo.weather.test/v1/forecast",
                "qweather": "https://qweather.weather.test/v1/forecast",
            }
        ),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["open_meteo", "qweather"],
        )
    )

    assert result.time_info.forecast_run_id == "forecast-run-20260809-0005"
    assert result.time_info.retrieved_at == "2026-08-09T00:05:00+00:00"
    assert result.time_info.aggregation_completed_at == "2026-08-09T00:05:00+00:00"
    assert result.time_info.provider_issued_at == {
        "open_meteo": "2026-08-08T12:00:00+00:00",
        "qweather": None,
    }
    assert result.time_info.valid_time.start == "2026-08-10T00:00:00+08:00"
    assert result.time_info.valid_time.end == "2026-08-10T23:00:00+08:00"
    assert result.time_info.valid_time.timezone == "Asia/Shanghai"
    assert result.time_info.business_submission_deadline == "2026-08-09T16:00:00+08:00"
    assert result.data_cutoff_time == result.time_info.business_submission_deadline
    assert result.time_info.data_cutoff_time == result.time_info.business_submission_deadline
    assert result.time_info.retrieved_at != result.time_info.business_submission_deadline


async def test_forecast_preserves_provider_issue_time_reported_by_external_api(
    monkeypatch,
    verified_test_source_registry,
):
    observed_at = datetime(2026, 8, 9, 0, 6, tzinfo=timezone.utc)

    response_body = {
                "updateTime": "2026-08-09T07:45+08:00",
                "hourly": [
                    {
                        "fxTime": f"2026-08-10T{hour:02d}:00+08:00",
                        "temp": "29",
                        "pop": "20",
                        "windSpeed": "10.8",
                        "cloud": "40",
                    }
                    for hour in range(24)
                ],
            }

    class StubAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, *args, **kwargs):
            import httpx

            return httpx.Response(
                200,
                json=response_body,
                request=httpx.Request("GET", args[0], params=kwargs.get("params")),
            )

    monkeypatch.setattr("services.weather_bot.providers.httpx.AsyncClient", StubAsyncClient)
    provider = QWeatherProvider("test-api-key", clock=lambda: observed_at)
    service = ForecastService(
        providers={"qweather": provider},
        settings=Settings(_env_file=None, app_env="test"),
        clock=lambda: observed_at,
        run_id_factory=lambda: "forecast-run-qweather",
        source_registry=verified_test_source_registry(
            {"qweather": "https://devapi.qweather.com/v7/weather/168h"}
        ),
    )

    result = await service.forecast(
        ForecastRequest(region="深圳", target_date="2026-08-10", providers=["qweather"])
    )

    assert result.provider_results[0].retrieved_at == "2026-08-09T00:06:00+00:00"
    assert result.provider_results[0].provider_issued_at == "2026-08-09T07:45+08:00"
    assert result.provider_results[0].raw is None
    assert result.provider_results[0].source_url == "https://devapi.qweather.com/v7/weather/168h"
    assert len(result.provider_results[0].content_sha256 or "") == 64
    assert result.provider_results[0].retention_policy == "derived_only"
    assert "test-api-key" not in result.provider_results[0].source_url
    assert result.time_info.provider_issued_at == {"qweather": "2026-08-09T07:45+08:00"}


async def test_forecast_does_not_claim_retrieval_or_issue_time_for_disabled_provider(
    external_source_metadata,
    verified_test_source_registry,
):
    observed_at = datetime(2026, 8, 9, 0, 7, tzinfo=timezone.utc)

    class DisabledProvider:
        name = "qweather"

        async def fetch(self, request: ForecastRequest) -> ProviderForecast:
            return ProviderForecast(
                provider=self.name,
                status="disabled",
                points=[],
                error_message="Missing API key",
            )

    service = ForecastService(
        providers={
            "open_meteo": MetadataProvider(
                "open_meteo",
                None,
                external_source_metadata(
                    "open_meteo", retrieved_at=observed_at.isoformat()
                ),
            ),
            "qweather": DisabledProvider(),
        },
        settings=Settings(_env_file=None, app_env="test"),
        clock=lambda: observed_at,
        run_id_factory=lambda: "forecast-run-disabled-provider",
        source_registry=verified_test_source_registry(
            {"open_meteo": "https://open_meteo.weather.test/v1/forecast"}
        ),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["open_meteo", "qweather"],
        )
    )

    disabled = next(item for item in result.provider_results if item.provider == "qweather")
    assert disabled.retrieved_at is None
    assert disabled.provider_issued_at is None
    assert result.time_info.provider_issued_at["qweather"] is None


async def test_forecast_provider_error_does_not_expose_exception_details(
    external_source_metadata,
    verified_test_source_registry,
):
    observed_at = datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc)

    class SecretFailingProvider:
        name = "caiyun"
        source_endpoints = (
            "https://api.caiyunapp.com/v2.6/{credential}/{location}/hourly",
        )

        async def fetch(self, request: ForecastRequest) -> ProviderForecast:
            raise RuntimeError(
                "https://api.caiyunapp.com/v2.6/secret-api-key/location/hourly"
            )

    service = ForecastService(
        providers={
            "open_meteo": MetadataProvider(
                "open_meteo",
                None,
                external_source_metadata(
                    "open_meteo", retrieved_at=observed_at.isoformat()
                ),
            ),
            "caiyun": SecretFailingProvider(),
        },
        settings=Settings(_env_file=None, app_env="test"),
        clock=lambda: observed_at,
        run_id_factory=lambda: "forecast-run-provider-error",
            source_registry=verified_test_source_registry(
            {
                "open_meteo": "https://open_meteo.weather.test/v1/forecast",
                "caiyun": (
                    "https://api.caiyunapp.com/v2.6/"
                    "{credential}/{location}/hourly"
                ),
            }
        ),
    )

    result = await service.forecast(
        ForecastRequest(
            region="深圳",
            target_date="2026-08-10",
            providers=["open_meteo", "caiyun"],
        )
    )

    provider_result = next(
        item for item in result.provider_results if item.provider == "caiyun"
    )
    assert provider_result.status == "error"
    assert provider_result.error_message == "RuntimeError: provider request failed"
    assert "secret-api-key" not in result.model_dump_json()
