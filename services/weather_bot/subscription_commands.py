"""Deterministic parsing for subscription drafts and explicit lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from services.weather_bot.electricity_entities import parse_electricity_entities
from services.weather_bot.subscriptions import SubscriptionSpec


SubscriptionAction = Literal[
    "create_draft",
    "confirm",
    "cancel",
    "update_threshold",
]


@dataclass(frozen=True)
class SubscriptionCommand:
    action: SubscriptionAction
    spec: SubscriptionSpec | None = None
    explicit_confirmation: bool = False
    subscription_id: str | None = None
    new_threshold: float | None = None


_CONFIRM_RE = re.compile(
    r"^确认订阅(?:[：:]?(?P<subscription_id>sub-[0-9a-f]{32}))?$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"^(?:取消订阅|停止订阅|关闭订阅)$")
_UPDATE_THRESHOLD_RE = re.compile(
    r"(?:阈值)?.{0,8}(?:改成|改为|调整为|调到)\s*(?P<value>\d+(?:\.\d+)?)\s*(?:℃|度|%)?"
)
_SCHEDULE_RE = re.compile(
    r"每天\s*(?P<hour>[01]?\d|2[0-3])(?:\s*[:：点时]\s*(?P<minute>[0-5]?\d))?\s*(?:分)?"
)
_THRESHOLD_RE = re.compile(
    r"(?P<operator>超过|高于|达到|不低于|低于|不高于)\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:℃|度|%)?"
)
_METRICS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"体感温度|体感高温"), "apparent_temperature"),
    (re.compile(r"(?:最高|最低|平均)?温度|气温"), "temperature"),
    (re.compile(r"降水概率|降雨概率"), "precipitation_probability"),
    (re.compile(r"10\s*米风速|地面风速|风速"), "wind_speed_10m"),
    (re.compile(r"数据源.*(?:差|分歧)"), "source_divergence"),
)
_OPERATOR = {
    "超过": ">",
    "高于": ">",
    "达到": ">=",
    "不低于": ">=",
    "低于": "<",
    "不高于": "<=",
}


def parse_subscription_command(text: str) -> SubscriptionCommand | None:
    normalized = re.sub(r"\s+", "", text).strip("，,。；;：:")
    if confirm_match := _CONFIRM_RE.fullmatch(normalized):
        subscription_id = confirm_match.group("subscription_id")
        return SubscriptionCommand(
            action="confirm",
            explicit_confirmation=True,
            subscription_id=subscription_id.lower() if subscription_id else None,
        )
    if _CANCEL_RE.fullmatch(normalized):
        return SubscriptionCommand(action="cancel")

    update_match = _UPDATE_THRESHOLD_RE.search(normalized)
    if update_match and any(marker in normalized for marker in ("阈值", "改成", "改为", "调整为", "调到")):
        return SubscriptionCommand(
            action="update_threshold",
            new_threshold=float(update_match.group("value")),
        )

    regions = _regions_from_text(text)
    schedule_match = _SCHEDULE_RE.search(text)
    if schedule_match and regions and any(marker in text for marker in ("看", "发", "报", "提醒")):
        hour = int(schedule_match.group("hour"))
        minute = int(schedule_match.group("minute") or 0)
        return SubscriptionCommand(
            action="create_draft",
            spec=SubscriptionSpec(
                kind="scheduled_briefing",
                regions=regions,
                schedule_time=f"{hour:02d}:{minute:02d}",
                timezone="Asia/Shanghai",
            ),
        )

    threshold_match = _THRESHOLD_RE.search(text)
    metric = _metric_from_text(text)
    if threshold_match and metric and regions and any(marker in text for marker in ("提醒", "通知", "告警")):
        trigger = float(threshold_match.group("value"))
        operator = _OPERATOR[threshold_match.group("operator")]
        return SubscriptionCommand(
            action="create_draft",
            spec=SubscriptionSpec(
                kind="threshold",
                regions=regions,
                metric=metric,
                operator=operator,
                trigger_threshold=trigger,
                recovery_threshold=_default_recovery_threshold(metric, operator, trigger),
                consecutive_hits=2,
                cooldown_seconds=6 * 3600,
            ),
        )
    return None


def _regions_from_text(text: str) -> tuple[str, ...]:
    entities = parse_electricity_entities(text)
    regions: list[str] = []
    for area in entities.analysis_areas:
        if area.kind != "provincial_area" or area.name in regions:
            continue
        regions.append(area.name)
    return tuple(regions)


def _metric_from_text(text: str) -> str | None:
    for pattern, metric in _METRICS:
        if pattern.search(text):
            return metric
    return None


def _default_recovery_threshold(metric: str, operator: str, trigger: float) -> float:
    if operator in {">", ">="}:
        return trigger - (2.0 if metric in {"temperature", "apparent_temperature"} else 1.0)
    return trigger + (2.0 if metric in {"temperature", "apparent_temperature"} else 1.0)


__all__ = ["SubscriptionCommand", "parse_subscription_command"]
