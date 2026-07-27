from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from services.weather_bot.briefing_cache import BriefingCache
from services.weather_bot import power_briefing


def _snapshot(cache_key: str) -> dict:
    generated = datetime.now(timezone.utc).isoformat()
    key_parts = cache_key.rsplit(":", 2)
    report_date, market_config_version, report_version = (
        key_parts
        if len(key_parts) == 3
        else ("2026-07-27", "test", "test")
    )
    coverage = {
        "provincial_areas": {"covered": 31, "total": 31},
        "markets": {"covered": 33, "total": 33},
        "points": {"covered": 75, "total": 75},
        "baseline_points": {"covered": 75, "total": 75},
    }
    statistics = {"configured_markets": 33, "classified_markets": 33}
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": "测试晨报"}},
            "elements": [],
        },
    }
    return {
        "schema_version": 1,
        "cache_key": cache_key,
        "report_date": report_date,
        "market_config_version": market_config_version,
        "report_version": report_version,
        "generated_at": generated,
        "expires_at": generated,
        "coverage": coverage,
        "statistics": statistics,
        "summary_card": card,
        "detail_card": card,
    }


def test_briefing_cache_round_trip_and_expiry(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    key = "test-key"
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        _snapshot(key),
        generator_version="test",
        generated_at=100,
    )

    assert cache.load_fresh(key, now=105)["cache_key"] == key
    assert cache.load_fresh(key, now=111) is None


def test_briefing_cache_rejects_parseable_but_incomplete_snapshot(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    key = "test-key"
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        {
            "schema_version": 1,
            "cache_key": key,
            "report_version": "test",
            "generated_at": "2026-07-27T09:00:00+08:00",
        },
        generator_version="test",
        generated_at=100,
    )

    assert cache.load_fresh(key, now=105) is None


def test_briefing_cache_rejects_card_without_renderable_title(tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=10)
    key = "test-key"
    snapshot = _snapshot(key)
    snapshot["summary_card"]["card"]["header"] = {}
    assert cache.claim_generation(key, "owner", now=100)
    cache.save_and_release(
        key,
        "owner",
        snapshot,
        generator_version="test",
        generated_at=100,
    )

    assert cache.load_fresh(key, now=105) is None


def test_briefing_cache_key_changes_across_date_and_config_version(monkeypatch):
    first = power_briefing.briefing_cache_key("2026-07-27")
    assert first != power_briefing.briefing_cache_key("2026-07-28")

    monkeypatch.setattr(power_briefing, "MARKET_CONFIG_VERSION", "next-config")
    assert first != power_briefing.briefing_cache_key("2026-07-27")


@pytest.mark.asyncio
async def test_briefing_cache_hit_skips_generation(monkeypatch, tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)
    key = power_briefing.briefing_cache_key("2026-07-27")
    assert cache.claim_generation(key, "seed")
    cache.save_and_release(
        key,
        "seed",
        _snapshot(key),
        generator_version=_snapshot(key)["report_version"],
    )

    async def fail_generation(*args, **kwargs):
        raise AssertionError("fresh cache must skip nationwide generation")

    monkeypatch.setattr(power_briefing, "generate_briefing_snapshot", fail_generation)
    snapshot, cache_hit = await power_briefing.get_or_generate_briefing(
        object(),
        None,
        "2026-07-27",
        cache=cache,
    )

    assert cache_hit is True
    assert snapshot["cache_key"] == key


@pytest.mark.asyncio
async def test_concurrent_briefing_generation_is_single_flight(monkeypatch, tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)
    key = power_briefing.briefing_cache_key("2026-07-27")
    calls = 0

    async def fake_generation(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return _snapshot(key)

    monkeypatch.setattr(power_briefing, "generate_briefing_snapshot", fake_generation)
    first, second = await asyncio.gather(
        power_briefing.get_or_generate_briefing(
            object(),
            None,
            "2026-07-27",
            cache=cache,
        ),
        power_briefing.get_or_generate_briefing(
            object(),
            None,
            "2026-07-27",
            cache=cache,
        ),
    )

    assert calls == 1
    assert first[0]["cache_key"] == second[0]["cache_key"] == key
    assert sorted((first[1], second[1])) == [False, True]


@pytest.mark.asyncio
async def test_cancelled_generation_releases_lease_immediately(monkeypatch, tmp_path):
    cache = BriefingCache(str(tmp_path / "briefing.db"), ttl_seconds=3600)
    started = asyncio.Event()

    async def cancelled_generation(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        power_briefing,
        "generate_briefing_snapshot",
        cancelled_generation,
    )
    task = asyncio.create_task(
        power_briefing.get_or_generate_briefing(
            object(),
            None,
            "2026-07-27",
            cache=cache,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    key = power_briefing.briefing_cache_key("2026-07-27")
    assert cache.claim_generation(key, "replacement", lease_seconds=1)
