import pytest
from pydantic import ValidationError

from services.weather_bot.location import (
    LocationResolver,
    ResolvedLocation,
    _best_nominatim_result,
    _best_open_meteo_result,
    _nominatim_province,
    _normalize_china_admin,
    _open_meteo_search_terms,
)
from services.weather_bot.models import ForecastRequest


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
        assert region == "珠海"
        return ResolvedLocation(
            name="广东省珠海市",
            latitude=22.273734,
            longitude=113.572133,
            source="nominatim_geo",
            province="广东省",
            city="珠海市",
        )

    async def fail_open_meteo(self, region):
        raise AssertionError("Open-Meteo should not be used when Nominatim resolves the city")

    monkeypatch.setattr(LocationResolver, "_resolve_with_nominatim", fake_nominatim)
    monkeypatch.setattr(LocationResolver, "_resolve_with_open_meteo", fail_open_meteo)

    location = await LocationResolver().resolve(ForecastRequest(region="珠海", target_date="2026-06-10"))

    assert location.name == "广东省珠海市"
    assert location.latitude == 22.273734
    assert location.source == "nominatim_geo"


def test_forecast_request_requires_coordinate_pair():
    with pytest.raises(ValidationError):
        ForecastRequest(latitude=22.5431, target_date="2026-06-10")
