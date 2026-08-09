from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json

import httpx
import pytest

from services.weather_bot.config import Settings
from services.weather_bot.location import LocationNotFoundError, LocationResolver
from services.weather_bot.models import ForecastRequest
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


QWEATHER_GEO_PROVIDER = "qweather_geocoding"
QWEATHER_GEO_ENDPOINT = "https://geo-api.qweather.test/geo/v2/city/lookup"
NOMINATIM_GEO_PROVIDER = "nominatim_geocoding"
NOMINATIM_GEO_ENDPOINT = "https://nominatim.openstreetmap.org/search"
OPEN_METEO_GEO_PROVIDER = "open_meteo_geocoding"
OPEN_METEO_GEO_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"


def _policy(
    provider: str,
    endpoint: str,
    **overrides: object,
) -> SourcePolicy:
    values: dict[str, object] = {
        "provider": provider,
        "environment": "test",
        "profile": "verified-geocoder-test",
        "license_status": "verified",
        "allowed_uses": {"calculation", "derived_storage"},
        "terms_version": "test-terms-2026-08-09",
        "source_url_prefixes": (endpoint,),
        "unit_manifest": (
            "name:text;latitude:degree;longitude:degree;country:text;"
            "province:text;city:text"
        ),
        "required_metrics": (
            "name",
            "latitude",
            "longitude",
            "country",
            "province",
            "city",
        ),
        "coverage_model": "place-candidate",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 86400,
        "retention_policy": "derived_only",
        "attribution_required": True,
        "attribution_text": "Test geocoder",
    }
    values.update(overrides)
    return SourcePolicy(**values)


def _request(region: str) -> ForecastRequest:
    return ForecastRequest(region=region, target_date=date(2026, 8, 10).isoformat())


@pytest.mark.asyncio
async def test_empty_source_registry_keeps_builtin_locations_but_unknown_is_zero_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("unlicensed geocoder must not construct an HTTP client")

    monkeypatch.setattr(
        "services.weather_bot.location.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    resolver = LocationResolver(Settings(_env_file=None, qweather_api_key="secret"))

    builtin = await resolver.resolve(_request("广州"))
    assert builtin.source == "builtin"

    with pytest.raises(LocationNotFoundError) as exc_info:
        await resolver.resolve(_request("未登记测试地点"))

    assert exc_info.value.region == "未登记测试地点"


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
        "malformed_host",
    ],
)
async def test_qweather_geocoder_rejects_invalid_policy_before_http(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    policy = _policy(QWEATHER_GEO_PROVIDER, QWEATHER_GEO_ENDPOINT)
    registry = SourceRegistry([policy], environment="test")
    host = "geo-api.qweather.test"
    if case == "empty_registry":
        registry = SourceRegistry([], environment="test")
    elif case == "unknown_license":
        policy = SourcePolicy.unconfigured(QWEATHER_GEO_PROVIDER, "test")
        registry = SourceRegistry([policy], environment="test")
    elif case == "wrong_endpoint":
        policy = _policy(
            QWEATHER_GEO_PROVIDER,
            "https://wrong.example.test/geo/v2/city/lookup",
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "overbroad_endpoint":
        policy = _policy(
            QWEATHER_GEO_PROVIDER,
            "https://geo-api.qweather.test/geo/",
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "wrong_environment":
        registry = SourceRegistry([], environment="production")
    elif case == "calculation_not_allowed":
        policy = _policy(
            QWEATHER_GEO_PROVIDER,
            QWEATHER_GEO_ENDPOINT,
            allowed_uses={"text_reference", "derived_storage"},
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "attribution_missing":
        policy = _policy(
            QWEATHER_GEO_PROVIDER,
            QWEATHER_GEO_ENDPOINT,
            attribution_required=False,
            attribution_text=None,
        )
        registry = SourceRegistry([policy], environment="test")
    elif case == "malformed_host":
        host = "reader@geo-api.qweather.test"

    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("rejected QWeather geocoder must be zero HTTP")

    monkeypatch.setattr(
        "services.weather_bot.location.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    resolver = LocationResolver(
        Settings(
            _env_file=None,
            app_env="test",
            qweather_api_key="secret",
            qweather_api_host=host,
        ),
        source_registry=registry,
    )

    with pytest.raises(LocationNotFoundError):
        await resolver.resolve(_request("未登记测试地点"))


@pytest.mark.asyncio
async def test_qweather_geocoder_with_verified_policy_but_no_credential_is_zero_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverCreateHttpClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("missing QWeather credential must be zero HTTP")

    monkeypatch.setattr(
        "services.weather_bot.location.httpx.AsyncClient",
        NeverCreateHttpClient,
    )
    policy = _policy(QWEATHER_GEO_PROVIDER, QWEATHER_GEO_ENDPOINT)
    resolver = LocationResolver(
        Settings(
            _env_file=None,
            app_env="test",
            qweather_api_key=None,
            qweather_api_host="geo-api.qweather.test",
        ),
        source_registry=SourceRegistry([policy], environment="test"),
    )

    with pytest.raises(LocationNotFoundError):
        await resolver.resolve(_request("未登记测试地点"))


@pytest.mark.asyncio
async def test_allowed_qweather_geocoder_returns_only_traceable_normalized_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = json.dumps(
        {
            "code": "200",
            "location": [
                {
                    "name": "测试市",
                    "id": "101999999",
                    "lat": "30.1",
                    "lon": "120.2",
                    "adm1": "浙江省",
                    "adm2": "测试市",
                    "country": "中国",
                    "rawNarrative": "must not escape",
                }
            ],
            "refer": {"sources": ["QWeather"], "license": ["commercial"]},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=raw_body,
                headers={"content-type": "application/json"},
            )

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.location.httpx.AsyncClient", fake_client)
    policy = _policy(
        QWEATHER_GEO_PROVIDER,
        QWEATHER_GEO_ENDPOINT,
        attribution_text="QWeather",
    )
    resolver = LocationResolver(
        Settings(
            _env_file=None,
            app_env="test",
            qweather_api_key="secret",
            qweather_api_host="geo-api.qweather.test",
        ),
        source_registry=SourceRegistry([policy], environment="test"),
        clock=lambda: datetime(2026, 8, 9, 1, 5, tzinfo=timezone.utc),
    )

    location = await resolver.resolve(_request("测试市"))

    assert len(requests) == 1
    assert location.name == "浙江省测试市"
    assert location.source == "qweather_geo"
    assert location.source_provider == QWEATHER_GEO_PROVIDER
    assert location.source_url == str(requests[0].url)
    assert location.retrieved_at == "2026-08-09T01:05:00+00:00"
    assert location.content_sha256 == sha256(raw_body).hexdigest()
    assert location.attribution == "QWeather"
    assert location.retention_policy == "derived_only"
    assert "rawNarrative" not in location.model_dump_json()
    assert "refer" not in location.model_dump_json()
    assert "secret" not in location.model_dump_json()


@pytest.mark.asyncio
async def test_calculation_only_geocoder_policy_forces_metadata_only_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = json.dumps(
        {
            "code": "200",
            "location": [
                {
                    "name": "测试市",
                    "id": "101999999",
                    "lat": "30.1",
                    "lon": "120.2",
                    "adm1": "浙江省",
                    "adm2": "测试市",
                    "country": "中国",
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=raw_body)
            )
        )

    monkeypatch.setattr("services.weather_bot.location.httpx.AsyncClient", fake_client)
    policy = _policy(
        QWEATHER_GEO_PROVIDER,
        QWEATHER_GEO_ENDPOINT,
        allowed_uses={"calculation"},
        retention_policy="derived_only",
    )
    resolver = LocationResolver(
        Settings(
            _env_file=None,
            app_env="test",
            qweather_api_key="secret",
            qweather_api_host="geo-api.qweather.test",
        ),
        source_registry=SourceRegistry([policy], environment="test"),
        clock=lambda: datetime(2026, 8, 9, 1, 5, tzinfo=timezone.utc),
    )

    location = await resolver.resolve(_request("测试市"))

    assert location.retention_policy == "metadata_only"


@pytest.mark.asyncio
async def test_allowed_nominatim_geocoder_returns_traceable_normalized_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = json.dumps(
        [
            {
                "name": "测试市",
                "lat": "30.1",
                "lon": "120.2",
                "type": "administrative",
                "addresstype": "city",
                "display_name": "测试市, 浙江省, 中国",
                "importance": 0.9,
                "place_rank": 10,
                "rawDisplayDetails": "must not escape",
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=raw_body)

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.location.httpx.AsyncClient", fake_client)
    policy = _policy(
        NOMINATIM_GEO_PROVIDER,
        NOMINATIM_GEO_ENDPOINT,
        attribution_text="OpenStreetMap contributors",
    )
    resolver = LocationResolver(
        Settings(_env_file=None, app_env="test", qweather_api_key=None),
        source_registry=SourceRegistry([policy], environment="test"),
        clock=lambda: datetime(2026, 8, 9, 1, 5, tzinfo=timezone.utc),
    )

    location = await resolver.resolve(_request("测试市"))

    assert len(requests) == 1
    assert requests[0].url.host == "nominatim.openstreetmap.org"
    assert location.source == "nominatim_geo"
    assert location.source_provider == NOMINATIM_GEO_PROVIDER
    assert location.source_url == str(requests[0].url)
    assert location.content_sha256 == sha256(raw_body).hexdigest()
    assert location.attribution == "OpenStreetMap contributors"
    assert "rawDisplayDetails" not in location.model_dump_json()


@pytest.mark.asyncio
async def test_allowed_open_meteo_geocoder_returns_traceable_normalized_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = json.dumps(
        {
            "results": [
                {
                    "name": "测试市",
                    "latitude": 30.1,
                    "longitude": 120.2,
                    "country": "中国",
                    "country_code": "CN",
                    "admin1": "浙江",
                    "admin2": "测试市",
                    "feature_code": "PPLA2",
                    "population": 1000000,
                    "rawTimezoneDetails": "must not escape",
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=raw_body)

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.location.httpx.AsyncClient", fake_client)
    policy = _policy(
        OPEN_METEO_GEO_PROVIDER,
        OPEN_METEO_GEO_ENDPOINT,
        attribution_text="GeoNames via Open-Meteo",
    )
    resolver = LocationResolver(
        Settings(_env_file=None, app_env="test", qweather_api_key=None),
        source_registry=SourceRegistry([policy], environment="test"),
        clock=lambda: datetime(2026, 8, 9, 1, 5, tzinfo=timezone.utc),
    )

    location = await resolver.resolve(_request("测试市"))

    assert requests
    assert all(request.url.host == "geocoding-api.open-meteo.com" for request in requests)
    assert location.source == "open_meteo_geo"
    assert location.source_provider == OPEN_METEO_GEO_PROVIDER
    assert location.source_url == str(requests[0].url)
    assert location.content_sha256 == sha256(raw_body).hexdigest()
    assert location.attribution == "GeoNames via Open-Meteo"
    assert "rawTimezoneDetails" not in location.model_dump_json()
