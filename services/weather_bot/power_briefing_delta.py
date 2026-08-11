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
    candidates: list[dict[str, Any]] = []
    for item in change.get("items") or []:
        if not isinstance(item, dict):
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


def build_afternoon_delta_card(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Build an incremental Feishu card, or ``None`` when silence is required."""

    items = meaningful_afternoon_changes(snapshot)
    if not items:
        return None
    report_date = str(snapshot.get("report_date") or "")
    current_run_id = str(snapshot.get("forecast_run_id") or "未提供")
    previous_run_id = str(
        (snapshot.get("window_version_change") or {}).get("previous_run_id")
        or "未提供"
    )
    lifecycle_labels = {
        "upgraded": "风险升级",
        "weakened": "风险减弱",
        "resolved": "结束跟踪",
        "first_observation": "新增关注",
        "time_shifted": "时段移动",
        "confidence_changed": "可信度变化",
    }
    lines = [
        "**比较基准**",
        "对比今日09:00晨报",
        f"预测批次：{previous_run_id} → {current_run_id}",
        "",
        "**本次只列发生实质变化的窗口**",
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
                    f"   {item.get('previous_direction') or '此前无同时间判断'} → "
                    f"{item.get('current_direction')}"
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
                f"   建议核对：{item.get('verification_item')}",
                f"   可信度：{item.get('confidence')}",
            )
        )
    generated_at = str(snapshot.get("retrieved_at") or snapshot.get("generated_at") or "未提供")
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {
                    "tag": "plain_text",
                    "content": "⏱ 15:00 电力气象变更快报",
                },
            },
            "elements": [
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"更新 {generated_at}｜仅在09:00后出现实质变化时发送",
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
                                "仅为天气侧代理变化；请结合负荷预测、新能源功率预测、"
                                "机组和线路信息核查，不构成价格、报价、仓位或买卖建议。"
                            ),
                        }
                    ],
                },
            ],
        },
    }
