import asyncio
from datetime import datetime
import json
import time

import pytest

from scripts import daily_power_briefing
from scripts.daily_power_briefing import _confidence_label, _continuous_windows, build_briefing_card
from services.weather_bot import main as weather_main
from services.weather_bot.briefing_cache import BriefingCache
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
    AnalysisWindow,
    MAINLAND_PROVINCIAL_AREAS,
    NATIONAL_MARKETS,
    MarketZone,
    RepresentativePoint,
    representative_points,
    validate_market_config,
)


def test_scheduled_briefing_has_no_source_code_chat_targets() -> None:
    assert not hasattr(daily_power_briefing, "CHAT_TARGETS")


def test_scheduled_briefing_targets_fail_closed_on_invalid_configuration() -> None:
    settings = Settings(power_briefing_targets_json="not-json")
    assert daily_power_briefing._approved_chat_targets(settings) == []


def test_scheduled_briefing_targets_fail_closed_on_duplicate_chat_id() -> None:
    settings = Settings(
        power_briefing_targets_json=json.dumps(
            [
                {"name": "晨报群 A", "chat_id": "oc_duplicate"},
                {"name": "晨报群 B", "chat_id": "oc_duplicate"},
            ],
            ensure_ascii=False,
        )
    )

    assert daily_power_briefing._approved_chat_targets(settings) == []


def test_scheduled_briefing_targets_fail_closed_on_any_invalid_member() -> None:
    settings = Settings(
        power_briefing_targets_json=json.dumps(
            [
                {"name": "已审核晨报群", "chat_id": "oc_reviewed"},
                {"name": "缺少目标 ID"},
            ],
            ensure_ascii=False,
        )
    )

    assert daily_power_briefing._approved_chat_targets(settings) == []


def _scheduled_snapshot(
    *,
    report_date: str = "2026-08-09",
    release_slot: str = "09:00",
    run_id: str = "briefing-run-20260809-0850",
) -> dict:
    cache_key = daily_power_briefing.briefing_cache_key(
        report_date,
        release_slot=release_slot,
    )
    base_cache_key = cache_key.rsplit(":release-", 1)[0]
    _, market_config_version, report_version = base_cache_key.rsplit(":", 2)
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": "scheduled briefing"}},
            "elements": [],
        },
    }
    return {
        "schema_version": 2,
        "cache_key": cache_key,
        "report_date": report_date,
        "market_config_version": market_config_version,
        "report_version": report_version,
        "generated_at": f"{report_date}T08:50:00+08:00",
        "expires_at": f"{report_date}T09:50:00+08:00",
        "forecast_run_id": run_id,
        "release_slot": release_slot,
        "retrieved_at": f"{report_date}T08:49:00+08:00",
        "provider_issued_at": {"open_meteo": None},
        "valid_time": {
            "start": f"{report_date}T00:00:00+08:00",
            "end": f"{report_date}T23:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "sources": ["open_meteo"],
        "source_forecast_run_ids": ["source-run-1"],
        "provider_run_metadata": [
            {
                "provider": "open_meteo",
                "statuses": ["ok"],
                "retrieved_at": f"{report_date}T08:49:00+08:00",
                "provider_issued_at": None,
                "source_urls": ["https://api.open-meteo.com/v1/forecast"],
                "content_sha256s": ["a" * 64],
                "retention_policy": "derived_only",
                "record_coverage": {
                    "ok": 1,
                    "source_url": 1,
                    "content_sha256": 1,
                },
            }
        ],
        "quality": {
            "status": "degraded",
            "reasons": ["provider_issued_at_missing:open_meteo"],
        },
        "metric_coverage": {},
        "confidence": {"level": "medium"},
        "previous_run_id": None,
        "version_change": {
            "status": "unavailable",
            "reason": "no_previous_same_release",
        },
        "coverage": {
            "provincial_areas": {"covered": 31, "total": 31},
            "markets": {"covered": 33, "total": 33},
            "points": {"covered": 75, "total": 75},
            "baseline_points": {"covered": 75, "total": 75},
        },
        "statistics": {"classified_markets": 33, "configured_markets": 33},
        "market_risk_snapshots": [],
        "window_assessment_snapshots": [],
        "window_version_change": {
            "status": "available",
            "reason": "same_target_window_lifecycle",
            "previous_run_id": None,
            "counts": {},
            "items": [],
        },
        "summary_card": card,
        "detail_card": card,
    }


class _ScheduledDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 9, 9, 0)
        return value.replace(tzinfo=tz) if tz is not None else value


class _AfternoonScheduledDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 9, 15, 0)
        return value.replace(tzinfo=tz) if tz is not None else value


def _seed_scheduled_snapshot(db_path: str) -> dict:
    snapshot = _scheduled_snapshot()
    cache = BriefingCache(db_path, ttl_seconds=3600)
    assert cache.claim_generation(snapshot["cache_key"], "seed")
    cache.save_and_release(
        snapshot["cache_key"],
        "seed",
        snapshot,
        generator_version=snapshot["report_version"],
    )
    return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache_state",
    ("missing", "expired", "release_slot_mismatch"),
)
async def test_scheduled_send_requires_the_fresh_0850_precomputed_snapshot(
    monkeypatch,
    tmp_path,
    capsys,
    cache_state,
):
    db_path = str(tmp_path / "briefing.db")
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=db_path,
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "reviewed briefing group", "chat_id": "oc_reviewed"}],
        ),
    )
    if cache_state != "missing":
        snapshot = _scheduled_snapshot(
            release_slot="08:00" if cache_state == "release_slot_mismatch" else "09:00"
        )
        seed = BriefingCache(
            db_path,
            ttl_seconds=1 if cache_state == "expired" else 3600,
        )
        assert seed.claim_generation(snapshot["cache_key"], "seed")
        seed.save_and_release(
            snapshot["cache_key"],
            "seed",
            snapshot,
            generator_version=snapshot["report_version"],
            generated_at=time.time() - (10 if cache_state == "expired" else 0),
        )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 9, 9, 0)
            return value.replace(tzinfo=tz) if tz is not None else value

    def fail_forecast_service(*args, **kwargs):
        raise AssertionError("send mode must not construct ForecastService")

    async def fail_generation(*args, **kwargs):
        raise AssertionError("send mode must not generate a briefing")

    async def fail_send(*args, **kwargs):
        raise AssertionError("an unusable precomputed snapshot must not reach Feishu")

    def fail_claim(*args, **kwargs):
        raise AssertionError("an unusable precomputed snapshot must not claim the ledger")

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", fail_forecast_service)
    monkeypatch.setattr(daily_power_briefing, "datetime", FixedDateTime)
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fail_generation)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fail_send)
    monkeypatch.setattr(BriefingCache, "claim_scheduled_delivery", fail_claim)
    monkeypatch.delenv("DRY_RUN", raising=False)

    result = await daily_power_briefing.go("send")

    assert result == "precompute_snapshot_missing"
    assert "reason=precompute_snapshot_missing" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_scheduled_send_publishes_one_exact_precomputed_snapshot_with_a_stable_uuid(
    monkeypatch,
    tmp_path,
):
    db_path = str(tmp_path / "briefing.db")
    snapshot = _scheduled_snapshot()
    cache = BriefingCache(db_path, ttl_seconds=3600)
    assert cache.claim_generation(snapshot["cache_key"], "seed")
    cache.save_and_release(
        snapshot["cache_key"],
        "seed",
        snapshot,
        generator_version=snapshot["report_version"],
    )
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=db_path,
        power_briefing_cache_ttl_seconds=3600,
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "reviewed briefing group", "chat_id": "oc_reviewed"}],
        ),
    )
    sends: list[tuple[str, dict, str | None]] = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 9, 9, 0)
            return value.replace(tzinfo=tz) if tz is not None else value

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sends.append((chat_id, card, idempotency_key))
        return "message-1"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(daily_power_briefing, "datetime", FixedDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(
        daily_power_briefing,
        "_remember_scheduled_briefing_thread",
        lambda *args: None,
    )
    monkeypatch.delenv("DRY_RUN", raising=False)

    await daily_power_briefing.go("send")
    await daily_power_briefing.go("send")

    assert sends == [
        (
            "oc_reviewed",
            snapshot["summary_card"],
            "c792d9da-6452-5859-9056-35e4a782a9ff",
        )
    ]


@pytest.mark.asyncio
async def test_afternoon_precompute_requires_the_same_day_0900_baseline_before_fetching(
    monkeypatch,
    tmp_path,
    capsys,
):
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
    )

    def fail_forecast_service(*args, **kwargs):
        raise AssertionError("missing 09:00 baseline must stop before weather fetch setup")

    async def fail_generation(*args, **kwargs):
        raise AssertionError("missing 09:00 baseline must not generate 15:00 snapshot")

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", fail_forecast_service)
    monkeypatch.setattr(daily_power_briefing, "datetime", _AfternoonScheduledDateTime)
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fail_generation)

    result = await daily_power_briefing.go("afternoon_precompute")

    assert result == "morning_baseline_missing"
    assert "reason=morning_baseline_missing" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_afternoon_send_is_silent_when_precomputed_snapshot_has_no_material_change(
    monkeypatch,
    tmp_path,
):
    db_path = str(tmp_path / "briefing.db")
    snapshot = _scheduled_snapshot(
        release_slot="15:00",
        run_id="briefing-run-20260809-1450",
    )
    snapshot["afternoon_send_required"] = False
    snapshot["afternoon_delta_card"] = None
    cache = BriefingCache(db_path, ttl_seconds=3600)
    assert cache.claim_generation(snapshot["cache_key"], "seed")
    cache.save_and_release(
        snapshot["cache_key"],
        "seed",
        snapshot,
        generator_version=snapshot["report_version"],
    )
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=db_path,
        global_feishu_send_enabled=True,
        power_briefing_afternoon_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "reviewed briefing group", "chat_id": "oc_reviewed"}],
        ),
    )

    async def fail_send(*args, **kwargs):
        raise AssertionError("no material change must remain silent")

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(daily_power_briefing, "datetime", _AfternoonScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fail_send)
    monkeypatch.delenv("DRY_RUN", raising=False)

    assert await daily_power_briefing.go("afternoon_send") == "no_material_change"


@pytest.mark.asyncio
async def test_afternoon_send_uses_its_own_gate_and_only_sends_the_delta_card(
    monkeypatch,
    tmp_path,
):
    db_path = str(tmp_path / "briefing.db")
    snapshot = _scheduled_snapshot(
        release_slot="15:00",
        run_id="briefing-run-20260809-1450",
    )
    delta_card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": "15:00 delta"}},
            "elements": [],
        },
    }
    snapshot["afternoon_send_required"] = True
    snapshot["afternoon_delta_card"] = delta_card
    cache = BriefingCache(db_path, ttl_seconds=3600)
    assert cache.claim_generation(snapshot["cache_key"], "seed")
    cache.save_and_release(
        snapshot["cache_key"],
        "seed",
        snapshot,
        generator_version=snapshot["report_version"],
    )
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=db_path,
        global_feishu_send_enabled=True,
        power_briefing_allow_send=False,
        power_briefing_afternoon_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "reviewed briefing group", "chat_id": "oc_reviewed"}],
        ),
    )
    sends: list[dict] = []

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sends.append(card)
        return "message-afternoon"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(daily_power_briefing, "datetime", _AfternoonScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(
        daily_power_briefing,
        "_remember_scheduled_briefing_thread",
        lambda *args: None,
    )
    monkeypatch.delenv("DRY_RUN", raising=False)

    await daily_power_briefing.go("afternoon_send")

    assert sends == [delta_card]


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


def test_0900_briefing_states_exact_remaining_horizon_and_checks_every_tomorrow_window():
    rows = [
        {
            "market_id": "cn-test",
            "market": "测试分析区",
            "province": "测试地区",
            "point_id": "test-main",
            "city": "测试城市",
            "roles": ["hydrology"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
        )
    )

    assert "分析范围：今日09:00–24:00 + 明日00:00–24:00" in text
    assert "范围 今日+明日" not in text
    assert "今日00:00–06:00｜凌晨｜已经过去，不作为未来预报" in text
    assert "今日09:00–10:00｜早峰剩余｜已检查，暂无需要重点跟踪的信号" in text
    for expected in (
        "明日00:00–06:00｜凌晨｜已检查，暂无需要重点跟踪的信号",
        "明日06:00–10:00｜早峰｜已检查，暂无需要重点跟踪的信号",
        "明日10:00–16:00｜午间光伏｜已检查，暂无需要重点跟踪的信号",
        "明日16:00–17:00｜下午过渡｜已检查，暂无需要重点跟踪的信号",
        "明日17:00–21:00｜晚峰｜已检查，暂无需要重点跟踪的信号",
        "明日21:00–24:00｜夜间｜已检查，暂无需要重点跟踪的信号",
    ):
        assert expected in text


def test_0900_briefing_names_the_actual_today_and_tomorrow_windows_that_need_attention():
    rows = [
        {
            "market_id": "cn-44-guangdong",
            "market": "广东样本区",
            "province": "广东",
            "point_id": "guangdong-guangzhou",
            "city": "广州",
            "roles": ["solar"],
            "submissions": {
                "2026-07-27": _submission(
                    "2026-07-27",
                    cloudy_hours={11, 12, 13, 14},
                ),
                "2026-07-28": _submission("2026-07-28"),
            },
        },
        {
            "market_id": "cn-32-jiangsu",
            "market": "江苏样本区",
            "province": "江苏",
            "point_id": "jiangsu-yancheng",
            "city": "盐城",
            "roles": ["wind"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    cloudy_hours={7, 8, 9},
                    wind_hours={7, 8, 9},
                ),
            },
        },
    ]

    card = build_briefing_card(
        rows,
        "2026-07-27",
        generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
    )
    text = _card_text(card)
    focus = _card_section(card, "今天先看哪三件事")

    assert "广东样本区·广州代表点｜今日10:00–16:00｜午间光伏" in focus
    assert "光资源代理转弱" in focus
    assert "建议核对：新能源功率预测和场站运行信息" in focus
    assert "江苏样本区·盐城代表点｜明日06:00–10:00｜早峰" in focus
    assert "局地风雨复合天气风险" in focus
    assert "今日17:00–21:00｜晚峰｜已检查，暂无需要重点跟踪的信号" in text


def test_window_check_distinguishes_checked_stable_areas_from_areas_with_missing_data():
    incomplete_tomorrow = _submission("2026-07-28")
    incomplete_tomorrow.aggregated_forecast.points = [
        point
        for point in incomplete_tomorrow.aggregated_forecast.points
        if int(str(point.time)[11:13]) < 21
    ]
    rows = [
        {
            "market_id": "cn-complete",
            "market": "完整分析区",
            "province": "完整地区",
            "point_id": "complete-main",
            "city": "完整城市",
            "roles": ["hydrology"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        },
        {
            "market_id": "cn-missing-night",
            "market": "夜间缺失分析区",
            "province": "缺失地区",
            "point_id": "missing-night-main",
            "city": "缺失城市",
            "roles": ["hydrology"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": incomplete_tomorrow,
            },
        },
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
        )
    )

    assert (
        "明日21:00–24:00｜夜间｜已检查，暂无需要重点跟踪的信号；"
        "另1个分析区数据不足"
    ) in text


def test_elapsed_today_weather_is_not_presented_as_future_or_as_unverified_observation():
    rows = [
        {
            "market_id": "cn-elapsed",
            "market": "已过时段分析区",
            "province": "测试地区",
            "point_id": "elapsed-main",
            "city": "测试城市",
            "roles": ["wind"],
            "submissions": {
                "2026-07-27": _submission(
                    "2026-07-27",
                    cloudy_hours={1, 2, 3},
                    wind_hours={1, 2, 3},
                ),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
    ]

    card = build_briefing_card(
        rows,
        "2026-07-27",
        generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
    )
    text = _card_text(card)
    focus = _card_section(card, "今天先看哪三件事")

    assert "今日早间回顾：未接入经过可用性门禁的实况数据" in text
    assert "已过时段不作为实况展示" in text
    assert "01:00–04:00" not in focus


def test_verified_morning_observation_is_shown_as_observation_not_as_forecast():
    rows = [
        {
            "market_id": "cn-44-guangdong",
            "market": "广东样本区",
            "province": "广东",
            "point_id": "guangzhou",
            "city": "广州",
            "roles": ["load"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
    ]
    observations = [
        {
            "availability_status": "allowed_for_calculation",
            "market": "广东样本区",
            "representative_point": "广州",
            "metric": "apparent_temperature",
            "value": 36.2,
            "unit": "℃",
            "valid_time": {
                "start": "2026-07-27T06:00:00+08:00",
                "end": "2026-07-27T09:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
            "retrieved_at": "2026-07-27T09:01:00+08:00",
            "source_url": "https://example.gov.cn/weather/observation/guangzhou",
            "provenance_ref": "official-observation:guangzhou:20260727-am",
        }
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:05:00+08:00"),
            verified_observations=observations,
        )
    )

    assert "今日早间回顾（已核验实况）" in text
    assert "广东样本区·广州代表点｜06:00–09:00｜实况体感温度 36.2℃" in text
    assert "未接入经过可用性门禁的实况数据" not in text


def test_unverified_morning_observation_is_never_presented_as_actual_weather():
    rows = [
        {
            "market_id": "cn-44-guangdong",
            "market": "广东样本区",
            "province": "广东",
            "point_id": "guangzhou",
            "city": "广州",
            "roles": ["load"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:05:00+08:00"),
            verified_observations=[
                {
                    "availability_status": "text_only",
                    "market": "广东样本区",
                    "representative_point": "广州",
                    "metric": "apparent_temperature",
                    "value": 99,
                    "unit": "℃",
                }
            ],
        )
    )

    assert "实况体感温度 99" not in text
    assert "今日早间回顾：未接入经过可用性门禁的实况数据" in text


def test_naive_observation_timestamps_are_not_treated_as_verified_actuals():
    text = _card_text(
        build_briefing_card(
            [],
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:05:00+08:00"),
            market_config=(),
            verified_observations=[
                {
                    "availability_status": "allowed_for_calculation",
                    "market": "广东样本区",
                    "representative_point": "广州",
                    "metric": "temperature",
                    "value": 31,
                    "unit": "℃",
                    "valid_time": {
                        "start": "2026-07-27T06:00:00",
                        "end": "2026-07-27T09:00:00",
                        "timezone": "Asia/Shanghai",
                    },
                    "retrieved_at": "2026-07-27T09:01:00",
                    "source_url": "https://example.gov.cn/weather/observation/guangzhou",
                    "provenance_ref": "official-observation:guangzhou:20260727-am",
                }
            ],
        )
    )

    assert "实况气温 31" not in text
    assert "未接入经过可用性门禁的实况数据" in text


def test_briefing_uses_analysis_area_window_configuration_instead_of_fixed_national_hours():
    market_config = (
        MarketZone(
            market_id="cn-custom",
            market_name="自定义分析区",
            provincial_area="测试地区",
            provincial_code="99",
            points=(
                RepresentativePoint(
                    point_id="custom-main",
                    city="测试城市",
                    query="测试城市",
                    roles=("hydrology",),
                ),
            ),
            analysis_windows=(
                AnalysisWindow("custom_overnight", "本区凌晨", 0, 5),
                AnalysisWindow("custom_early_peak", "本区早峰", 5, 9),
                AnalysisWindow("custom_remaining", "本区其余时段", 9, 24),
            ),
        ),
    )
    rows = [
        {
            "market_id": "cn-custom",
            "market": "自定义分析区",
            "province": "测试地区",
            "point_id": "custom-main",
            "city": "测试城市",
            "roles": ["hydrology"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28"),
            },
        }
    ]

    text = _card_text(
        build_briefing_card(
            rows,
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
            market_config=market_config,
        )
    )

    assert "明日05:00–09:00｜本区早峰｜已检查" in text
    assert "明日06:00–10:00｜早峰" not in text


def test_briefing_rejects_analysis_area_windows_with_an_unchecked_time_gap():
    market_config = (
        MarketZone(
            market_id="cn-gap",
            market_name="时间缺口分析区",
            provincial_area="测试地区",
            provincial_code="99",
            points=(
                RepresentativePoint(
                    point_id="gap-main",
                    city="测试城市",
                    query="测试城市",
                    roles=("hydrology",),
                ),
            ),
            analysis_windows=(
                AnalysisWindow("overnight", "凌晨", 0, 6),
                AnalysisWindow("daytime", "白天", 7, 24),
            ),
        ),
    )

    with pytest.raises(ValueError, match="must cover 00:00-24:00 without gaps"):
        build_briefing_card(
            [],
            "2026-07-27",
            generated_at=datetime.fromisoformat("2026-07-27T09:00:00+08:00"),
            market_config=market_config,
        )


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
    assert "负荷天气压力（同类代表点等权汇总）：山东·济南代表点" in text
    assert "光资源转弱代理（同类代表点等权汇总）：山东·济南代表点" in text
    assert "地面风资源代理（同类代表点等权汇总）：山东·济南代表点" in text
    assert "变化：较今日新增风雨复合时段" in text
    assert "置信度：中等" in text
    assert "稳定分析区 1 个，精简卡未逐一列出" in text
    assert "当前为多城市代表点扫描" in text
    for unsupported_claim in ("强对流", "现货承压", "电价", "风电出力"):
        assert unsupported_claim not in text


def test_confidence_uses_provider_disagreement_not_only_provider_count():
    submission = _submission("2026-07-28", provider_offset=8.0)

    assert _confidence_label(submission) == "偏低（数据源分歧较大）"


def test_briefing_reports_risks_beyond_top_five_and_omits_zero_stable_count():
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
    assert "稳定分析区 0 个" not in text
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


def test_same_severity_top_five_uses_strength_then_duration_before_input_order():
    rows = [
        {
            "market": "较弱同级市场",
            "city": "较弱点",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission("2026-07-28", hot_hours={17, 18}),
            },
        },
        {
            "market": "较强同级市场",
            "city": "较强点",
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    hot_hours={17, 18, 19, 20, 21, 22},
                ),
            },
        },
    ]

    card = build_briefing_card(rows, "2026-07-27")
    top_five = _card_section(card, "Top 5 气象侧风险")

    assert top_five.index("较强同级市场") < top_five.index("较弱同级市场")


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

    assert (
        "分析区有数据 1/1（所有代表城市均有数据 1，部分代表城市缺数据 0，"
        "仅1个代表城市有数据 0）"
    ) in text
    assert "明日代表城市 2/2" in text


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
    assert "代表城市数据不齐的分析区 1 个；只按已有城市展示，不外推整个分析区" in text
    assert "部分覆盖" not in text
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
        {
            "market_id": "cn-test",
            "market": "测试样本区",
            "province": "测试地区",
            "point_id": "also-missing",
            "city": "另一缺失点",
            "roles": ["load", "solar", "wind"],
            "submissions": {},
        },
    ]

    card = build_briefing_card(rows, "2026-07-27")
    text = _card_text(card)
    ranking = _card_section(card, "资源代理排行")

    assert "测试样本区·高温点代表点｜用于分析的3个城市中，仅1个有数据；不能代表全区" in text
    assert "代表点数据 1/3" not in text
    assert "测试样本区" not in ranking
    assert "代表点不足" in ranking


def test_full_representative_point_coverage_uses_plain_language_not_sample_jargon():
    rows = [
        {
            "market_id": "cn-test-full",
            "market": "测试分析区",
            "province": "测试地区",
            "point_id": f"point-{index}",
            "city": city,
            "roles": ["load", "solar", "wind"],
            "submissions": {
                "2026-07-27": _submission("2026-07-27"),
                "2026-07-28": _submission(
                    "2026-07-28",
                    hot_hours={17, 18, 19},
                ),
            },
        }
        for index, city in enumerate(("代表点甲", "代表点乙"), start=1)
    ]

    text = _card_text(build_briefing_card(rows, "2026-07-27"))

    assert "用于分析的2个城市都有数据" in text
    assert "完整样本" not in text


def test_partial_representative_point_coverage_explains_missing_city_in_plain_language():
    rows = [
        {
            "market_id": "cn-test-partial",
            "market": "测试分析区",
            "province": "测试地区",
            "point_id": f"point-{index}",
            "city": city,
            "roles": ["load", "solar", "wind"],
            "submissions": (
                {
                    "2026-07-27": _submission("2026-07-27"),
                    "2026-07-28": _submission(
                        "2026-07-28",
                        hot_hours={17, 18, 19},
                    ),
                }
                if index < 3
                else {}
            ),
        }
        for index, city in enumerate(("参考城市甲", "参考城市乙", "参考城市丙"), start=1)
    ]

    text = _card_text(build_briefing_card(rows, "2026-07-27"))

    assert "用于分析的3个城市中，2个有数据；结论可能不完整" in text
    assert "代表点数据 2/3" not in text


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
async def test_dry_run_send_stops_before_forecast_cache_ledger_or_feishu(monkeypatch, tmp_path):
    settings = Settings(
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "reviewed briefing group", "chat_id": "oc_reviewed"}],
        ),
    )
    async def fail_generation(*args, **kwargs):
        raise AssertionError("DRY_RUN send must not generate a briefing")

    async def fail_send(*args, **kwargs):
        raise AssertionError("DRY_RUN must never send Feishu messages")

    def fail_forecast_service(*args, **kwargs):
        raise AssertionError("DRY_RUN send must not construct ForecastService")

    def fail_cache(*args, **kwargs):
        raise AssertionError("DRY_RUN send must not open the briefing cache")

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", fail_forecast_service)
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fail_generation)
    monkeypatch.setattr(daily_power_briefing, "BriefingCache", fail_cache)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fail_send)
    monkeypatch.setenv("DRY_RUN", "1")

    assert await daily_power_briefing.go("send") == "dry_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings_overrides", "expected_reason"),
    (
        (
            {
                "global_feishu_send_enabled": False,
                "power_briefing_allow_send": True,
                "power_briefing_targets_json": json.dumps(
                    [{"name": "reviewed", "chat_id": "oc_reviewed"}]
                ),
            },
            "global_send_disabled",
        ),
        (
            {
                "global_feishu_send_enabled": True,
                "power_briefing_allow_send": False,
                "power_briefing_targets_json": json.dumps(
                    [{"name": "reviewed", "chat_id": "oc_reviewed"}]
                ),
            },
            "scheduled_send_disabled",
        ),
        (
            {
                "global_feishu_send_enabled": True,
                "power_briefing_allow_send": True,
                "power_briefing_targets_json": "[]",
            },
            "target_not_configured",
        ),
    ),
)
async def test_scheduled_send_gates_run_before_forecast_or_cache(
    monkeypatch,
    tmp_path,
    settings_overrides,
    expected_reason,
):
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        **settings_overrides,
    )

    def fail_boundary(*args, **kwargs):
        raise AssertionError("a denied scheduled send must stop before forecast and cache")

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", fail_boundary)
    monkeypatch.setattr(daily_power_briefing, "BriefingCache", fail_boundary)
    monkeypatch.delenv("DRY_RUN", raising=False)

    assert await daily_power_briefing.go("send") == expected_reason


@pytest.mark.asyncio
async def test_scheduled_entry_generates_the_shared_snapshot_for_the_0900_release_slot(
    monkeypatch,
    tmp_path,
):
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
    )
    snapshot = {
        "cache_key": "2026-08-09:test:test",
        "report_date": "2026-08-09",
        "release_slot": "09:00",
        "generated_at": "2026-08-09T08:50:00+08:00",
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
    requested_slots: list[str | None] = []

    async def fake_snapshot(*args, **kwargs):
        requested_slots.append(kwargs.get("release_slot"))
        return snapshot, False

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fake_snapshot)

    await daily_power_briefing.go("precompute")

    assert requested_slots == ["09:00"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_mode", [None, "send"])
async def test_briefing_requires_explicit_send_opt_in(
    monkeypatch,
    tmp_path,
    requested_mode,
):
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

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sent.append(chat_id)
        return f"message-{chat_id}"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fake_snapshot)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("POWER_BRIEFING_MODE", raising=False)
    monkeypatch.delenv("POWER_BRIEFING_ALLOW_SEND", raising=False)

    await daily_power_briefing.go(requested_mode)

    assert sent == []


@pytest.mark.asyncio
async def test_scheduled_send_records_each_card_thread_pointer(monkeypatch, tmp_path):
    approved_targets = [
        {"name": "已审核晨报群", "chat_id": "oc_approved_briefing"},
    ]
    settings = Settings(
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        feishu_app_id=None,
        feishu_app_secret=None,
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(approved_targets, ensure_ascii=False),
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
    snapshot = _seed_scheduled_snapshot(settings.power_briefing_cache_db)
    sent: list[str] = []
    pointers: list[tuple[str, str, str, str | None]] = []

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sent.append(chat_id)
        return f"message-{chat_id}"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "datetime", _ScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(
        daily_power_briefing,
        "_remember_scheduled_briefing_thread",
        lambda chat_id, message_id, cache_key, generated_at: pointers.append(
            (chat_id, message_id, cache_key, generated_at)
        ),
    )
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("POWER_BRIEFING_ALLOW_SEND", "1")

    await daily_power_briefing.go("send")

    expected_chats = [item["chat_id"] for item in approved_targets]
    assert sent == expected_chats
    assert [item[0] for item in pointers] == expected_chats
    assert all(item[2] == snapshot["cache_key"] for item in pointers)


@pytest.mark.asyncio
async def test_scheduled_send_obeys_global_kill_switch_even_with_local_opt_in(
    monkeypatch,
    tmp_path,
):
    settings = Settings(
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        global_feishu_send_enabled=False,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "已审核晨报群", "chat_id": "oc_approved_briefing"}],
            ensure_ascii=False,
        ),
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

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sent.append(chat_id)
        return f"message-{chat_id}"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "get_or_generate_briefing", fake_snapshot)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("POWER_BRIEFING_ALLOW_SEND", "1")

    await daily_power_briefing.go("send")

    assert sent == []


@pytest.mark.asyncio
async def test_scheduled_briefing_sends_each_release_target_at_most_once_across_runs(
    monkeypatch,
    tmp_path,
):
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "已审核晨报群", "chat_id": "oc_approved_briefing"}],
            ensure_ascii=False,
        ),
    )
    snapshot = {
        "cache_key": "2026-08-09:test:test",
        "report_date": "2026-08-09",
        "release_slot": "09:00",
        "generated_at": "2026-08-09T08:50:00+08:00",
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
    snapshot = _seed_scheduled_snapshot(settings.power_briefing_cache_db)
    sends: list[tuple[str, str | None]] = []

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sends.append((chat_id, idempotency_key))
        return "message-1"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "datetime", _ScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(daily_power_briefing, "_remember_scheduled_briefing_thread", lambda *args: None)
    monkeypatch.delenv("DRY_RUN", raising=False)

    await daily_power_briefing.go("send")
    await daily_power_briefing.go("send")

    assert len(sends) == 1
    assert sends[0][0] == "oc_approved_briefing"
    assert sends[0][1]


@pytest.mark.asyncio
async def test_failed_scheduled_briefing_delivery_can_retry_with_the_same_idempotency_key(
    monkeypatch,
    tmp_path,
):
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "已审核晨报群", "chat_id": "oc_approved_briefing"}],
            ensure_ascii=False,
        ),
    )
    snapshot = {
        "cache_key": "2026-08-09:test:test",
        "report_date": "2026-08-09",
        "release_slot": "09:00",
        "generated_at": "2026-08-09T08:50:00+08:00",
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
    snapshot = _seed_scheduled_snapshot(settings.power_briefing_cache_db)
    attempts: list[str | None] = []

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def flaky_send(self, chat_id, card, *, idempotency_key=None):
        attempts.append(idempotency_key)
        if len(attempts) == 1:
            raise RuntimeError("simulated boundary failure")
        return "message-after-retry"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "datetime", _ScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", flaky_send)
    monkeypatch.setattr(daily_power_briefing, "_remember_scheduled_briefing_thread", lambda *args: None)
    monkeypatch.delenv("DRY_RUN", raising=False)

    await daily_power_briefing.go("send")
    await daily_power_briefing.go("send")

    assert len(attempts) == 2
    assert attempts[0]
    assert attempts[1] == attempts[0]


@pytest.mark.asyncio
async def test_concurrent_scheduled_briefing_runs_send_one_card_per_release_target(
    monkeypatch,
    tmp_path,
):
    settings = Settings(
        _env_file=None,
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [{"name": "已审核晨报群", "chat_id": "oc_approved_briefing"}],
            ensure_ascii=False,
        ),
    )
    snapshot = {
        "cache_key": "2026-08-09:test:test",
        "report_date": "2026-08-09",
        "release_slot": "09:00",
        "generated_at": "2026-08-09T08:50:00+08:00",
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
    snapshot = _seed_scheduled_snapshot(settings.power_briefing_cache_db)
    send_started = asyncio.Event()
    finish_send = asyncio.Event()
    sends: list[str | None] = []

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def blocking_send(self, chat_id, card, *, idempotency_key=None):
        sends.append(idempotency_key)
        send_started.set()
        await finish_send.wait()
        return "message-1"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings)
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "datetime", _ScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", blocking_send)
    monkeypatch.setattr(daily_power_briefing, "_remember_scheduled_briefing_thread", lambda *args: None)
    monkeypatch.delenv("DRY_RUN", raising=False)

    first = asyncio.create_task(daily_power_briefing.go("send"))
    await send_started.wait()
    second = asyncio.create_task(daily_power_briefing.go("send"))
    await asyncio.sleep(0)
    finish_send.set()
    await asyncio.gather(first, second)

    assert len(sends) == 1
    assert sends[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_gate", ["schedule_disabled", "dry_run"])
async def test_suppressed_scheduled_run_does_not_consume_the_release_target_key(
    monkeypatch,
    tmp_path,
    initial_gate,
):
    common = {
        "_env_file": None,
        "power_briefing_cache_db": str(tmp_path / "briefing.db"),
        "global_feishu_send_enabled": True,
        "power_briefing_targets_json": json.dumps(
            [{"name": "已审核晨报群", "chat_id": "oc_approved_briefing"}],
            ensure_ascii=False,
        ),
    }
    settings_ref = {
        "current": Settings(
            **common,
            power_briefing_allow_send=initial_gate != "schedule_disabled",
            dry_run=initial_gate == "dry_run",
        )
    }
    snapshot = {
        "cache_key": "2026-08-09:test:test",
        "report_date": "2026-08-09",
        "release_slot": "09:00",
        "generated_at": "2026-08-09T08:50:00+08:00",
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
    snapshot = _seed_scheduled_snapshot(common["power_briefing_cache_db"])
    sends: list[str] = []

    async def fake_snapshot(*args, **kwargs):
        return snapshot, True

    async def fake_send(self, chat_id, card, *, idempotency_key=None):
        sends.append(chat_id)
        return "message-1"

    monkeypatch.setattr(weather_main, "Settings", lambda: settings_ref["current"])
    monkeypatch.setattr(weather_main, "ForecastService", lambda settings: object())
    monkeypatch.setattr(daily_power_briefing, "datetime", _ScheduledDateTime)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(daily_power_briefing, "_remember_scheduled_briefing_thread", lambda *args: None)
    monkeypatch.delenv("DRY_RUN", raising=False)

    await daily_power_briefing.go("send")
    settings_ref["current"] = Settings(
        **common,
        power_briefing_allow_send=True,
        dry_run=False,
    )
    await daily_power_briefing.go("send")

    assert sends == ["oc_approved_briefing"]
