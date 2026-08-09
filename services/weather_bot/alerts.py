"""Deterministic alert evaluation and idempotent outbox.

Evaluation accepts already-gated external observations.  This module never
fetches data and the outbox never sends unless an explicit caller supplies a
sender and both delivery gates are open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Awaitable, Callable, Literal
from uuid import uuid4

from services.weather_bot.subscriptions import SubscriptionRecord


AlertAction = Literal[
    "inactive_rule",
    "data_unavailable",
    "duplicate_observation",
    "pending_trigger",
    "triggered",
    "suppressed_active",
    "escalated",
    "retriggered",
    "pending_recovery",
    "recovered",
    "inactive",
]


@dataclass(frozen=True)
class AlertObservation:
    source_run_id: str
    provenance_ref: str | None
    availability_status: Literal["allowed_for_calculation", "text_only", "rejected"]
    observed_at: datetime
    risk_window: str
    value: float
    severity: str
    data_available: bool


@dataclass(frozen=True)
class AlertEvaluation:
    action: AlertAction
    outbox_id: str | None = None


@dataclass(frozen=True)
class OutboxItem:
    outbox_id: str
    subscription_id: str
    rule_version: int
    kind: Literal["trigger", "escalation", "repeat", "recovery"]
    dedupe_key: str
    risk_window: str
    severity: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class DeliveryResult:
    sent: int
    failed: int
    pending: int
    reason: str


_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class AlertOutbox:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS alert_state (
                    subscription_id TEXT NOT NULL,
                    rule_version INTEGER NOT NULL,
                    trigger_hits INTEGER NOT NULL DEFAULT 0,
                    recovery_hits INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    current_severity TEXT,
                    last_sent_at TEXT,
                    PRIMARY KEY(subscription_id, rule_version)
                );
                CREATE TABLE IF NOT EXISTS alert_observations (
                    subscription_id TEXT NOT NULL,
                    rule_version INTEGER NOT NULL,
                    source_run_id TEXT NOT NULL,
                    risk_window TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(subscription_id, rule_version, source_run_id, risk_window)
                );
                CREATE TABLE IF NOT EXISTS alert_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    subscription_id TEXT NOT NULL,
                    rule_version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    risk_window TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                """
            )

    def pending(self) -> list[OutboxItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alert_outbox WHERE sent_at IS NULL ORDER BY created_at, outbox_id"
            ).fetchall()
        return [_outbox_item(row) for row in rows]

    async def deliver(
        self,
        sender: Callable[[OutboxItem], Awaitable[object]],
        *,
        send_enabled: bool,
        dry_run: bool,
        now: datetime,
    ) -> DeliveryResult:
        _require_aware(now)
        items = self.pending()
        if dry_run:
            return DeliveryResult(sent=0, failed=0, pending=len(items), reason="dry_run")
        if not send_enabled:
            return DeliveryResult(sent=0, failed=0, pending=len(items), reason="send_disabled")
        sent = 0
        failed = 0
        policy_suppressed = 0
        for item in items:
            if not self._delivery_policy_allows(item, now=now):
                policy_suppressed += 1
                continue
            try:
                await sender(item)
            except Exception:  # noqa: BLE001 - keep item pending for a reviewed retry
                failed += 1
                continue
            with self._connect() as connection:
                connection.execute(
                    "UPDATE alert_outbox SET sent_at = ? WHERE outbox_id = ? AND sent_at IS NULL",
                    (now.isoformat(), item.outbox_id),
                )
            sent += 1
        return DeliveryResult(
            sent=sent,
            failed=failed,
            pending=len(self.pending()),
            reason=(
                "policy_suppressed"
                if sent == 0 and failed == 0 and policy_suppressed
                else "delivered"
                if failed == 0 and policy_suppressed == 0
                else "partial"
            ),
        )

    def _delivery_policy_allows(self, item: OutboxItem, *, now: datetime) -> bool:
        policy = item.payload.get("delivery_policy")
        if not isinstance(policy, dict):
            return False
        local_now = _as_policy_timezone(now, str(policy.get("timezone") or ""))
        if local_now is None:
            return False
        quiet_start = policy.get("quiet_hours_start")
        quiet_end = policy.get("quiet_hours_end")
        if isinstance(quiet_start, str) and isinstance(quiet_end, str):
            if _in_quiet_hours(local_now.time(), quiet_start, quiet_end):
                return False
        try:
            hourly_cap = int(policy["max_sends_per_hour"])
            daily_cap = int(policy["max_sends_per_day"])
        except (KeyError, TypeError, ValueError):
            return False
        if hourly_cap < 1 or daily_cap < 1:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sent_at FROM alert_outbox
                WHERE subscription_id = ? AND sent_at IS NOT NULL
                """,
                (item.subscription_id,),
            ).fetchall()
        sent_times = [
            datetime.fromisoformat(str(row["sent_at"]))
            for row in rows
            if row["sent_at"]
        ]
        hour_count = sum(
            1
            for sent_at in sent_times
            if timedelta(0) <= now.astimezone(timezone.utc) - sent_at.astimezone(timezone.utc) < timedelta(hours=1)
        )
        day_count = sum(
            1
            for sent_at in sent_times
            if (_as_policy_timezone(sent_at, str(policy.get("timezone") or "")) or sent_at).date()
            == local_now.date()
        )
        return hour_count < hourly_cap and day_count < daily_cap


class AlertEngine:
    def __init__(self, outbox: AlertOutbox) -> None:
        self.outbox = outbox

    def evaluate(
        self,
        rule: SubscriptionRecord,
        observation: AlertObservation,
        *,
        now: datetime,
    ) -> AlertEvaluation:
        _require_aware(now)
        _require_aware(observation.observed_at)
        if rule.status != "active" or rule.spec.kind != "threshold":
            return AlertEvaluation("inactive_rule")

        with self.outbox._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO alert_observations(
                        subscription_id, rule_version, source_run_id, risk_window, observed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        rule.subscription_id,
                        rule.version,
                        observation.source_run_id,
                        observation.risk_window,
                        observation.observed_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                return AlertEvaluation("duplicate_observation")

            if (
                not observation.data_available
                or observation.availability_status != "allowed_for_calculation"
                or not (observation.provenance_ref or "").strip()
            ):
                return AlertEvaluation("data_unavailable")

            state = connection.execute(
                """
                SELECT * FROM alert_state
                WHERE subscription_id = ? AND rule_version = ?
                """,
                (rule.subscription_id, rule.version),
            ).fetchone()
            trigger_hits = int(state["trigger_hits"]) if state else 0
            recovery_hits = int(state["recovery_hits"]) if state else 0
            is_active = bool(state["is_active"]) if state else False
            current_severity = str(state["current_severity"] or "") if state else ""
            last_sent_at = (
                datetime.fromisoformat(str(state["last_sent_at"]))
                if state and state["last_sent_at"]
                else None
            )

            trigger = _compare(
                observation.value,
                rule.spec.operator or ">=",
                float(rule.spec.trigger_threshold),
            )
            recovered = observation.value < float(rule.spec.recovery_threshold)
            action: AlertAction
            item: OutboxItem | None = None

            if not is_active:
                recovery_hits = 0
                if trigger:
                    trigger_hits += 1
                    if trigger_hits >= rule.spec.consecutive_hits:
                        is_active = True
                        trigger_hits = 0
                        current_severity = observation.severity
                        last_sent_at = now
                        item = _queue(
                            connection,
                            rule,
                            observation,
                            kind="trigger",
                            now=now,
                        )
                        action = "triggered"
                    else:
                        action = "pending_trigger"
                else:
                    trigger_hits = 0
                    action = "inactive"
            elif recovered:
                trigger_hits = 0
                recovery_hits += 1
                if recovery_hits >= rule.spec.consecutive_hits:
                    is_active = False
                    recovery_hits = 0
                    current_severity = ""
                    last_sent_at = now
                    item = _queue(
                        connection,
                        rule,
                        observation,
                        kind="recovery",
                        severity="recovered",
                        now=now,
                    )
                    action = "recovered"
                else:
                    action = "pending_recovery"
            else:
                recovery_hits = 0
                if _severity_rank(observation.severity) > _severity_rank(current_severity):
                    current_severity = observation.severity
                    last_sent_at = now
                    item = _queue(
                        connection,
                        rule,
                        observation,
                        kind="escalation",
                        now=now,
                    )
                    action = "escalated"
                elif (
                    trigger
                    and last_sent_at is not None
                    and (now - last_sent_at).total_seconds() >= rule.spec.cooldown_seconds
                ):
                    last_sent_at = now
                    cycle = int(now.timestamp() // max(1, rule.spec.cooldown_seconds))
                    item = _queue(
                        connection,
                        rule,
                        observation,
                        kind="repeat",
                        now=now,
                        dedupe_suffix=str(cycle),
                    )
                    action = "retriggered" if item else "suppressed_active"
                else:
                    action = "suppressed_active"

            connection.execute(
                """
                INSERT INTO alert_state(
                    subscription_id, rule_version, trigger_hits, recovery_hits,
                    is_active, current_severity, last_sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id, rule_version) DO UPDATE SET
                    trigger_hits=excluded.trigger_hits,
                    recovery_hits=excluded.recovery_hits,
                    is_active=excluded.is_active,
                    current_severity=excluded.current_severity,
                    last_sent_at=excluded.last_sent_at
                """,
                (
                    rule.subscription_id,
                    rule.version,
                    trigger_hits,
                    recovery_hits,
                    int(is_active),
                    current_severity or None,
                    last_sent_at.isoformat() if last_sent_at else None,
                ),
            )
            return AlertEvaluation(action, item.outbox_id if item else None)


def _queue(
    connection: sqlite3.Connection,
    rule: SubscriptionRecord,
    observation: AlertObservation,
    *,
    kind: Literal["trigger", "escalation", "repeat", "recovery"],
    now: datetime,
    severity: str | None = None,
    dedupe_suffix: str = "",
) -> OutboxItem | None:
    effective_severity = severity or observation.severity
    dedupe_key = "|".join(
        filter(
            None,
            (
                rule.subscription_id,
                str(rule.version),
                observation.risk_window,
                kind,
                effective_severity,
                dedupe_suffix,
            ),
        )
    )
    item = OutboxItem(
        outbox_id=f"out-{uuid4().hex}",
        subscription_id=rule.subscription_id,
        rule_version=rule.version,
        kind=kind,
        dedupe_key=dedupe_key,
        risk_window=observation.risk_window,
        severity=effective_severity,
        payload={
            "source_run_id": observation.source_run_id,
            "provenance_ref": observation.provenance_ref or "",
            "availability_status": observation.availability_status,
            "observed_at": observation.observed_at.isoformat(),
            "value": observation.value,
            "metric": rule.spec.metric or "",
            "regions": list(rule.spec.regions),
            "delivery_policy": {
                "timezone": rule.spec.timezone,
                "quiet_hours_start": rule.spec.quiet_hours_start,
                "quiet_hours_end": rule.spec.quiet_hours_end,
                "max_sends_per_hour": rule.spec.max_sends_per_hour,
                "max_sends_per_day": rule.spec.max_sends_per_day,
            },
        },
        created_at=now,
    )
    try:
        connection.execute(
            """
            INSERT INTO alert_outbox(
                outbox_id, subscription_id, rule_version, kind, dedupe_key,
                risk_window, severity, payload_json, created_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                item.outbox_id,
                item.subscription_id,
                item.rule_version,
                item.kind,
                item.dedupe_key,
                item.risk_window,
                item.severity,
                json.dumps(item.payload, ensure_ascii=False, sort_keys=True),
                item.created_at.isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        return None
    return item


def _outbox_item(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        outbox_id=str(row["outbox_id"]),
        subscription_id=str(row["subscription_id"]),
        rule_version=int(row["rule_version"]),
        kind=str(row["kind"]),
        dedupe_key=str(row["dedupe_key"]),
        risk_window=str(row["risk_window"]),
        severity=str(row["severity"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _compare(value: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return value >= threshold
    if operator == ">":
        return value > threshold
    if operator == "<=":
        return value <= threshold
    if operator == "<":
        return value < threshold
    raise ValueError(f"unsupported threshold operator: {operator}")


def _severity_rank(value: str) -> int:
    return _SEVERITY_RANK.get(value.lower(), 0)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("alert timestamps must be timezone-aware")


def _as_policy_timezone(value: datetime, timezone_name: str) -> datetime | None:
    if timezone_name == "Asia/Shanghai":
        return value.astimezone(timezone(timedelta(hours=8), name="Asia/Shanghai"))
    if timezone_name == "UTC":
        return value.astimezone(timezone.utc)
    return None


def _in_quiet_hours(current: time, start_text: str, end_text: str) -> bool:
    try:
        start = time.fromisoformat(start_text)
        end = time.fromisoformat(end_text)
    except ValueError:
        return True
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


__all__ = [
    "AlertEngine",
    "AlertEvaluation",
    "AlertObservation",
    "AlertOutbox",
    "DeliveryResult",
    "OutboxItem",
]
