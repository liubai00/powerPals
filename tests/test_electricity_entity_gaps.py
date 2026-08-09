from datetime import datetime, time, timedelta, timezone

from services.weather_bot.electricity_entities import parse_electricity_entities


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 9, 8, 0, tzinfo=SHANGHAI)


def test_elapsed_valley_period_requires_historical_or_next_window_clarification() -> None:
    entities = parse_electricity_entities("今天谷段怎么样，谷段已过", now=NOW)

    assert entities.clarification_required is True
    assert "elapsed_trading_window" in entities.clarification_reasons
    assert entities.trading_window is None


def test_relative_six_hour_convection_window_is_anchored_to_current_clock() -> None:
    entities = parse_electricity_entities("未来6小时强对流", now=NOW)

    assert entities.trading_window is not None
    assert entities.trading_window.kind == "relative_hours"
    assert entities.trading_window.start_at == NOW
    assert entities.trading_window.end_at == NOW + timedelta(hours=6)
    assert entities.trading_window.label == "未来6小时"


def test_eight_to_fifteen_day_trend_is_an_extended_lead_window() -> None:
    entities = parse_electricity_entities("华东8–15天趋势", now=NOW)

    assert entities.forecast_period is not None
    assert entities.forecast_period.start_date.isoformat() == "2026-08-16"
    assert entities.forecast_period.end_date.isoformat() == "2026-08-23"
    assert entities.forecast_period.days == 8
    assert entities.forecast_period.requested_days == 15
    assert entities.forecast_period.horizon_kind == "extended_outlook"


def test_explicit_full_day_overrides_any_inherited_peak_window() -> None:
    entities = parse_electricity_entities("山东明天全天", now=NOW)

    assert entities.trading_window is not None
    assert entities.trading_window.kind == "full_day"
    assert entities.trading_window.start_time == time(0, 0)
    assert entities.trading_window.end_time == time(23, 59, 59)
    assert entities.trading_window.start_at == datetime(2026, 8, 10, 0, 0, tzinfo=SHANGHAI)
    assert entities.trading_window.end_at == datetime(2026, 8, 11, 0, 0, tzinfo=SHANGHAI)
    assert entities.trading_window.window_source == "explicit_user_text"
