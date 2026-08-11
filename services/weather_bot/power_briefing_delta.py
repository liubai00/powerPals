"""Derived-only 15:00 power-weather change briefing.

The delta is allowed to describe only changes between two traceable forecast
runs for the same target window.  It never turns weather proxies into load,
generation, price, position or bidding conclusions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


SHANGHAI_TZ = timezone(timedelta(hours=8))
_MATERIAL_LIFECYCLES = {"upgraded", "weakened", "resolved"}


def meaningful_afternoon_changes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only changes that justify a conditional afternoon message."""

    change = snapshot.get("window_version_change")
    if not isinstance(change, dict) or change.get("status") != "available":
        return []
    try:
        edition_cutoff = datetime.fromisoformat(
            f"{snapshot['report_date']}T{snapshot['release_slot']}:00+08:00"
        )
    except (KeyError, TypeError, ValueError):
        return []
    candidates: list[dict[str, Any]] = []
    for item in change.get("items") or []:
        if not isinstance(item, dict):
            continue
        valid_time = item.get("target_valid_time")
        try:
            valid_end = datetime.fromisoformat(str(valid_time["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if valid_end.tzinfo is None or valid_end.utcoffset() is None:
            continue
        if valid_end.astimezone(SHANGHAI_TZ) <= edition_cutoff:
            continue
        lifecycle = str(item.get("lifecycle") or "")
        severity = int(item.get("current_severity") or 0)
        if lifecycle in _MATERIAL_LIFECYCLES or (
            lifecycle == "first_observation" and severity > 0
        ):
            candidates.append(dict(item))
        elif (
            lifecycle == "continuing"
            and item.get("previous_confidence")
            and item.get("confidence")
            and item.get("previous_confidence") != item.get("confidence")
        ):
            confidence_change = dict(item)
            confidence_change["lifecycle"] = "confidence_changed"
            candidates.append(confidence_change)

    consumed: set[int] = set()
    shifted: list[dict[str, Any]] = []
    for first_index, first in enumerate(candidates):
        if first.get("lifecycle") != "first_observation":
            continue
        current_event_id = str(first.get("current_event_id") or "").strip()
        if not current_event_id:
            continue
        for resolved_index, resolved in enumerate(candidates):
            if resolved_index == first_index or resolved.get("lifecycle") != "resolved":
                continue
            if str(resolved.get("previous_event_id") or "").strip() != current_event_id:
                continue
            moved = dict(first)
            moved["lifecycle"] = "time_shifted"
            moved["previous_target_valid_time"] = resolved.get("target_valid_time")
            moved["previous_direction"] = resolved.get("previous_direction")
            moved["previous_severity"] = resolved.get("previous_severity")
            shifted.append(moved)
            consumed.update({first_index, resolved_index})
            break
    items = [
        item for index, item in enumerate(candidates) if index not in consumed
    ] + shifted
    lifecycle_priority = {
        "upgraded": 0,
        "time_shifted": 1,
        "first_observation": 2,
        "confidence_changed": 3,
        "weakened": 4,
        "resolved": 5,
    }
    return sorted(
        items,
        key=lambda item: (
            lifecycle_priority.get(str(item.get("lifecycle")), 9),
            -int(item.get("current_severity") or 0),
            str(item.get("target_date") or ""),
            str(item.get("market") or ""),
        ),
    )


def _clock_range(value: Any) -> str:
    if not isinstance(value, dict):
        return "时间未提供"
    try:
        start = datetime.fromisoformat(str(value.get("start"))).astimezone(SHANGHAI_TZ)
        end = datetime.fromisoformat(str(value.get("end"))).astimezone(SHANGHAI_TZ)
    except (TypeError, ValueError):
        return "时间未提供"
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _relative_day(report_date: str, target_date: Any) -> str:
    if str(target_date or "") == report_date:
        return "今日"
    try:
        tomorrow = (date.fromisoformat(report_date) + timedelta(days=1)).isoformat()
    except ValueError:
        tomorrow = ""
    if str(target_date or "") == tomorrow:
        return "明日"
    return str(target_date or "目标日")


def _display_clock(value: Any) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return "未记录"
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return "未记录"
    timestamp = timestamp.astimezone(SHANGHAI_TZ)
    return timestamp.strftime("%H:%M")


def _plain_weather_message(value: Any) -> str:
    text = str(value or "").strip()
    if "负荷天气压力代理" in text:
        if any(word in text for word in ("下调", "减弱", "缓解")):
            return "高温或严寒影响有所缓解，用电天气压力减轻"
        return "高温或严寒可能增加用电需求，需结合负荷预测观察"
    if "光资源代理" in text or "光资源天气条件" in text:
        if any(word in text for word in ("改善", "增强")):
            return "云量减少，光伏发电天气条件有所改善"
        return "云量或降雨增加，光伏发电天气条件转弱"
    if "地面风" in text or "10米风" in text:
        return "地面风速明显变化，新能源预测波动可能增加"
    return text or "暂未形成可展示的天气判断"


def _plain_reliability(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("较高") or text.startswith("高"):
        return "较可靠"
    if text.startswith("中等"):
        return "可参考"
    return "继续观察"


def build_afternoon_delta_card(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Build an incremental Feishu card, or ``None`` when silence is required."""

    items = meaningful_afternoon_changes(snapshot)
    if not items:
        return None
    report_date = str(snapshot.get("report_date") or "")
    lifecycle_labels = {
        "upgraded": "风险升级",
        "weakened": "风险减弱",
        "resolved": "结束跟踪",
        "first_observation": "新增关注",
        "time_shifted": "时段移动",
        "confidence_changed": "可信度变化",
    }
    reasons = "；".join(
        (
            f"{item.get('market')}·{item.get('representative_point')}代表点"
            f"{item.get('window_label')}{lifecycle_labels.get(str(item.get('lifecycle')), '预测变化')}"
        )
        for item in items[:3]
    )
    lines = [
        "**为什么发送**",
        f"相较今天09:00晨报，{reasons}，因此发送本次更新。",
        "",
        "**变化详情**",
    ]
    for index, item in enumerate(items, start=1):
        relative_day = _relative_day(report_date, item.get("target_date"))
        lifecycle = lifecycle_labels.get(str(item.get("lifecycle")), "预测变化")
        lines.extend(
            (
                (
                    f"{index}. **{item.get('market')}·{item.get('representative_point')}代表点｜"
                    f"{relative_day}{_clock_range(item.get('target_valid_time'))}｜"
                    f"{item.get('window_label')}｜{lifecycle}**"
                ),
                (
                    f"   {_plain_weather_message(item.get('previous_direction') or '此前无同时间判断')} → "
                    f"{_plain_weather_message(item.get('current_direction'))}"
                ),
                *(
                    (
                        "   时段变化："
                        f"{_clock_range(item.get('previous_target_valid_time'))} → "
                        f"{_clock_range(item.get('target_valid_time'))}",
                    )
                    if item.get("lifecycle") == "time_shifted"
                    else ()
                ),
                *(
                    (
                        "   可信度变化："
                        f"{item.get('previous_confidence')} → {item.get('confidence')}",
                    )
                    if item.get("lifecycle") == "confidence_changed"
                    else ()
                ),
                f"   变化原因：{item.get('driver')}",
                f"   继续观察：{item.get('verification_item')}",
                f"   可靠程度：{_plain_reliability(item.get('confidence'))}",
            )
        )
    retrieved_at = _display_clock(
        snapshot.get("retrieved_at") or snapshot.get("generated_at")
    )
    display_date = report_date[5:].replace("-", "/") if len(report_date) >= 10 else report_date
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {
                    "tag": "plain_text",
                    "content": f"🔔 午后气象变化提醒｜{display_date} 15:00",
                },
            },
            "elements": [
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": (
                                f"对比基准：今天09:00晨报｜最新数据：{retrieved_at}"
                            ),
                        }
                    ],
                },
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(lines)},
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": (
                                "仅为天气侧变化；请结合负荷预测、新能源功率预测、"
                                "机组和线路信息核查，不构成价格、报价、仓位或买卖建议。"
                            ),
                        }
                    ],
                },
            ],
        },
    }
