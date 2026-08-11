from __future__ import annotations

from services.weather_bot.power_briefing_delta import (
    build_afternoon_delta_card,
    meaningful_afternoon_changes,
)


def _change(
    *,
    lifecycle: str,
    target_date: str = "2026-08-12",
    current_severity: int = 3,
) -> dict:
    return {
        "market_id": "cn-44-guangdong",
        "market": "广东样本区",
        "representative_point": "广州",
        "target_date": target_date,
        "window_id": "evening_peak",
        "window_label": "晚峰",
        "target_valid_time": {
            "start": f"{target_date}T17:00:00+08:00",
            "end": f"{target_date}T21:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "signal_type": "load",
        "lifecycle": lifecycle,
        "previous_direction": "暂无需要重点跟踪的天气侧信号",
        "current_direction": "负荷天气压力代理偏高",
        "previous_severity": 0,
        "current_severity": current_severity,
        "driver": "最新预报将体感高温持续时间延长至20:00",
        "verification_item": "晚峰负荷预测、机组可用状态",
        "confidence": "中等",
        "comparison_basis": {
            "reason": "same_area_target_window_proxy_and_methodology",
            "current_run_id": "briefing-run-20260811-1500",
            "previous_run_id": "briefing-run-20260811-0900",
        },
    }


def _snapshot(items: list[dict]) -> dict:
    return {
        "report_date": "2026-08-11",
        "release_slot": "15:00",
        "forecast_run_id": "briefing-run-20260811-1500",
        "retrieved_at": "2026-08-11T14:50:00+08:00",
        "window_version_change": {
            "status": "available",
            "reason": "same_target_window_lifecycle",
            "previous_run_id": "briefing-run-20260811-0900",
            "items": items,
        },
    }


def _card_text(card: dict) -> str:
    chunks = [card["card"]["header"]["title"]["content"]]
    for element in card["card"]["elements"]:
        text = element.get("text")
        if isinstance(text, dict):
            chunks.append(text.get("content", ""))
        for child in element.get("elements", []):
            if isinstance(child, dict):
                chunks.append(child.get("content", ""))
    return "\n".join(chunks)


def test_afternoon_delta_is_silent_when_0900_forecast_has_no_material_change():
    snapshot = _snapshot(
        [
            _change(lifecycle="continuing"),
            _change(lifecycle="stable", current_severity=0),
        ]
    )

    assert meaningful_afternoon_changes(snapshot) == []
    assert build_afternoon_delta_card(snapshot) is None


def test_afternoon_delta_excludes_today_windows_that_ended_before_the_1500_edition():
    ended = _change(
        lifecycle="resolved",
        target_date="2026-08-11",
        current_severity=0,
    )
    ended["window_id"] = "morning_peak"
    ended["window_label"] = "早峰"
    ended["target_valid_time"] = {
        "start": "2026-08-11T08:00:00+08:00",
        "end": "2026-08-11T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
    }
    future = _change(lifecycle="upgraded", target_date="2026-08-11")

    changes = meaningful_afternoon_changes(_snapshot([ended, future]))

    assert [item["window_id"] for item in changes] == ["evening_peak"]


def test_afternoon_delta_only_contains_explicit_changes_from_today_0900():
    snapshot = _snapshot(
        [
            _change(lifecycle="upgraded"),
            _change(lifecycle="continuing"),
            _change(lifecycle="stable", current_severity=0),
        ]
    )

    changes = meaningful_afternoon_changes(snapshot)
    card = build_afternoon_delta_card(snapshot)

    assert [item["lifecycle"] for item in changes] == ["upgraded"]
    assert card is not None
    assert card["card"]["header"]["title"]["content"] == (
        "🔔 午后气象变化提醒｜08/11 15:00"
    )
    text = _card_text(card)
    assert "对比基准：今天09:00晨报｜最新数据：14:50" in text
    assert "为什么发送" in text
    assert "briefing-run-20260811-0900" not in text
    assert "briefing-run-20260811-1500" not in text
    assert "2026-08-11T14:50:00+08:00" not in text
    assert "广东样本区·广州代表点｜明日17:00–21:00｜晚峰｜风险升级" in text
    assert "高温或严寒可能增加用电需求，需结合负荷预测观察" in text
    assert "代理" not in text
    assert "继续观察：晚峰负荷预测、机组可用状态" in text
    assert "可靠程度：可参考" in text
    assert "实际负荷" not in text
    assert "电价上涨" not in text


def test_afternoon_delta_does_not_guess_a_timezone_for_naive_update_time():
    snapshot = _snapshot([_change(lifecycle="upgraded")])
    snapshot["retrieved_at"] = "2026-08-11T14:50:00"

    card = build_afternoon_delta_card(snapshot)

    assert card is not None
    assert "最新数据：未记录" in _card_text(card)


def test_afternoon_first_observation_only_sends_when_it_crosses_attention_threshold():
    no_attention = _snapshot(
        [_change(lifecycle="first_observation", current_severity=0)]
    )
    attention = _snapshot(
        [_change(lifecycle="first_observation", current_severity=2)]
    )

    assert meaningful_afternoon_changes(no_attention) == []
    assert [item["lifecycle"] for item in meaningful_afternoon_changes(attention)] == [
        "first_observation"
    ]


def test_afternoon_delta_collapses_a_resolved_and_new_window_into_a_time_shift():
    resolved = _change(lifecycle="resolved", target_date="2026-08-12", current_severity=0)
    resolved["previous_signal_type"] = "load"
    resolved["previous_event_id"] = "cn-44-guangdong|2026-08-12|load|power-weather-proxy-v1"
    resolved["previous_direction"] = "负荷天气压力代理偏高"
    resolved["current_direction"] = "暂无需要重点跟踪的天气侧信号"
    moved = _change(lifecycle="first_observation", target_date="2026-08-12")
    moved["current_event_id"] = "cn-44-guangdong|2026-08-12|load|power-weather-proxy-v1"
    moved["target_valid_time"] = {
        "start": "2026-08-12T18:00:00+08:00",
        "end": "2026-08-12T22:00:00+08:00",
        "timezone": "Asia/Shanghai",
    }

    changes = meaningful_afternoon_changes(_snapshot([resolved, moved]))

    assert len(changes) == 1
    assert changes[0]["lifecycle"] == "time_shifted"
    assert changes[0]["previous_target_valid_time"] == resolved["target_valid_time"]
    assert changes[0]["target_valid_time"] == moved["target_valid_time"]


def test_afternoon_delta_reports_a_material_confidence_change_for_an_active_window():
    continuing = _change(lifecycle="continuing")
    continuing["previous_confidence"] = "中等"
    continuing["confidence"] = "偏低（数据源分歧扩大）"

    changes = meaningful_afternoon_changes(_snapshot([continuing]))

    assert len(changes) == 1
    assert changes[0]["lifecycle"] == "confidence_changed"
    assert changes[0]["previous_confidence"] == "中等"
    assert changes[0]["confidence"] == "偏低（数据源分歧扩大）"
