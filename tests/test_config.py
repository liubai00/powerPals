from services.weather_bot.config import Settings


def test_blank_default_coordinates_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("DEFAULT_WEATHER_LATITUDE", "")
    monkeypatch.setenv("DEFAULT_WEATHER_LONGITUDE", "")

    settings = Settings(_env_file=None)

    assert settings.default_weather_latitude is None
    assert settings.default_weather_longitude is None
