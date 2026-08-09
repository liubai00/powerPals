import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from services.weather_bot.config import Settings
from services.weather_bot.main import create_app
from services.weather_bot.location import (
    QWEATHER_GEO_PROVIDER,
    QWEATHER_GEO_PATH,
    LocationResolver,
    PROVINCE_LEVEL_LOCATIONS,
    ResolvedLocation,
    _best_nominatim_result,
    _best_open_meteo_result,
    _best_qweather_result,
    _nominatim_province,
    _normalize_china_admin,
    _open_meteo_search_terms,
    interpret_region_scope,
)
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService
from services.weather_bot.source_registry import SourcePolicy, SourceRegistry


async def test_location_resolver_keeps_shenzhen_as_default():
    location = await LocationResolver().resolve(ForecastRequest(target_date="2026-06-10"))

    assert location.name == "广东省深圳市"
    assert location.code == "440300"
    assert location.latitude == 22.5431
    assert location.longitude == 114.0579
    assert location.source == "builtin"


async def test_location_resolver_supports_city_aliases_nationwide():
    location = await LocationResolver().resolve(ForecastRequest(region="广州", target_date="2026-06-10"))

    assert location.name == "广东省广州市"
    assert location.code == "440100"
    assert location.latitude == 23.1291
    assert location.longitude == 113.2644


async def test_location_resolver_supports_normalized_builtin_city_names():
    location = await LocationResolver().resolve(ForecastRequest(region="广东省广州市", target_date="2026-06-10"))

    assert location.name == "广东省广州市"
    assert location.code == "440100"
    assert location.latitude == 23.1291
    assert location.longitude == 113.2644


async def test_location_resolver_supports_province_level_aliases():
    location = await LocationResolver().resolve(ForecastRequest(region="辽宁", target_date="2026-06-10"))

    assert location.name == "辽宁省"
    assert location.code == "210000"
    assert location.latitude == 41.8057
    assert location.longitude == 123.4315
    assert location.city == "沈阳市"


def test_forecast_api_regression_for_liaoning_whole_region_request(
    external_source_metadata,
    verified_test_source_registry,
    test_source_clock,
):
    class FakeProvider:
        name = "open_meteo"
        source_endpoints = ("https://open_meteo.weather.test/v1/forecast",)

        async def fetch(self, request):
            return ProviderForecast(
                provider=self.name,
                status="ok",
                points=[
                    ForecastPoint(
                        time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                        temperature=28.0,
                        precipitation_probability=20.0,
                        wind_speed=3.0,
                        cloud_cover=40.0,
                    )
                    for hour in range(24)
                ],
                **external_source_metadata("open_meteo"),
            )

    settings = Settings(_env_file=None, app_env="test", qweather_api_key="")
    resolver = LocationResolver(settings)
    service = ForecastService(
        providers={"open_meteo": FakeProvider()},
        location_resolver=resolver,
        settings=settings,
        source_registry=verified_test_source_registry(
            {"open_meteo": "https://open_meteo.weather.test/v1/forecast"}
        ),
        clock=test_source_clock,
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/api/weather/forecast",
        json={
            "region": "辽宁整个地区",
            "target_date": "2026-07-28",
            "days": 1,
            "providers": ["open_meteo"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["region"] == "辽宁省"
    assert body["scope"]["location"]["source"] == "builtin"
    assert body["scope"]["location"]["representation"] == "province_representative_point"
    assert body["scope"]["location"]["representative_city"] == "沈阳市"


@pytest.mark.parametrize(
    "region",
    ["辽宁整个地区", "辽宁地区", "整个辽宁地区", "辽宁全省", "辽宁省全境", "辽宁省内"],
)
async def test_location_resolver_normalizes_province_scope_before_external_geocoding(monkeypatch, region):
    async def fail_external(self, value):
        raise AssertionError(f"province scope leaked to external geocoder: {value}")

    monkeypatch.setattr(LocationResolver, "_resolve_with_nominatim", fail_external)
    monkeypatch.setattr(LocationResolver, "_resolve_with_open_meteo", fail_external)

    location = await LocationResolver().resolve(ForecastRequest(region=region, target_date="2026-06-10"))

    assert location.name == "辽宁省"
    assert location.code == "210000"
    assert location.city == "沈阳市"


async def test_qweather_candidate_selection_rejects_wrong_province(monkeypatch):
    payload = {
        "code": "200",
        "location": [
            {
                "name": "阿里地区",
                "id": "101140701",
                "lat": "32.50319",
                "lon": "80.10550",
                "adm1": "西藏自治区",
                "adm2": "阿里地区",
                "country": "中国",
            },
            {
                "name": "盘锦市",
                "id": "101071301",
                "lat": "41.11996",
                "lon": "122.07078",
                "adm1": "辽宁省",
                "adm2": "盘锦市",
                "country": "中国",
            },
        ],
    }
    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        async def handler(request):
            return httpx.Response(200, json=payload, request=request)

        return original_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("services.weather_bot.location.httpx.AsyncClient", fake_client)
    endpoint = f"https://geoapi.qweather.com{QWEATHER_GEO_PATH}"
    policy = SourcePolicy(
        provider=QWEATHER_GEO_PROVIDER,
        environment="test",
        profile="verified-qweather-geocoder-test",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="test-only",
        source_url_prefixes=(endpoint,),
        unit_manifest=(
            "name:text;latitude:degree;longitude:degree;country:text;"
            "province:text;city:text"
        ),
        required_metrics=(
            "name",
            "latitude",
            "longitude",
            "country",
            "province",
            "city",
        ),
        coverage_model="place-candidate",
        timezone="Asia/Shanghai",
        max_age_seconds=86400,
        retention_policy="derived_only",
        retention_seconds=86_400,
        attribution_required=True,
        attribution_text="QWeather",
    )
    resolver = LocationResolver(
        Settings(_env_file=None, app_env="test", qweather_api_key="test-key"),
        source_registry=SourceRegistry([policy], environment="test"),
    )

    location = await resolver._resolve_with_qweather("辽宁盘锦")

    assert location is not None
    assert location.name == "辽宁省盘锦市"
    assert location.province == "辽宁省"


def test_qweather_candidate_selection_returns_none_when_all_candidates_are_unrelated():
    result = _best_qweather_result(
        [
            {
                "name": "阿里地区",
                "lat": "32.50319",
                "lon": "80.10550",
                "adm1": "西藏自治区",
                "adm2": "阿里地区",
                "country": "中国",
            }
        ],
        "辽宁盘锦",
    )

    assert result is None


PROVINCE_SCOPE_CASES = [
    (f"{aliases[-1]}{scope}", canonical)
    for aliases, canonical, _code, _lat, _lon, _province, _city in PROVINCE_LEVEL_LOCATIONS
    for scope in ("整个地区", "地区", "全省", "省全境", "省内")
]


@pytest.mark.parametrize(("region", "expected"), PROVINCE_SCOPE_CASES)
def test_all_province_level_aliases_separate_scope_modifier(region, expected):
    interpreted = interpret_region_scope(region)

    assert interpreted.entity == expected
    assert interpreted.scope is not None


@pytest.mark.parametrize(
    "region",
    [
        "上海浦东新区",
        "深圳南山区",
        "西藏阿里地区",
        "新疆塔城地区",
        "黑龙江大兴安岭地区",
        "辽宁盘锦",
    ],
)
def test_scope_normalizer_preserves_real_subregions(region):
    interpreted = interpret_region_scope(region)

    assert interpreted.entity == region
    assert interpreted.scope is None


async def test_location_resolver_supports_explicit_coordinates():
    location = await LocationResolver().resolve(
        ForecastRequest(
            region="广州南沙",
            latitude=22.8016,
            longitude=113.5252,
            target_date="2026-06-10",
        )
    )

    assert location.name == "广州南沙"
    assert location.code is None
    assert location.latitude == 22.8016
    assert location.longitude == 113.5252
    assert location.source == "coordinates"


def test_nominatim_result_scoring_prefers_administrative_city():
    results = [
        {
            "name": "珠海",
            "lat": "35.87124",
            "lon": "119.99638",
            "type": "hamlet",
            "addresstype": "village",
            "display_name": "珠海, 青岛市, 山东省, 中国",
            "importance": 0.1,
            "place_rank": 19,
        },
        {
            "name": "珠海市",
            "lat": "22.2737340",
            "lon": "113.5721327",
            "type": "administrative",
            "addresstype": "city",
            "display_name": "珠海市, 广东省, 中国",
            "importance": 0.59,
            "place_rank": 10,
        },
    ]

    item = _best_nominatim_result(results, "珠海")

    assert item["name"] == "珠海市"
    assert _nominatim_province(item["display_name"]) == "广东省"


def test_open_meteo_scoring_prefers_city_variant_for_bare_city_name():
    results = [
        {
            "name": "珠海",
            "latitude": 35.87124,
            "longitude": 119.99638,
            "country_code": "CN",
            "admin1": "山东",
            "admin2": "青岛市",
            "feature_code": "PPL",
            "population": None,
            "_query": "珠海",
        },
        {
            "name": "珠海市",
            "latitude": 22.27694,
            "longitude": 113.56778,
            "country_code": "CN",
            "admin1": "广东",
            "admin2": "珠海市",
            "feature_code": "PPLA2",
            "population": 2207090,
            "_query": "珠海市",
        },
    ]

    item = _best_open_meteo_result(results, "珠海")

    assert _open_meteo_search_terms("珠海")[:2] == ["珠海", "珠海市"]
    assert item["name"] == "珠海市"
    assert item["admin1"] == "广东"
    assert _normalize_china_admin(item["admin1"]) == "广东省"


async def test_location_resolver_uses_nominatim_before_open_meteo_for_arbitrary_city(monkeypatch):
    async def fake_nominatim(self, region):
        assert region == "漠河"
        return ResolvedLocation(
            name="黑龙江省漠河市",
            latitude=52.972272,
            longitude=122.538592,
            source="nominatim_geo",
            province="黑龙江省",
            city="漠河市",
        )

    async def fail_open_meteo(self, region):
        raise AssertionError("Open-Meteo should not be used when Nominatim resolves the city")

    monkeypatch.setattr(LocationResolver, "_resolve_with_nominatim", fake_nominatim)
    monkeypatch.setattr(LocationResolver, "_resolve_with_open_meteo", fail_open_meteo)

    location = await LocationResolver().resolve(ForecastRequest(region="漠河", target_date="2026-06-10"))

    assert location.name == "黑龙江省漠河市"
    assert location.latitude == 52.972272
    assert location.source == "nominatim_geo"


def test_forecast_request_requires_coordinate_pair():
    with pytest.raises(ValidationError):
        ForecastRequest(latitude=22.5431, target_date="2026-06-10")
