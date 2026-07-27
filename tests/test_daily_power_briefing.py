from datetime import datetime
import json

import pytest

from scripts import daily_power_briefing
from scripts.daily_power_briefing import _confidence_label, _continuous_windows, build_briefing_card
from services.weather_bot import main as weather_main
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ProviderForecast,
    WeatherSubmission,
)
from services.weather_bot.power_briefing import briefing_coverage, briefing_statistics
from services.weather_bot.power_briefing_markets import (
    MAINLAND_PROVINCIAL_AREAS,
    NATIONAL_MARKETS,
    representative_points,
    validate_market_config,
)


def _points(
    target_date: str,
    *,
    base_temperature: float = 25.0,
    hot_hours: set[int] | None = None,
    cloudy_hours: set[int] | None = None,
    wind_hours: set[int] | None = None,
    offset: float = 0.0,
) -> list[ForecastPoint]:
    hot_hours = hot_hours or set()
    cloudy_hours = cloudy_hours or set()
    wind_hours = wind_hours or set()
    return [
        ForecastPoint(
            time=f"{target_date}T{hour:02d}:00:00+08:00",
            temperature=(37.0 if hour in hot_hours else base_temperature) + offset,
            apparent_temperature=(39.0 if hour in hot_hours else base_temperature + 1) + offset,
            precipitation_probability=75.0 if hour in cloudy_hours else 10.0,
            cloud_cover=92.0 if hour in cloudy_hours else 25.0,
            wind_speed=11.0 if hour in wind_hours else 4.0,
        )
        for hour in range(24)
    ]


def _submission(
    target_date: str,
    *,
    hot_hours: set[int] | None = None,
    cloudy_hours: set[int] | None = None,
    wind_hours: set[int] | None = None,
    provider_offset: float = 0.5,
) -> WeatherSubmission:
    aggregated = _points(
        target_date,
        hot_hours=hot_hours,
        cloudy_hours=cloudy_hours,
        wind_hours=wind_hours,
    )
    provider_a = _points(
        target_date,
        hot_hours=hot_hours,
        cloudy_hours=cloudy_hours,
        wind_hours=wind_hours,
    )
    provider_b = _points(
        target_date,
        hot_hours=hot_hours,
        cloudy_hours=cloudy_hours,
        wind_hours=wind_hours,
        offset=provider_offset,
    )
    return WeatherSubmission(
        task_id=f"WEATHER-CN-TEST-{target_date.replace('-', '')}-DAYAHEAD-001",
        region="测试城市",
        target_date=target_date,
        data_cutoff_time=f"{target_date}T09:00:00+08:00",
        provider_results=[
            ProviderForecast(provider="open_meteo", status="ok", points=provider_a),
            ProviderForecast(provider="qweather", status="ok", points=provider_b),
        ],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo", "qweather"],
            points=aggregated,
            summary=ForecastSummary(
                max_temperature=max(point.temperature for point in aggregated),
                min_temperature=min(point.temperature for point in aggregated),
                rain_probability=max(point.precipitation_probability for point in aggregated),
                wind_speed=max(point.wind_speed for point in aggregated),
                cloud_cover=sum(point.cloud_cover for point in aggregated) / len(aggregated),
                main_weather="多云",
                high_risk_period="无明显高风险时段",
                sunrise="06:00",
                sunset="19:00",
            ),
        ),
        confidence={"score": 0.7, "description": "中等"},
        key_factors=[],
        risk_notes=[],
    )


def _card_text(card: dict) -> str:
    chunks = [card["card"]["header"]["title"]["content"]]
    for element in card["card"]["elements"]:
        if isinstance(element.get("text"), dict):
            chunks.append(element["text"].get("content", ""))
        for item in element.get("elements", []):
            if isinstance(item, dict):
                chunks.append(item.get("content", ""))
    return "\n".join(chunks)


def _card_section(card: dict, heading: str) -> str:
    for element in card["card"]["elements"]:
        text = element.get("text")
        if isinstance(text, dict) and heading in text.get("content", ""):
            return text["content"]
    return ""


def test_continuous_windows_reports_ranges_instead_of_first_hit():
    points = _points("2026-07-28", hot_hours={17, 18, 19}, cloudy_hours={11, 12, 13})

    result = _continuous_windows(
        points,
        lambda point: point.temperature >= 35 or point.cloud_cover >= 85,
    )

    assert result == "11:00–14:00、17:00–20:00"


def test_briefing_2_0_prioritizes_risk_change_confidence_and_proxy_boundaries():
    rows = [
        {
            "market": "山东",
            "city": "济南",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    hot_hours={17, 18, 19, 20},
                    cloudy_hours={11, 12, 13, 14, 15},
                    wind_hours={12, 13},
                ),
            },
        },
        {
            "market": "山西",
            "city": "太原",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        },
    ]

    card = build_briefing_card(
        rows,
        "2026-07-27",
        generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
    )
    text = _card_text(card)

    assert "电力气象决策晨报 2.0" in text
    assert "Top 5 气象侧风险" in text
    assert "山东·济南代表点" in text
    assert "局地风雨复合风险" in text
    assert "另有 3 个独立风险信号" in text
    assert "负荷天气压力（角色内等权样本）：山东·济南代表点" in text
    assert "光资源转弱代理（角色内等权样本）：山东·济南代表点" in text
    assert "地面风资源代理（角色内等权样本）：山东·济南代表点" in text
    assert "变化：较今日新增风雨复合时段" in text
    assert "置信度：中等" in text
    assert "稳定分析区 1 个，精简卡未逐一列出" in text
    assert "当前为多城市代表点样本扫描" in text
    for unsupported_claim in ("强对流", "现货承压", "电价", "风电出力"):
        assert unsupported_claim not in text


def test_confidence_uses_provider_disagreement_not_only_provider_count():
    submission = _submission("2026-07-28", provider_offset=8.0)

    assert _confidence_label(submission) == "偏低（数据源分歧较大）"


def test_briefing_reports_risks_beyond_top_five_and_does_not_fold_zero_stable_markets():
    rows = [
        {
            "market": f"市场{index}",
            "city": f"城市{index}",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28", hot_hours={17, 18, 19}),
            },
        }
        for index in range(1, 8)
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
        )
    )

    assert "另有 2 个较低优先级风险分析区未在卡片展开" in text
    assert "稳定分析区 0 个" in text
    assert "稳定市场 0 个，已折叠" not in text

    detail_text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
            expanded=True,
        )
    )
    assert "市场6·城市6代表点" in detail_text
    assert "市场7·城市7代表点" in detail_text


def test_briefing_coverage_distinguishes_market_count_from_representative_points():
    rows = [
        {
            "market": "广东",
            "province": "广东",
            "city": city,
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
        for city in ("广州", "阳江")
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
        )
    )

    assert "分析区有数据 1/1（完整 1，部分 0，其中单点 0）" in text
    assert "代表点 2/2" in text


def test_national_market_config_covers_31_areas_33_zones_and_75_unique_points():
    assert {market.provincial_area for market in NATIONAL_MARKETS} == MAINLAND_PROVINCIAL_AREAS
    assert len(NATIONAL_MARKETS) == 33
    assert len(representative_points()) == 75
    assert len({market.market_id for market in NATIONAL_MARKETS}) == 33
    assert len({point.point_id for market in NATIONAL_MARKETS for point in market.points}) == 75
    assert all(len(market.points) >= 2 for market in NATIONAL_MARKETS)
    assert {
        "冀北样本区",
        "河北南网样本区",
        "蒙东样本区",
        "蒙西样本区",
    }.issubset({market.market_name for market in NATIONAL_MARKETS})


def test_partial_point_coverage_is_reported_without_marking_market_missing():
    rows = [
        {
            "market_id": "cn-44-guangdong",
            "market": "广东样本区",
            "province": "广东",
            "point_id": f"point-{index}",
            "city": city,
            "submissions": (
                {
                    "2026-07-27": _submission("2026-07-27"),
                    "2026-07-28": _submission("2026-07-28"),
                }
                if index == 1
                else {}
            ),
        }
        for index, city in enumerate(("广州", "阳江"), start=1)
    ]

    coverage = briefing_coverage(rows, "2026-07-27")
    text = _card_text(build_briefing_card(rows, "2026-07-27"))

    assert coverage["markets"] == {
        "covered": 1,
        "total": 1,
        "full": 0,
        "partial": 1,
        "single_point": 1,
        "missing": 0,
    }
    assert coverage["points"] == {"covered": 1, "total": 2, "missing": 1}
    assert "部分覆盖分析区 1 个" in text
    assert "数据完全缺失分析区" not in text


def test_single_point_partial_zone_is_local_only_and_excluded_from_zone_rankings():
    rows = [
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "hot",
            "city": "高温点",
            "roles": ["load", "solar", "wind"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    hot_hours={17, 18, 19},
                    cloudy_hours={11, 12},
                    wind_hours={20, 21},
                ),
            },
        },
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "missing",
            "city": "缺失点",
            "roles": ["load", "solar", "wind"],
            "submissions": {},
        },
    ]

    card = build_briefing_card(rows, "2026-07-27")
    text = _card_text(card)
    ranking = _card_section(card, "资源代理排行")

    assert "测试样本区·高温点代表点（1/2点，不可外推全区）" in text
    assert "测试样本区" not in ranking
    assert "样本不足" in ranking


def test_point_roles_strictly_constrain_resource_signals():
    rows = [
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "solar-only",
            "city": "光伏点",
            "roles": ["solar"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    hot_hours={17, 18, 19},
                    cloudy_hours={11, 12, 13},
                    wind_hours={20, 21},
                ),
            },
        }
    ]

    risk_section = _card_section(
        build_briefing_card(rows, "2026-07-27"),
        "Top 5 气象侧风险",
    )

    assert "光伏资源代理↓" in risk_section
    assert "负荷天气压力代理↑" not in risk_section
    assert "地面风资源代理↑" not in risk_section
    assert "11:00–14:00" in risk_section
    assert "17:00–20:00" not in risk_section
    assert "17:00–22:00" not in risk_section
    assert "20:00–22:00" not in risk_section


def test_multi_point_signals_keep_their_own_city_and_time_window():
    rows = [
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "load",
            "city": "负荷点",
            "roles": ["load"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28", hot_hours={17, 18, 19}),
            },
        },
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "solar",
            "city": "光伏点",
            "roles": ["solar"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    cloudy_hours={11, 12, 13, 14, 15},
                ),
            },
        },
    ]

    risk_section = _card_section(
        build_briefing_card(rows, "2026-07-27"),
        "Top 5 气象侧风险",
    )

    assert "负荷点代表点" in risk_section
    assert "17:00–20:00" in risk_section
    assert "光伏资源代理↓" not in risk_section
    assert "另有 1 个独立风险信号" in risk_section


def test_multi_point_market_reports_internal_resource_divergence():
    rows = [
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "clear",
            "city": "晴空点",
            "roles": ["solar"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        },
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "cloudy",
            "city": "多云点",
            "roles": ["solar"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    cloudy_hours={10, 11, 12, 13, 14, 15},
                ),
            },
        },
    ]

    text = _card_text(build_briefing_card(rows, "2026-07-27"))

    assert "光伏资源代理区内分化" in text
    assert "光伏资源代理↑ / 光伏资源代理↓" not in text


def test_expanded_card_is_an_all_zone_detail_view_not_a_false_complete_claim():
    rows = [
        {
            "market_id": "risk",
            "market": "风险样本区",
            "province": "风险地区",
            "point_id": "risk-point",
            "city": "风险点",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28", hot_hours={17, 18, 19}),
            },
        },
        {
            "market_id": "stable",
            "market": "稳定样本区",
            "province": "稳定地区",
            "point_id": "stable-point",
            "city": "稳定点",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        },
    ]

    card = build_briefing_card(rows, "2026-07-27", expanded=True)
    text = _card_text(card)
    title = card["card"]["header"]["title"]["content"]

    assert "全部明细" in title
    assert "完整市场" not in title
    assert "全部分析区明细" in text
    assert "稳定样本区·稳定点代表点" in text


def test_national_coverage_uses_fixed_denominators_and_separate_today_baseline():
    first_market = NATIONAL_MARKETS[0]
    first_point = first_market.points[0]
    rows = [
        {
            "market_id": first_market.market_id,
            "market": first_market.market_name,
            "province": first_market.provincial_area,
            "point_id": first_point.point_id,
            "city": first_point.city,
            "roles": list(first_point.roles),
            "submissions": {
                "2026-07-28": _submission("2026-07-28"),
            },
        }
    ]

    coverage = briefing_coverage(
        rows,
        "2026-07-27",
        market_config=NATIONAL_MARKETS,
    )

    assert coverage["provincial_areas"] == {"covered": 1, "total": 31}
    assert coverage["markets"]["total"] == 33
    assert coverage["points"] == {"covered": 1, "total": 75, "missing": 74}
    assert coverage["baseline_points"] == {"covered": 0, "total": 75, "missing": 75}


def test_runtime_market_config_validation_locks_counts_splits_and_roles():
    validate_market_config()
    assert all(
        {"load", "solar", "wind"}.issubset(
            {role for point in market.points for role in point.roles}
        )
        for market in NATIONAL_MARKETS
    )


def test_expanded_national_card_stays_within_30kb_payload_budget():
    rows = [
        {
            "market_id": market.market_id,
            "market": market.market_name,
            "province": market.provincial_area,
            "point_id": point.point_id,
            "city": point.city,
            "roles": list(point.roles),
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28", hot_hours={17, 18, 19}),
            },
        }
        for market in NATIONAL_MARKETS
        for point in market.points
    ]

    card = build_briefing_card(
        rows,
        "2026-07-27",
        expanded=True,
        market_config=NATIONAL_MARKETS,
    )
    payload_size = len(
        json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    assert payload_size < 30_000


def test_market_classification_counts_are_conservative():
    risk_rows = [
        {
            "market_id": f"risk-{index}",
            "market": f"风险市场{index}",
            "province": f"地区{index}",
            "city": f"城市{index}",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28", hot_hours={17, 18, 19}),
            },
        }
        for index in range(1, 9)
    ]
    stable_rows = [
        {
            "market_id": f"stable-{index}",
            "market": f"稳定市场{index}",
            "province": f"稳定地区{index}",
            "city": f"稳定城市{index}",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
        for index in range(1, 3)
    ]
    missing_row = {
        "market_id": "missing-1",
        "market": "缺失市场",
        "province": "缺失地区",
        "city": "缺失城市",
        "submissions": {},
    }

    statistics = briefing_statistics(
        [*risk_rows, *stable_rows, missing_row],
        "2026-07-27",
    )

    assert statistics == {
        "top_risks": 5,
        "remaining_risks": 3,
        "stable_markets": 2,
        "missing_markets": 1,
        "configured_markets": 11,
        "classified_markets": 11,
    }


@pytest.mark.asyncio
async def test_dry_run_generates_cache_without_sending_feishu(monkeypatch, tmp_path):
    settings = Settings(
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        feishu_app_id=None,
        feishu_app_secret=None,
    )
    snapshot = {
        "coverage": {
            "provincial_areas": {"covered": 31, "total": 31},
            "markets": {"covered": 33, "total": 33},
            "points": {"covered": 75, "total": 75},
        },
        "statistics": {"classified_markets": 33, "configured_markets": 33},
        "summary_card": {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"content": "测试晨报"}},
                "elements": [],
            },
        },
    }

    async def fake_snapshot(*args, **kwargs):
        return snapshot, False

    async def fail_send(*args, **kwargs):
        raise AssertionError("DRY_RUN must never send Feishu messages")

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fake_snapshot)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fail_send)
    monkeypatch.setenv("DRY_RUN", "1")

    await daily_power_briefing.go("send")


@pytest.mark.asyncio
async def test_scheduled_send_records_each_card_thread_pointer(monkeypatch, tmp_path):
    settings = Settings(
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        feishu_app_id=None,
        feishu_app_secret=None,
    )
    snapshot = {
        "cache_key": "2026-07-27:test:test",
        "generated_at": "2026-07-27T09:00:00+08:00",
        "coverage": {
            "provincial_areas": {"covered": 31, "total": 31},
            "markets": {"covered": 33, "total": 33},
            "points": {"covered": 75, "total": 75},
        },
        "statistics": {"classified_markets": 33, "configured_markets": 33},
        "summary_card": {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"content": "测试晨报"}},
                "elements": [],
            },
        },
    }
    sent: list[str] = []
    pointers: list[tuple[str, str, str, str | None]] = []

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def fake_send(self, chat_id, card):
        sent.append(chat_id)
        return f"message-{chat_id}"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fake_snapshot)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(
        daily_power_briefing,
        "_remember_scheduled_briefing_thread",
        lambda chat_id, message_id, cache_key, generated_at: pointers.append(
            (chat_id, message_id, cache_key, generated_at)
        ),
    )
    monkeypatch.delenv("DRY_RUN", raising=False)

    await daily_power_briefing.go("send")

    expected_chats = [chat_id for _name, chat_id in daily_power_briefing.CHAT_TARGETS]
    assert sent == expected_chats
    assert [item[0] for item in pointers] == expected_chats
    assert all(item[2] == snapshot["cache_key"] for item in pointers)
