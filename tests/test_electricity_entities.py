from datetime import datetime, timedelta, timezone

import pytest

from services.weather_bot.electricity_entities import parse_electricity_entities


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
FIXED_NOW = datetime(2026, 8, 9, 8, 0, tzinfo=SHANGHAI)


def test_parses_provincial_area_and_tomorrow_evening_peak() -> None:
    entities = parse_electricity_entities("山东明日晚峰风险", now=FIXED_NOW)

    assert [(area.kind, area.area_id, area.name) for area in entities.analysis_areas] == [
        ("provincial_area", "cn-province-37", "山东")
    ]
    assert entities.analysis_areas[0].analysis_zone_ids == ("cn-37-shandong",)
    assert entities.analysis_areas[0].evidence.raw_text == "山东"
    assert entities.forecast_period is not None
    assert entities.forecast_period.start_date.isoformat() == "2026-08-10"
    assert entities.forecast_period.days == 1
    assert entities.trading_window is not None
    assert entities.trading_window.kind == "evening_peak"
    assert entities.trading_window.start_at.isoformat() == "2026-08-10T17:00:00+08:00"
    assert entities.trading_window.end_at.isoformat() == "2026-08-10T21:00:00+08:00"
    assert entities.trading_window.evidence.raw_text == "晚峰"
    assert entities.confidence >= 0.9


@pytest.mark.parametrize(
    ("text", "kind", "start_hour", "end_hour", "evidence"),
    [
        ("山东明日早峰风险", "early_peak", 7, 10, "早峰"),
        ("浙江明天午间光伏", "midday_solar", 11, 16, "午间光伏"),
    ],
)
def test_parses_named_power_windows(
    text: str,
    kind: str,
    start_hour: int,
    end_hour: int,
    evidence: str,
) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert entities.trading_window is not None
    assert entities.trading_window.kind == kind
    assert entities.trading_window.start_time.hour == start_hour
    assert entities.trading_window.end_time.hour == end_hour
    assert entities.trading_window.evidence.raw_text == evidence


@pytest.mark.parametrize(
    ("text", "days", "end_date", "evidence"),
    [
        ("辽宁未来3天负荷压力", 3, "2026-08-11", "未来3天"),
        ("山东未来7天", 7, "2026-08-15", "未来7天"),
    ],
)
def test_parses_multi_day_forecast_periods(
    text: str,
    days: int,
    end_date: str,
    evidence: str,
) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert entities.forecast_period is not None
    assert entities.forecast_period.start_date.isoformat() == "2026-08-09"
    assert entities.forecast_period.end_date.isoformat() == end_date
    assert entities.forecast_period.days == days
    assert entities.forecast_period.horizon_kind == "multi_day_trend"
    assert entities.forecast_period.evidence.raw_text == evidence


@pytest.mark.parametrize(
    ("text", "zone_id", "zone_name"),
    [
        ("蒙西明日晚峰风险", "cn-15-mengxi", "蒙西"),
        ("蒙东明日晚峰风险", "cn-15-mengdong", "蒙东"),
        ("冀北分析区明日早峰", "cn-13-jibei", "冀北"),
        ("河北南网样本区明日早峰", "cn-13-hebeisouth", "河北南网"),
    ],
)
def test_parses_explicit_analysis_zones_without_collapsing_them_to_provinces(
    text: str,
    zone_id: str,
    zone_name: str,
) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert len(entities.analysis_areas) == 1
    area = entities.analysis_areas[0]
    assert area.kind == "analysis_zone"
    assert area.area_id == zone_id
    assert area.name == zone_name
    assert area.analysis_zone_ids == (zone_id,)
    assert area.evidence.raw_text in text
    assert area.confidence == 1.0


def test_parses_regional_collection_and_resolves_its_configured_zones() -> None:
    entities = parse_electricity_entities("华东明天有哪些高温分析区", now=FIXED_NOW)

    assert len(entities.analysis_areas) == 1
    area = entities.analysis_areas[0]
    assert (area.kind, area.area_id, area.name) == ("regional_collection", "cn-region-east", "华东")
    assert set(area.analysis_zone_ids) == {
        "cn-31-shanghai",
        "cn-32-jiangsu",
        "cn-33-zhejiang",
        "cn-34-anhui",
        "cn-35-fujian",
        "cn-36-jiangxi",
        "cn-37-shandong",
    }
    assert area.evidence.raw_text == "华东"


def test_parses_explicit_national_scope_as_all_configured_analysis_zones() -> None:
    entities = parse_electricity_entities("全国明日 Top 5", now=FIXED_NOW)

    assert len(entities.analysis_areas) == 1
    area = entities.analysis_areas[0]
    assert (area.kind, area.area_id, area.name) == ("national", "cn-national", "全国")
    assert len(area.analysis_zone_ids) == 33
    assert len(set(area.analysis_zone_ids)) == 33


@pytest.mark.parametrize(
    ("text", "stage", "evidence"),
    [
        ("山东日前价格风险", "day_ahead", "日前"),
        ("广东日内剩余时段", "intraday", "日内"),
        ("山东实时市场气象风险", "real_time", "实时"),
    ],
)
def test_parses_electricity_market_stages(text: str, stage: str, evidence: str) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert entities.market_stage is not None
    assert entities.market_stage.stage == stage
    assert entities.market_stage.evidence.raw_text == evidence
    assert entities.market_stage.confidence >= 0.9


def test_intraday_remaining_resolves_from_current_time_to_local_day_end() -> None:
    entities = parse_electricity_entities("广东日内剩余时段", now=FIXED_NOW)

    assert entities.trading_window is not None
    assert entities.trading_window.kind == "intraday_remaining"
    assert entities.trading_window.start_at.isoformat() == "2026-08-09T08:00:00+08:00"
    assert entities.trading_window.end_at.isoformat() == "2026-08-10T00:00:00+08:00"
    assert entities.trading_window.window_source == "current_clock"


def test_real_time_market_stage_resolves_to_a_clock_snapshot() -> None:
    entities = parse_electricity_entities("山东实时市场气象风险", now=FIXED_NOW)

    assert entities.trading_window is not None
    assert entities.trading_window.kind == "real_time_snapshot"
    assert entities.trading_window.start_at == FIXED_NOW
    assert entities.trading_window.end_at == FIXED_NOW


def test_explicit_clock_range_overrides_a_named_default_window() -> None:
    entities = parse_electricity_entities("明天18点到22点山东晚峰", now=FIXED_NOW)

    assert entities.trading_window is not None
    assert entities.trading_window.kind == "explicit_clock_range"
    assert entities.trading_window.start_at.isoformat() == "2026-08-10T18:00:00+08:00"
    assert entities.trading_window.end_at.isoformat() == "2026-08-10T22:00:00+08:00"
    assert entities.trading_window.evidence.raw_text == "18点到22点"
    assert entities.trading_window.window_source == "explicit_user_text"


def test_invalid_clock_time_requires_clarification_instead_of_guessing() -> None:
    entities = parse_electricity_entities("山东明天25:00", now=FIXED_NOW)

    assert entities.trading_window is None
    assert entities.clarification_required is True
    assert "invalid_clock_time" in entities.clarification_reasons


def test_date_phrase_and_generic_regions_are_not_treated_as_a_location() -> None:
    entities = parse_electricity_entities("7月下旬各地区天气", now=FIXED_NOW)

    assert entities.analysis_areas == ()
    assert entities.forecast_period is not None
    assert entities.forecast_period.evidence.raw_text == "7月下旬"
    assert entities.clarification_required is True
    assert "ambiguous_analysis_scope" in entities.clarification_reasons


@pytest.mark.parametrize(
    ("text", "blocked_fact"),
    [
        ("山东当前实际负荷多少", "actual_load"),
        ("浙江实际光伏出力多少", "actual_generation"),
        ("山东日前价格会涨吗", "price"),
    ],
)
def test_power_fact_requests_are_structured_but_never_filled_without_external_data(
    text: str,
    blocked_fact: str,
) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert entities.data_boundary.can_emit_power_facts is False
    assert entities.data_boundary.allowed_output_level == "weather_proxy_only"
    assert entities.data_boundary.blocked_fact_types == (blocked_fact,)
    assert entities.data_boundary.required_external_data == (blocked_fact,)
    assert entities.data_boundary.reason == "traceable_external_power_data_required"
    assert entities.data_boundary.requirements[0].fact_type == blocked_fact
    assert entities.data_boundary.requirements[0].evidence.raw_text in text
    assert entities.data_boundary.requirements[0].confidence >= 0.9


def test_grounded_province_abbreviation_keeps_original_evidence() -> None:
    entities = parse_electricity_entities("鲁明日晚峰", now=FIXED_NOW)

    assert len(entities.analysis_areas) == 1
    area = entities.analysis_areas[0]
    assert (area.kind, area.area_id, area.name) == ("provincial_area", "cn-province-37", "山东")
    assert area.evidence.raw_text == "鲁"
    assert area.evidence.rule_id == "province_abbreviation"
    assert area.confidence == 0.8


def test_multiple_provinces_keep_user_order_for_comparison() -> None:
    entities = parse_electricity_entities("山东、河南、河北晚峰对比", now=FIXED_NOW)

    assert [area.name for area in entities.analysis_areas] == ["山东", "河南", "河北"]
    assert all(area.kind == "provincial_area" for area in entities.analysis_areas)


def test_weather_realtime_and_cloud_words_do_not_become_power_or_province_entities() -> None:
    entities = parse_electricity_entities("实时天气里云量和降水怎么样", now=FIXED_NOW)

    assert entities.market_stage is None
    assert entities.analysis_areas == ()
    assert entities.data_boundary.blocked_fact_types == ()


@pytest.mark.parametrize(
    ("text", "name", "zone_id"),
    [
        ("广西全区未来3天", "广西", "cn-45-guangxi"),
        ("宁夏未来3天", "宁夏", "cn-64-ningxia"),
        ("新疆未来7天", "新疆", "cn-65-xinjiang"),
    ],
)
def test_autonomous_regions_use_the_same_names_as_analysis_zone_configuration(
    text: str,
    name: str,
    zone_id: str,
) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert entities.analysis_areas[0].name == name
    assert entities.analysis_areas[0].analysis_zone_ids == (zone_id,)


@pytest.mark.parametrize(
    "text",
    [
        "吉林市未来3天",
        "辽宁盘锦未来3天",
        "上海浦东新区明天天气",
    ],
)
def test_specific_city_or_district_is_left_for_the_location_resolver(text: str) -> None:
    entities = parse_electricity_entities(text, now=FIXED_NOW)

    assert entities.analysis_areas == ()


def test_next_weekday_uses_the_calendar_week_not_an_extra_seven_days() -> None:
    entities = parse_electricity_entities("下周三山东晚峰", now=FIXED_NOW)

    assert entities.forecast_period is not None
    assert entities.forecast_period.start_date.isoformat() == "2026-08-12"
    assert entities.trading_window is not None
    assert entities.trading_window.start_at.isoformat() == "2026-08-12T17:00:00+08:00"
