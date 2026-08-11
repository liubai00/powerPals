from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

import httpx
import pytest

from services.weather_bot.source_registry import SourcePolicy, SourceRegistry
from services.weather_bot import power_briefing
from services.weather_bot.typhoon import (
    QWEATHER_TYPHOON_PROVIDER,
    TyphoonClient,
    TyphoonDataUnavailable,
    format_active_for_briefing,
)


API_HOST = "tropical-api.qweather.test"


def _verified_policy(**overrides: object) -> SourcePolicy:
    root = f"https://{API_HOST}/v7/tropical"
    values: dict[str, object] = {
        "provider": QWEATHER_TYPHOON_PROVIDER,
        "environment": "test",
        "profile": "verified-qweather-tropical-test",
        "license_status": "verified",
        "allowed_uses": {"text_reference", "derived_storage"},
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": (
            f"{root}/storm-list",
            f"{root}/storm-track",
            f"{root}/storm-forecast",
        ),
        "unit_manifest": (
            "storm_id:text;storm_name:text;is_active:boolean;"
            "observation_time:iso8601;latitude:degree;longitude:degree;"
            "wind_speed:m/s;forecast_time:iso8601"
        ),
        "required_metrics": (
            "storm_id",
            "storm_name",
            "is_active",
            "observation_time",
            "latitude",
            "longitude",
            "wind_speed",
            "forecast_time",
        ),
        "coverage_model": "tropical-cyclone-track",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 900,
        "retention_policy": "metadata_only",
        "retention_seconds": 86_400,
        "attribution_required": True,
        "attribution_text": "QWeather",
    }
    values.update(overrides)
    return SourcePolicy(**values)


def _client(
    policy: SourcePolicy,
    *,
    registry: SourceRegistry | None = None,
    api_host: str = API_HOST,
) -> TyphoonClient:
    return TyphoonClient(
        "secret",
        api_host,
        source_registry=registry or SourceRegistry([policy], environment="test"),
        source_policy=policy,
        clock=lambda: datetime(2026, 8, 9, 1, 5, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_unconfigured_typhoon_source_is_disabled_before_any_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("unconfigured source must not construct an HTTP client")

    monkeypatch.setattr(
        "services.weather_bot.typhoon.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    client = TyphoonClient("secret", "approved.example.com")

    assert client.enabled is False
    assert await client.brief_for_text("台风实时路径") is None
    assert await client.active_storms() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown_license",
        "wrong_endpoint",
        "overbroad_endpoint",
        "not_registered",
        "wrong_environment",
        "text_reference_not_allowed",
        "attribution_missing",
        "malformed_host",
    ],
)
async def test_invalid_typhoon_source_policy_fails_closed_with_zero_http(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    policy = _verified_policy()
    registry = SourceRegistry([policy], environment="test")
    api_host = API_HOST
    if case == "unknown_license":
        policy = SourcePolicy.unconfigured(QWEATHER_TYPHOON_PROVIDER, "test")
        registry = SourceRegistry([policy], environment="test")
    elif case == "wrong_endpoint":
        policy = _verified_policy(
            source_url_prefixes=("https://wrong.example.test/v7/tropical/storm-list",),
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "overbroad_endpoint":
        policy = _verified_policy(
            source_url_prefixes=(f"https://{API_HOST}/v7/tropical/",),
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "not_registered":
        registry = SourceRegistry([], environment="test")
    elif case == "wrong_environment":
        registry = SourceRegistry([], environment="production")
    elif case == "text_reference_not_allowed":
        policy = _verified_policy(allowed_uses={"calculation", "derived_storage"})
        registry = SourceRegistry([policy], environment="test")
    elif case == "attribution_missing":
        policy = _verified_policy(
            attribution_required=False,
            attribution_text=None,
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "malformed_host":
        api_host = f"reader@{API_HOST}"

    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("rejected source must not construct an HTTP client")

    monkeypatch.setattr(
        "services.weather_bot.typhoon.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    client = _client(policy, registry=registry, api_host=api_host)

    assert client.enabled is False
    assert await client.brief_for_text("台风实时路径") is None
    assert await client.active_storms() == []


@pytest.mark.asyncio
async def test_admitted_typhoon_list_exposes_only_minimal_traceable_fields() -> None:
    raw_body = json.dumps(
        {
            "code": "200",
            "updateTime": "2026-08-09T09:00+08:00",
            "storm": [
                {
                    "id": "NP202601",
                    "name": "测试台风",
                    "basin": "NP",
                    "year": "2026",
                    "isActive": "1",
                    "description": "raw provider narrative must not escape",
                    "internalDebug": "raw provider payload must not escape",
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=raw_body,
            headers={"content-type": "application/json"},
        )

    policy = _verified_policy()
    typhoon = _client(policy)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        storms = await typhoon.list_storms(http_client, "NP", "2026")

    assert len(requests) == 1
    assert storms == [
        {
            "id": "NP202601",
            "name": "测试台风",
            "basin": "NP",
            "year": "2026",
            "isActive": "1",
            "_provenance": {
                "provider": QWEATHER_TYPHOON_PROVIDER,
                "source_url": str(requests[0].url),
                "retrieved_at": storms[0]["_provenance"]["retrieved_at"],
                "provider_issued_at": "2026-08-09T09:00:00+08:00",
                "content_sha256": sha256(raw_body).hexdigest(),
                "attribution": "QWeather",
                "retention_policy": "metadata_only",
            },
        }
    ]
    assert storms[0]["_provenance"]["retrieved_at"].endswith("+00:00")
    assert "description" not in storms[0]
    assert "internalDebug" not in storms[0]


@pytest.mark.asyncio
async def test_missing_runtime_provenance_returns_unavailable_without_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "storm": [
                        {
                            "id": "NP202601",
                            "name": "测试台风",
                            "year": "2026",
                            "isActive": "1",
                        }
                    ],
                },
            )

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.typhoon.httpx.AsyncClient", fake_client)
    policy = _verified_policy()

    result = await _client(policy).brief_for_text("测试台风路径", years=["2026"])

    assert result is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_typhoon_brief_is_traceable_minimal_and_does_not_claim_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            common = {"code": "200", "updateTime": "2026-08-09T09:00+08:00"}
            if request.url.path.endswith("storm-list"):
                body = {
                    **common,
                    "storm": [
                        {
                            "id": "NP202601",
                            "name": "测试台风",
                            "basin": "NP",
                            "year": "2026",
                            "isActive": "1",
                            "rawNarrative": "must not escape",
                        }
                    ],
                }
            elif request.url.path.endswith("storm-track"):
                body = {
                    **common,
                    "now": {
                        "pubTime": "2026-08-09T09:00+08:00",
                        "type": "TY",
                        "lat": "20.0",
                        "lon": "120.0",
                        "windSpeed": "35",
                        "rawTrack": "must not escape",
                    },
                }
            else:
                body = {
                    **common,
                    "forecast": [
                        {
                            "fxTime": "2026-08-10T09:00+08:00",
                            "type": "TY",
                            "lat": "21.0",
                            "lon": "119.0",
                            "windSpeed": "33",
                            "rawForecast": "must not escape",
                        }
                    ],
                }
            return httpx.Response(200, json=body)

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.typhoon.httpx.AsyncClient", fake_client)
    policy = _verified_policy()

    brief = await _client(policy).brief_for_text("测试台风路径", years=["2026"])

    assert brief is not None
    assert len(requests) == 3
    assert "测试台风" in brief
    assert "来源：QWeather" in brief
    assert "抓取：2026-08-09T01:05:00+00:00" in brief
    assert "仅为气象侧证据" in brief
    assert "权威事实" not in brief
    assert "rawNarrative" not in brief
    assert "rawTrack" not in brief
    assert "rawForecast" not in brief


@pytest.mark.asyncio
async def test_active_typhoon_batch_does_not_return_partial_facts_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            common = {"code": "200", "updateTime": "2026-08-09T09:00+08:00"}
            if request.url.path.endswith("storm-list"):
                return httpx.Response(
                    200,
                    json={
                        **common,
                        "storm": [
                            {
                                "id": "NP202601",
                                "name": "台风甲",
                                "year": "2026",
                                "isActive": "1",
                            },
                            {
                                "id": "NP202602",
                                "name": "台风乙",
                                "year": "2026",
                                "isActive": "1",
                            },
                        ],
                    },
                )
            if request.url.params.get("stormid") == "NP202602":
                return httpx.Response(503, json={"code": "503"})
            return httpx.Response(
                200,
                json={
                    **common,
                    "now": {
                        "pubTime": "2026-08-09T09:00+08:00",
                        "type": "TY",
                        "lat": "20.0",
                        "lon": "120.0",
                        "windSpeed": "35",
                    },
                },
            )

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.typhoon.httpx.AsyncClient", fake_client)

    with pytest.raises(TyphoonDataUnavailable, match="provider_unavailable"):
        await _client(_verified_policy()).active_storms(year="2026")


@pytest.mark.asyncio
async def test_briefing_marks_unlicensed_typhoon_data_unavailable_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(*args: object, **kwargs: object) -> dict[str, object]:
        return {"unused": True}

    monkeypatch.setattr(power_briefing, "MARKET_POINTS", ((object(), object()),))
    monkeypatch.setattr(power_briefing, "_fetch", fake_fetch)
    monkeypatch.setattr(
        power_briefing,
        "briefing_coverage",
        lambda *args, **kwargs: {
            "points": {"covered": 1, "total": 1},
            "markets": {"covered": 1, "total": 1, "missing": 0},
            "provincial_areas": {"covered": 1, "total": 1},
            "baseline_points": {"covered": 1, "total": 1},
        },
    )
    monkeypatch.setattr(power_briefing, "_aggregate_market_insights", lambda *a, **k: [])
    monkeypatch.setattr(power_briefing, "_market_risk_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        power_briefing,
        "compare_market_risk_versions",
        lambda *a, **k: {"status": "unavailable", "previous_run_id": None},
    )
    monkeypatch.setattr(
        power_briefing,
        "build_run_provenance",
        lambda *a, **k: {"quality": {"status": "good"}},
    )
    monkeypatch.setattr(power_briefing, "briefing_statistics", lambda *a, **k: {})
    monkeypatch.setattr(
        power_briefing,
        "build_briefing_card",
        lambda *a, **kwargs: {"typhoon_block": kwargs.get("typhoon_block")},
    )

    class Cache:
        ttl_seconds = 3600

        def load_previous_same_release(self, **kwargs: object) -> None:
            return None

    client = TyphoonClient("secret", API_HOST)
    snapshot = await power_briefing.generate_briefing_snapshot(
        object(),
        client,
        "2026-08-10",
        cache=Cache(),  # type: ignore[arg-type]
        generated_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
    )

    typhoon_block = snapshot["summary_card"]["typhoon_block"]
    assert "台风实时数据不可用" in typhoon_block
    assert "不代表无活跃台风" in typhoon_block


def test_briefing_formatter_rejects_unverified_raw_typhoon_facts() -> None:
    block = format_active_for_briefing(
        [
            {
                "storm": {
                    "id": "NP202601",
                    "name": "未经门禁的台风",
                    "year": "2026",
                    "isActive": "1",
                },
                "now": {
                    "pubTime": "2026-08-09T09:00+08:00",
                    "lat": "20.0",
                    "lon": "120.0",
                    "windSpeed": "35",
                },
            }
        ]
    )

    assert block is not None
    assert "台风实时数据不可用" in block
    assert "未经门禁的台风" not in block


def test_briefing_formatter_does_not_render_provider_null_as_storm_name() -> None:
    provenance = {
        "provider": QWEATHER_TYPHOON_PROVIDER,
        "source_url": f"https://{API_HOST}/v7/tropical/storm-list",
        "retrieved_at": "2026-08-10T00:50:00+00:00",
        "content_sha256": "a" * 64,
        "attribution": "QWeather",
        "retention_policy": "metadata_only",
    }
    block = format_active_for_briefing(
        [
            {
                "storm": {
                    "id": "NP202616",
                    "name": "null",
                    "year": "2026",
                    "_provenance": provenance,
                },
                "now": {
                    "lat": "20.5",
                    "lon": "147.2",
                    "windSpeed": "20",
                    "_provenance": provenance,
                },
            }
        ]
    )

    assert block is not None
    assert "**null**" not in block
    assert "**未命名台风**" in block


def test_briefing_formatter_only_renders_storms_with_a_verified_market_relevance_link() -> None:
    provenance = {
        "provider": QWEATHER_TYPHOON_PROVIDER,
        "source_url": f"https://{API_HOST}/v7/tropical/storm-list",
        "retrieved_at": "2026-08-10T00:50:00+00:00",
        "content_sha256": "a" * 64,
        "attribution": "QWeather",
        "retention_policy": "metadata_only",
    }
    active = [
        {
            "storm": {
                "id": "NP202616",
                "name": "测试台风",
                "year": "2026",
                "_provenance": provenance,
            },
            "now": {
                "lat": "20.5",
                "lon": "147.2",
                "windSpeed": "20",
                "_provenance": provenance,
            },
            "affected_market_ids": ["cn-44-guangdong"],
            "impact_valid_time": {
                "start": "2026-08-11T08:00:00+08:00",
                "end": "2026-08-12T20:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
        }
    ]

    assert (
        format_active_for_briefing(active, market_ids={"cn-31-shanghai"})
        is None
    )
    relevant = format_active_for_briefing(active, market_ids={"cn-44-guangdong"})
    assert relevant is not None
    assert "测试台风" in relevant
    assert "关联关注分析区：cn-44-guangdong" in relevant
    assert "影响窗口：08/11 08:00–08/12 20:00" in relevant
    assert "来源：QWeather｜更新时间：08:50" in relevant
    assert "https://" not in relevant
