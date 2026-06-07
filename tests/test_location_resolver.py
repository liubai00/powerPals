import pytest
from pydantic import ValidationError

from services.weather_bot.location import LocationResolver
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


def test_forecast_request_requires_coordinate_pair():
    with pytest.raises(ValidationError):
        ForecastRequest(latitude=22.5431, target_date="2026-06-10")
