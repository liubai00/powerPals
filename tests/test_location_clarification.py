from datetime import date

import pytest

from services.weather_bot.config import Settings
from services.weather_bot.location import (
    AmbiguousLocationError,
    LocationNotFoundError,
    LocationResolver,
    ResolvedLocation,
)
from services.weather_bot.models import ForecastRequest


def _request(region: str) -> ForecastRequest:
    return ForecastRequest(region=region, target_date=date(2026, 8, 10).isoformat())


@pytest.mark.asyncio
async def test_ambiguous_bare_admin_name_requires_grounded_candidate_choice(monkeypatch) -> None:
    resolver = LocationResolver(Settings(qweather_api_key=None))

    async def unexpected_external_lookup(_region: str):
        raise AssertionError("known ambiguity must be clarified before external lookup")

    monkeypatch.setattr(resolver, "_resolve_with_nominatim", unexpected_external_lookup)
    monkeypatch.setattr(resolver, "_resolve_with_open_meteo", unexpected_external_lookup)

    with pytest.raises(AmbiguousLocationError) as exc_info:
        await resolver.resolve(_request("朝阳"))

    assert exc_info.value.region == "朝阳"
    assert exc_info.value.candidates == ("北京市朝阳区", "辽宁省朝阳市")


@pytest.mark.asyncio
async def test_unknown_location_returns_typed_not_found_after_resolvers_reject_it(monkeypatch) -> None:
    resolver = LocationResolver(Settings(qweather_api_key=None))

    async def no_match(_region: str):
        return None

    monkeypatch.setattr(resolver, "_resolve_with_nominatim", no_match)
    monkeypatch.setattr(resolver, "_resolve_with_open_meteo", no_match)

    with pytest.raises(LocationNotFoundError) as exc_info:
        await resolver.resolve(_request("火星市"))

    assert exc_info.value.region == "火星市"


@pytest.mark.asyncio
async def test_province_qualified_ambiguous_name_can_resolve_normally(monkeypatch) -> None:
    resolver = LocationResolver(Settings(qweather_api_key=None))
    expected = ResolvedLocation(
        name="北京市朝阳区",
        latitude=39.9219,
        longitude=116.4436,
        source="test_geocoder",
        province="北京市",
        city="朝阳区",
    )

    async def resolve_beijing(region: str):
        assert region == "北京朝阳区"
        return expected

    monkeypatch.setattr(resolver, "_resolve_with_nominatim", resolve_beijing)

    resolved = await resolver.resolve(_request("北京朝阳区"))

    assert resolved == expected
