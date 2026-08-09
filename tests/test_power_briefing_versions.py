from __future__ import annotations

from datetime import datetime

import pytest

from services.weather_bot.briefing_cache import BriefingCache
from services.weather_bot import power_briefing
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ForecastWindow,
    ProviderForecast,
    TimeInfo,
    WeatherSubmission,
)
from services.weather_bot.power_briefing_markets import MarketZone, RepresentativePoint


def _card(title: str = "测试晨报 3.0") -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": title}},
            "elements": [],
        },
    }


def _snapshot(
    *,
    run_id: str,
    report_date: str = "2026-08-10",
    release_slot: str = "09:00",
) -> dict:
    return {
        "schema_version": 2,
        "cache_key": f"{report_date}:market-v1:power-briefing-3.0",
        "report_date": report_date,
        "market_config_version": "market-v1",
        "report_version": "power-briefing-3.0",
        "generated_at": f"{report_date}T09:01:00+08:00",
        "expires_at": f"{report_date}T10:01:00+08:00",
        "forecast_run_id": run_id,
        "release_slot": release_slot,
        "retrieved_at": f"{report_date}T08:58:00+08:00",
        "provider_issued_at": {"open_meteo": None},
        "valid_time": {
            "start": f"{report_date}T00:00:00+08:00",
            "end": "2026-08-11T23:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "sources": ["open_meteo"],
        "source_forecast_run_ids": ["source-run-1"],
        "provider_run_metadata": [
            {
                "provider": "open_meteo",
                "statuses": ["ok"],
                "retrieved_at": f"{report_date}T08:58:00+08:00",
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
            "point_coverage": 1.0,
            "baseline_coverage": 1.0,
        },
        "metric_coverage": {
            "weather_points": {"covered": 75, "total": 75, "ratio": 1.0},
            "today_baseline_points": {"covered": 75, "total": 75, "ratio": 1.0},
            "provider_issued_at": {"covered": 0, "total": 1, "ratio": 0.0},
        },
        "confidence": {"level": "中等", "basis": "coverage_and_provenance"},
        "previous_run_id": None,
        "version_change": {
            "status": "unavailable",
            "reason": "no_previous_same_release",
            "previous_run_id": None,
        },
        "coverage": {
            "provincial_areas": {"covered": 31, "total": 31},
            "markets": {"covered": 33, "total": 33},
            "points": {"covered": 75, "total": 75},
            "baseline_points": {"covered": 75, "total": 75},
        },
        "statistics": {"configured_markets": 33, "classified_markets": 33},
        "market_risk_snapshots": [],
        "summary_card": _card(),
        "detail_card": _card(),
    }


def _submission(
    target_date: str,
    *,
    hot: bool = False,
    retrieved_at: str,
    source_run_id: str,
) -> WeatherSubmission:
    points = [
        ForecastPoint(
            time=f"{target_date}T{hour:02d}:00:00+08:00",
            temperature=37.0 if hot and 17 <= hour <= 20 else 25.0,
            apparent_temperature=39.0 if hot and 17 <= hour <= 20 else 26.0,
            precipitation_probability=10.0,
            cloud_cover=20.0,
            wind_speed=4.0,
        )
        for hour in range(24)
    ]
    return WeatherSubmission(
        task_id=f"task-{target_date}",
        region="山东省济南市",
        target_date=target_date,
        data_cutoff_time=f"{target_date}T16:00:00+08:00",
        time_info=TimeInfo(
            retrieved_at=retrieved_at,
            provider_issued_at={
                "open_meteo": None,
                "qweather": "2026-08-10T07:00:00+08:00",
            },
            aggregation_completed_at=retrieved_at,
            valid_time=ForecastWindow(
                start=f"{target_date}T00:00:00+08:00",
                end=f"{target_date}T23:00:00+08:00",
                timezone="Asia/Shanghai",
            ),
            forecast_run_id=source_run_id,
        ),
        provider_results=[
            ProviderForecast(
                provider="open_meteo",
                status="ok",
                points=points,
                retrieved_at=retrieved_at,
                provider_issued_at=None,
                source_url="https://api.open-meteo.com/v1/forecast",
                content_sha256="a" * 64,
            ),
            ProviderForecast(
                provider="qweather",
                status="ok",
                points=points,
                retrieved_at=retrieved_at,
                provider_issued_at="2026-08-10T07:00:00+08:00",
                source_url="https://api.qweather.com/v7/weather/24h",
                content_sha256="b" * 64,
            ),
        ],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo", "qweather"],
            points=points,
            summary=ForecastSummary(
                max_temperature=max(point.temperature or 0 for point in points),
                min_temperature=min(point.temperature or 0 for point in points),
                rain_probability=10.0,
                wind_speed=4.0,
                cloud_cover=20.0,
                main_weather="晴到多云",
                high_risk_period="17:00–21:00" if hot else "无明显风险",
                sunrise="06:00",
                sunset="19:00",
            ),
        ),
        confidence={"score": 0.8, "description": "中等"},
        key_factors=[],
        risk_notes=[],
    )


def _row(*, hot_tomorrow: bool = False) -> dict:
    return {
        "market_id": "cn-37-shandong",
        "market": "山东样本区",
        "province": "山东",
        "point_id": "jinan",
        "city": "济南",
        "roles": ["load", "solar", "wind"],
        "submissions": {
            "2026-08-10": _submission(
                "2026-08-10",
                retrieved_at="2026-08-10T08:56:00+08:00",
                source_run_id="source-today",
            ),
            "2026-08-11": _submission(
                "2026-08-11",
                hot=hot_tomorrow,
                retrieved_at="2026-08-10T08:58:00+08:00",
                source_run_id="source-tomorrow",
            ),
        },
        "errors": [],
    }


def _market_config() -> tuple[MarketZone, ...]:
    point = RepresentativePoint(
        point_id="jinan",
        city="济南",
        query="山东省济南市",
        roles=("load", "solar", "wind"),
    )
    return (
        MarketZone(
            market_id="cn-37-shandong",
            market_name="山东样本区",
            provincial_area="山东",
            provincial_code="37",
            points=(point,),
        ),
    )


def _card_text(card: dict) -> str:
    chunks = [card["card"]["header"]["title"]["content"]]
    for element in card["card"]["elements"]:
        text = element.get("text")
        if isinstance(text, dict):
            chunks.append(text.get("content", ""))
        for item in element.get("elements", []):
            if isinstance(item, dict):
                chunks.append(item.get("content", ""))
    return "\n".join(chunks)


def test_cache_persists_an_immutable_briefing_version_by_forecast_run_id(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    snapshot = _snapshot(run_id="run-20260810-0900")
    key = snapshot["cache_key"]
    assert cache.claim_generation(key, "owner", now=100)

    cache.save_and_release(
        key,
        "owner",
        snapshot,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    assert cache.load_version("run-20260810-0900") == snapshot


def test_existing_forecast_run_id_cannot_be_overwritten_with_different_derived_facts(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    original = _snapshot(run_id="run-immutable")
    key = original["cache_key"]
    assert cache.claim_generation(key, "first", now=100)
    cache.save_and_release(
        key,
        "first",
        original,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    altered = _snapshot(run_id="run-immutable")
    altered["quality"]["status"] = "good"
    assert cache.claim_generation(key, "second", now=200)
    with pytest.raises(ValueError, match="immutable"):
        cache.save_and_release(
            key,
            "second",
            altered,
            generator_version="power-briefing-3.0",
            generated_at=200,
        )

    assert cache.load_version("run-immutable") == original


def test_version_library_applies_a_bounded_retention_window(tmp_path):
    day = 24 * 60 * 60
    cache = BriefingCache(
        str(tmp_path / "briefing.db"),
        ttl_seconds=10,
        version_retention_days=2,
    )
    old = _snapshot(run_id="run-expired", report_date="2026-08-07")
    old_key = old["cache_key"]
    assert cache.claim_generation(old_key, "old", now=100)
    cache.save_and_release(
        old_key,
        "old",
        old,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    current = _snapshot(run_id="run-retained", report_date="2026-08-10")
    current_key = current["cache_key"]
    assert cache.claim_generation(current_key, "current", now=100 + 3 * day)
    cache.save_and_release(
        current_key,
        "current",
        current,
        generator_version="power-briefing-3.0",
        generated_at=100 + 3 * day,
    )

    assert cache.load_version("run-expired") is None
    assert cache.load_version("run-retained") == current


def test_current_cache_is_not_mislabeled_as_a_previous_same_release_version(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)
    current = _snapshot(run_id="run-current")
    key = current["cache_key"]
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        current,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    previous = cache.load_previous_same_release(
        report_date="2026-08-10",
        release_slot="09:00",
        market_config_version="market-v1",
        report_version="power-briefing-3.0",
    )

    assert previous is None


def test_briefing_3_cache_fails_closed_when_required_run_provenance_is_missing(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    incomplete = _snapshot(run_id="run-incomplete")
    incomplete.pop("forecast_run_id")
    key = incomplete["cache_key"]
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        incomplete,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    assert cache.load_fresh(key, now=105) is None


def test_version_library_does_not_return_a_run_with_missing_retrieval_time(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    incomplete = _snapshot(run_id="run-without-retrieval-time")
    incomplete["retrieved_at"] = None
    key = incomplete["cache_key"]
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        incomplete,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    assert cache.load_version("run-without-retrieval-time") is None


def test_version_library_rejects_untraceable_or_raw_provider_metadata(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    invalid = _snapshot(run_id="run-with-untraceable-provider")
    invalid["provider_run_metadata"][0]["source_urls"] = []
    invalid["provider_run_metadata"][0]["raw"] = {"temperature": [30.0]}
    key = invalid["cache_key"]
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        invalid,
        generator_version="power-briefing-3.0",
        generated_at=100,
    )

    assert cache.load_version("run-with-untraceable-provider") is None


def test_previous_version_is_yesterdays_matching_release_slot_not_an_arbitrary_run(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)
    runs = [
        _snapshot(run_id="run-older", report_date="2026-08-08"),
        _snapshot(run_id="run-wrong-slot", report_date="2026-08-09", release_slot="15:00"),
        _snapshot(run_id="run-previous", report_date="2026-08-09", release_slot="09:00"),
    ]
    for index, snapshot in enumerate(runs):
        key = snapshot["cache_key"]
        assert cache.claim_generation(key, f"owner-{index}", now=100 + index)
        cache.save_and_release(
            key,
            f"owner-{index}",
            snapshot,
            generator_version="power-briefing-3.0",
            generated_at=100 + index,
        )

    previous = cache.load_previous_same_release(
        report_date="2026-08-10",
        release_slot="09:00",
        market_config_version="market-v1",
        report_version="power-briefing-3.0",
    )

    assert previous is not None
    assert previous["forecast_run_id"] == "run-previous"


@pytest.mark.asyncio
async def test_generated_briefing_3_snapshot_exposes_traceable_run_and_quality_metadata(
    monkeypatch,
    tmp_path,
):
    market_config = _market_config()
    row = _row(hot_tomorrow=True)

    async def fake_fetch(*args, **kwargs):
        return row

    monkeypatch.setattr(power_briefing, "NATIONAL_MARKETS", market_config)
    monkeypatch.setattr(
        power_briefing,
        "MARKET_POINTS",
        ((market_config[0], market_config[0].points[0]),),
    )
    monkeypatch.setattr(power_briefing, "_fetch", fake_fetch)
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)

    snapshot = await power_briefing.generate_briefing_snapshot(
        object(),
        None,
        "2026-08-10",
        cache=cache,
        generated_at=datetime.fromisoformat("2026-08-10T09:01:00+08:00"),
        forecast_run_id="briefing-run-20260810-0900",
        release_slot="09:00",
    )

    assert snapshot["schema_version"] == 2
    assert snapshot["forecast_run_id"] == "briefing-run-20260810-0900"
    assert snapshot["retrieved_at"] == "2026-08-10T08:58:00+08:00"
    assert snapshot["provider_issued_at"] == {
        "open_meteo": None,
        "qweather": "2026-08-10T07:00:00+08:00",
    }
    assert snapshot["valid_time"] == {
        "start": "2026-08-10T00:00:00+08:00",
        "end": "2026-08-11T23:00:00+08:00",
        "timezone": "Asia/Shanghai",
    }
    assert snapshot["sources"] == ["open_meteo", "qweather"]
    assert snapshot["quality"]["status"] == "degraded"
    assert "provider_issued_at_missing:open_meteo" in snapshot["quality"]["reasons"]
    assert snapshot["previous_run_id"] is None
    assert snapshot["version_change"] == {
        "status": "unavailable",
        "reason": "no_previous_same_release",
        "previous_run_id": None,
    }
    assert all("raw" not in item for item in snapshot["provider_run_metadata"])

    card_text = _card_text(snapshot["summary_card"])
    assert "电力气象决策晨报 3.0" in card_text
    assert "briefing-run-20260810-0900" in card_text
    assert "实际抓取 2026-08-10 08:58" in card_text
    assert "聚合完成 2026-08-10 08:58" in card_text
    assert "Open-Meteo 起报时间未提供" in card_text
    assert "数据质量 降级" in card_text
    assert "provider_issued_at_missing" not in card_text
    assert "上一同发布时次版本不可用" in card_text
    assert "未接入负荷、出力、机组、联络线及价格数据" in card_text


@pytest.mark.asyncio
async def test_briefing_compares_risk_only_with_yesterdays_same_release_version(
    monkeypatch,
    tmp_path,
):
    market_config = _market_config()
    current_row = _row(hot_tomorrow=True)

    async def fake_fetch(*args, **kwargs):
        return current_row

    monkeypatch.setattr(power_briefing, "NATIONAL_MARKETS", market_config)
    monkeypatch.setattr(
        power_briefing,
        "MARKET_POINTS",
        ((market_config[0], market_config[0].points[0]),),
    )
    monkeypatch.setattr(power_briefing, "_fetch", fake_fetch)
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)
    previous = _snapshot(run_id="briefing-run-20260809-0900", report_date="2026-08-09")
    previous["market_config_version"] = power_briefing.MARKET_CONFIG_VERSION
    previous["report_version"] = power_briefing.POWER_BRIEFING_REPORT_VERSION
    previous["cache_key"] = (
        "2026-08-09:"
        f"{power_briefing.MARKET_CONFIG_VERSION}:"
        f"{power_briefing.POWER_BRIEFING_REPORT_VERSION}"
    )
    previous["market_risk_snapshots"] = [
        {"market_id": "cn-37-shandong", "severity": 0}
    ]
    previous_key = previous["cache_key"]
    assert cache.claim_generation(previous_key, "previous", now=100)
    cache.save_and_release(
        previous_key,
        "previous",
        previous,
        generator_version=power_briefing.POWER_BRIEFING_REPORT_VERSION,
        generated_at=100,
    )

    current = await power_briefing.generate_briefing_snapshot(
        object(),
        None,
        "2026-08-10",
        cache=cache,
        generated_at=datetime.fromisoformat("2026-08-10T09:01:00+08:00"),
        forecast_run_id="briefing-run-20260810-0900",
        release_slot="09:00",
    )

    assert current["previous_run_id"] == "briefing-run-20260809-0900"
    assert current["version_change"] == {
        "status": "available",
        "reason": "same_release_previous_day",
        "previous_run_id": "briefing-run-20260809-0900",
        "previous_report_date": "2026-08-09",
        "comparable_markets": 1,
        "upgraded_markets": 1,
        "downgraded_markets": 0,
        "unchanged_markets": 0,
    }
    assert "较上一同发布时次 briefing-run-20260809-0900" in _card_text(
        current["summary_card"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provenance_field", "provenance_value"),
    [
        ("source_url", None),
        ("source_url", "provider-name-only"),
        ("content_sha256", None),
        ("content_sha256", "not-a-sha256"),
    ],
)
async def test_briefing_fails_closed_when_external_weather_source_is_not_traceable(
    monkeypatch,
    tmp_path,
    provenance_field,
    provenance_value,
):
    market_config = _market_config()
    untraceable_row = _row(hot_tomorrow=True)
    for submission in untraceable_row["submissions"].values():
        for provider in submission.provider_results:
            setattr(provider, provenance_field, provenance_value)

    async def fake_fetch(*args, **kwargs):
        return untraceable_row

    monkeypatch.setattr(power_briefing, "NATIONAL_MARKETS", market_config)
    monkeypatch.setattr(
        power_briefing,
        "MARKET_POINTS",
        ((market_config[0], market_config[0].points[0]),),
    )
    monkeypatch.setattr(power_briefing, "_fetch", fake_fetch)

    with pytest.raises(RuntimeError, match="traceable external weather provenance"):
        await power_briefing.generate_briefing_snapshot(
            object(),
            None,
            "2026-08-10",
            cache=BriefingCache(str(tmp_path / "briefing.db")),
            generated_at=datetime.fromisoformat("2026-08-10T09:01:00+08:00"),
            forecast_run_id="briefing-run-untraceable",
            release_slot="09:00",
        )


@pytest.mark.asyncio
async def test_briefing_fails_closed_when_external_forecast_valid_time_is_missing(
    monkeypatch,
    tmp_path,
):
    market_config = _market_config()
    row = _row(hot_tomorrow=True)
    for submission in row["submissions"].values():
        submission.time_info.valid_time = ForecastWindow()

    async def fake_fetch(*args, **kwargs):
        return row

    monkeypatch.setattr(power_briefing, "NATIONAL_MARKETS", market_config)
    monkeypatch.setattr(
        power_briefing,
        "MARKET_POINTS",
        ((market_config[0], market_config[0].points[0]),),
    )
    monkeypatch.setattr(power_briefing, "_fetch", fake_fetch)

    with pytest.raises(RuntimeError, match="traceable external weather provenance"):
        await power_briefing.generate_briefing_snapshot(
            object(),
            None,
            "2026-08-10",
            cache=BriefingCache(str(tmp_path / "briefing.db")),
            generated_at=datetime.fromisoformat("2026-08-10T09:01:00+08:00"),
            forecast_run_id="briefing-run-no-valid-time",
            release_slot="09:00",
        )


@pytest.mark.asyncio
async def test_briefing_requires_every_used_provider_result_to_be_traceable(
    monkeypatch,
    tmp_path,
):
    market_config = _market_config()
    row = _row(hot_tomorrow=True)
    row["submissions"]["2026-08-10"].provider_results[0].content_sha256 = None

    async def fake_fetch(*args, **kwargs):
        return row

    monkeypatch.setattr(power_briefing, "NATIONAL_MARKETS", market_config)
    monkeypatch.setattr(
        power_briefing,
        "MARKET_POINTS",
        ((market_config[0], market_config[0].points[0]),),
    )
    monkeypatch.setattr(power_briefing, "_fetch", fake_fetch)

    with pytest.raises(RuntimeError, match="traceable external weather provenance"):
        await power_briefing.generate_briefing_snapshot(
            object(),
            None,
            "2026-08-10",
            cache=BriefingCache(str(tmp_path / "briefing.db")),
            generated_at=datetime.fromisoformat("2026-08-10T09:01:00+08:00"),
            forecast_run_id="briefing-run-partially-untraceable",
            release_slot="09:00",
        )
