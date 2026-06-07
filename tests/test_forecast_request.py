from services.weather_bot.models import ForecastRequest


def test_forecast_request_defaults_to_official_shenzhen_region():
    request = ForecastRequest(target_date="2026-06-10")

    assert request.region == "广东省深圳市"
