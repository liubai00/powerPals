from __future__ import annotations

from datetime import datetime, timezone
import logging

import httpx
import pytest

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastRequest
from services.weather_bot.providers import CaiyunProvider
from services.weather_bot.service import ForecastService
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


SANITIZED_ENDPOINT = (
    "https://api.caiyunapp.com/v2.6/{credential}/{location}/hourly"
)
SECRET = "cy-unit-test-secret-never-log"


def _policy() -> SourcePolicy:
    return SourcePolicy(
        provider="caiyun",
        environment="test",
        profile="verified-caiyun-endpoint-test",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="test-terms-2026-08-09",
        source_url_prefixes=(SANITIZED_ENDPOINT,),
        unit_manifest=(
            "temperature:degC;precipitation_probability:percent;"
            "wind_speed:m/s;cloud_cover:percent"
        ),
        required_metrics=(
            "temperature",
            "precipitation_probability",
            "wind_speed",
            "cloud_cover",
        ),
        coverage_model="point",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
        retention_policy="derived_only",
    )


def _body(target_date: str) -> dict[str, object]:
    timestamps = [f"{target_date}T{hour:02d}:00+08:00" for hour in range(24)]
    return {
        "result": {
            "hourly": {
                "temperature": [
                    {"datetime": timestamp, "value": 28.0}
                    for timestamp in timestamps
                ],
                "precipitation": [
                    {"datetime": timestamp, "probability": 0.2}
                    for timestamp in timestamps
                ],
                "wind": [
                    {"datetime": timestamp, "speed": 3.0}
                    for timestamp in timestamps
                ],
                "cloudrate": [
                    {"datetime": timestamp, "value": 0.4}
                    for timestamp in timestamps
                ],
            }
        }
    }


@pytest.mark.asyncio
async def test_authorized_caiyun_fetch_never_logs_or_returns_path_credential(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_body("2026-08-10"))

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.providers.httpx.AsyncClient", fake_client)
    service = ForecastService(
        providers={"caiyun": CaiyunProvider(SECRET)},
        settings=Settings(_env_file=None, app_env="test"),
        source_registry=SourceRegistry([_policy()], environment="test"),
        clock=lambda: datetime(2026, 8, 9, 0, 5, tzinfo=timezone.utc),
    )

    with caplog.at_level(logging.INFO, logger="httpx"):
        submission = await service.forecast(
            ForecastRequest(
                region="深圳",
                target_date="2026-08-10",
                providers=["caiyun"],
            )
        )

    assert len(requests) == 1
    assert submission.provider_results[0].source_url == SANITIZED_ENDPOINT
    assert SECRET not in submission.model_dump_json()
    assert SECRET not in caplog.text
    assert "[REDACTED_CREDENTIAL]" in caplog.text
