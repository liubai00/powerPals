from __future__ import annotations

import json

from scripts import daily_power_briefing
from services.weather_bot import main as weather_main
from services.weather_bot.config import Settings
from services.weather_bot.source_registry import SourcePolicy
from services.weather_bot.typhoon import QWEATHER_TYPHOON_PROVIDER


def _typhoon_policy_json(environment: str = "test") -> str:
    metrics = (
        "storm_id",
        "storm_name",
        "is_active",
        "observation_time",
        "latitude",
        "longitude",
        "wind_speed",
        "forecast_time",
    )
    policy = SourcePolicy(
        provider=QWEATHER_TYPHOON_PROVIDER,
        environment=environment,
        profile="runtime-wiring-test",
        license_status="verified",
        allowed_uses={"text_reference"},
        terms_version="test-terms-2026-08-09",
        source_url_prefixes=(
            "https://weather.example.test/v7/tropical/storm-list",
            "https://weather.example.test/v7/tropical/storm-track",
            "https://weather.example.test/v7/tropical/storm-forecast",
        ),
        unit_manifest=";".join(f"{metric}:text" for metric in metrics),
        required_metrics=metrics,
        coverage_model="western-north-pacific",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
        retention_policy="metadata_only",
        retention_seconds=86_400,
        attribution_required=True,
        attribution_text="QWeather test attribution",
    )
    return json.dumps([policy.model_dump(mode="json")], ensure_ascii=False)


def test_create_app_wires_one_source_registry_into_location_and_typhoon_adapters(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingLocationResolver:
        def __init__(self, settings, *, source_registry=None, **kwargs):
            captured["location_registry"] = source_registry

    class CapturingTyphoonClient:
        def __init__(
            self,
            api_key,
            api_host=None,
            *,
            source_registry=None,
            source_policy=None,
            **kwargs,
        ):
            captured["typhoon_registry"] = source_registry
            captured["typhoon_policy"] = source_policy

    monkeypatch.setattr(weather_main, "LocationResolver", CapturingLocationResolver)
    monkeypatch.setattr(weather_main, "TyphoonClient", CapturingTyphoonClient)
    settings = Settings(
        _env_file=None,
        app_env="test",
        qweather_api_key="test-key",
        qweather_api_host="weather.example.test",
        weather_source_policies_json=_typhoon_policy_json(),
    )

    weather_main.create_app(forecast_service=object(), settings=settings)

    assert captured["location_registry"] is captured["typhoon_registry"]
    assert captured["typhoon_policy"].provider == QWEATHER_TYPHOON_PROVIDER
    assert captured["typhoon_policy"].license_status == "verified"


async def test_scheduled_briefing_wires_source_registry_into_typhoon_adapter(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    settings = Settings(
        _env_file=None,
        app_env="test",
        qweather_api_key="test-key",
        qweather_api_host="weather.example.test",
        weather_source_policies_json=_typhoon_policy_json(),
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
    )

    class CapturingTyphoonClient:
        def __init__(
            self,
            api_key,
            api_host=None,
            *,
            source_registry=None,
            source_policy=None,
            **kwargs,
        ):
            captured["registry"] = source_registry
            captured["policy"] = source_policy

    async def fake_snapshot(service, typhoon_client, start_date, **kwargs):
        captured["client"] = typhoon_client
        return (
            {
                "coverage": {
                    "provincial_areas": {"covered": 0, "total": 31},
                    "markets": {"covered": 0, "total": 33},
                    "points": {"covered": 0, "total": 75},
                },
                "statistics": {"classified_markets": 0, "configured_markets": 33},
                "summary_card": {
                    "msg_type": "interactive",
                    "card": {
                        "header": {"title": {"content": "来源门禁接线测试"}},
                        "elements": [],
                    },
                },
            },
            False,
        )

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(weather_main, "TyphoonClient", CapturingTyphoonClient)
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fake_snapshot)

    await daily_power_briefing.go("precompute")

    assert captured["registry"].environment == "test"
    assert captured["policy"].provider == QWEATHER_TYPHOON_PROVIDER
    assert captured["policy"].license_status == "verified"
    assert isinstance(captured["client"], CapturingTyphoonClient)
