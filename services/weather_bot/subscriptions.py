"""Reviewed subscription state with immutable versions and conversation scoping.

This module stores authorization/configuration only.  It contains no weather or
power facts and has no network or message-sending capability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal
from uuid import uuid4


SubscriptionStatus = Literal[
    "draft",
    "pending_confirmation",
    "active",
    "paused",
    "cancelled",
]


@dataclass(frozen=True)
class ConversationScope:
    bot_role: str
    chat_type: str
    chat_id: str
    thread_id: str
    user_id: str


@dataclass(frozen=True)
class SubscriptionSpec:
    kind: Literal["scheduled_briefing", "threshold"]
    regions: tuple[str, ...]
    schedule_time: str | None = None
    timezone: str = "Asia/Shanghai"
    metric: str | None = None
    operator: Literal[">=", ">", "<=", "<"] | None = None
    trigger_threshold: float | None = None
    recovery_threshold: float | None = None
    consecutive_hits: int = 2
    cooldown_seconds: int = 6 * 3600
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    max_sends_per_hour: int = 2
    max_sends_per_day: int = 8

    def __post_init__(self) -> None:
        if not self.regions:
            raise ValueError("at least one region is required")
        if self.consecutive_hits < 1:
            raise ValueError("consecutive_hits must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        if self.max_sends_per_hour < 1 or self.max_sends_per_day < 1:
            raise ValueError("send caps must be positive")
        if bool(self.quiet_hours_start) != bool(self.quiet_hours_end):
            raise ValueError("quiet hour start and end must be configured together")
        for value in (self.quiet_hours_start, self.quiet_hours_end):
            if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError("quiet hours must use HH:MM")
        if self.kind == "scheduled_briefing" and not self.schedule_time:
            raise ValueError("scheduled briefing requires schedule_time")
        if self.kind == "threshold" and (
            not self.metric
            or not self.operator
            or self.trigger_threshold is None
            or self.recovery_threshold is None
        ):
            raise ValueError("threshold subscription requires metric, operator and thresholds")


@dataclass(frozen=True)
class SubscriptionRecord:
    subscription_id: str
    version: int
    status: SubscriptionStatus
    scope: ConversationScope
    spec: SubscriptionSpec
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None = None
    backfill_from: datetime | None = None
    confirmed_by_user_id: str | None = None


class SubscriptionScopeMismatch(PermissionError):
    pass


class SubscriptionStore:
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
                CREATE TABLE IF NOT EXISTS subscription_versions (
                    subscription_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (subscription_id, version)
                );
                CREATE TABLE IF NOT EXISTS subscription_heads (
                    subscription_id TEXT PRIMARY KEY,
                    latest_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscription_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    happened_at TEXT NOT NULL
                );
                """
            )

    def create_draft(
        self,
        scope: ConversationScope,
        spec: SubscriptionSpec,
        *,
        now: datetime,
    ) -> SubscriptionRecord:
        _require_aware(now)
        record = SubscriptionRecord(
            subscription_id=f"sub-{uuid4().hex}",
            version=1,
            status="draft",
            scope=scope,
            spec=spec,
            created_at=now,
            updated_at=now,
        )
        return self._append(record, action="create_draft", actor_user_id=scope.user_id)

    def get(self, subscription_id: str) -> SubscriptionRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT versions.record_json
                FROM subscription_heads AS heads
                JOIN subscription_versions AS versions
                  ON versions.subscription_id = heads.subscription_id
                 AND versions.version = heads.latest_version
                WHERE heads.subscription_id = ?
                """,
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise KeyError(subscription_id)
        return _record_from_json(str(row["record_json"]))

    def history(self, subscription_id: str) -> list[SubscriptionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM subscription_versions
                WHERE subscription_id = ? ORDER BY version
                """,
                (subscription_id,),
            ).fetchall()
        return [_record_from_json(str(row["record_json"])) for row in rows]

    def find_latest(
        self,
        scope: ConversationScope,
        *,
        statuses: tuple[SubscriptionStatus, ...] | None = None,
        allow_group_admin: bool = False,
    ) -> SubscriptionRecord | None:
        """Find the latest subscription visible to this exact conversation scope."""
        candidates = self.find_all(
            scope,
            statuses=statuses,
            allow_group_admin=allow_group_admin,
        )
        return candidates[0] if candidates else None

    def find_all(
        self,
        scope: ConversationScope,
        *,
        statuses: tuple[SubscriptionStatus, ...] | None = None,
        allow_group_admin: bool = False,
    ) -> list[SubscriptionRecord]:
        """Find all subscriptions visible to this conversation scope, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT versions.record_json
                FROM subscription_heads AS heads
                JOIN subscription_versions AS versions
                  ON versions.subscription_id = heads.subscription_id
                 AND versions.version = heads.latest_version
                """
            ).fetchall()
        candidates: list[SubscriptionRecord] = []
        for row in rows:
            record = _record_from_json(str(row["record_json"]))
            if statuses is not None and record.status not in statuses:
                continue
            try:
                self._require_same_scope(
                    record,
                    scope,
                    allow_group_admin=allow_group_admin,
                )
            except SubscriptionScopeMismatch:
                continue
            candidates.append(record)
        return sorted(
            candidates,
            key=lambda item: (item.updated_at, item.version),
            reverse=True,
        )

    def request_activation(
        self,
        subscription_id: str,
        scope: ConversationScope,
        *,
        explicit_confirmation: bool,
        actor_is_admin: bool,
        now: datetime,
    ) -> SubscriptionRecord:
        del actor_is_admin  # first confirmation never grants group send authority
        current = self.get(subscription_id)
        self._require_same_scope(current, scope)
        _require_explicit(explicit_confirmation)
        if current.status == "pending_confirmation":
            return current
        if current.status != "draft":
            raise ValueError(f"cannot request activation from {current.status}")
        return self._next_version(
            current,
            status="pending_confirmation",
            now=now,
            action="request_activation",
            actor_user_id=scope.user_id,
        )

    def confirm(
        self,
        subscription_id: str,
        scope: ConversationScope,
        *,
        explicit_confirmation: bool,
        actor_is_admin: bool,
        now: datetime,
    ) -> SubscriptionRecord:
        current = self.get(subscription_id)
        self._require_same_scope(
            current,
            scope,
            allow_group_admin=actor_is_admin,
        )
        _require_explicit(explicit_confirmation)
        if current.status == "active":
            return current
        if current.scope.chat_type == "group":
            if current.status != "pending_confirmation" or not actor_is_admin:
                raise PermissionError("group activation requires pending state and administrator confirmation")
        elif current.status not in {"draft", "pending_confirmation"}:
            raise ValueError(f"cannot confirm from {current.status}")
        return self._next_version(
            current,
            status="active",
            now=now,
            action="activate",
            activated_at=now,
            backfill_from=None,
            confirmed_by_user_id=scope.user_id,
            actor_user_id=scope.user_id,
        )

    def update_spec(
        self,
        subscription_id: str,
        scope: ConversationScope,
        spec: SubscriptionSpec,
        *,
        now: datetime,
    ) -> SubscriptionRecord:
        current = self.get(subscription_id)
        self._require_same_scope(current, scope)
        if current.status in {"cancelled", "paused"}:
            raise ValueError(f"cannot update {current.status} subscription")
        return self._next_version(
            current,
            status=current.status,
            now=now,
            action="update_spec",
            spec=spec,
            actor_user_id=scope.user_id,
        )

    def cancel(
        self,
        subscription_id: str,
        scope: ConversationScope,
        *,
        now: datetime,
    ) -> SubscriptionRecord:
        current = self.get(subscription_id)
        self._require_same_scope(current, scope)
        if current.status == "cancelled":
            return current
        return self._next_version(
            current,
            status="cancelled",
            now=now,
            action="cancel",
            actor_user_id=scope.user_id,
        )

    def _next_version(
        self,
        current: SubscriptionRecord,
        *,
        status: SubscriptionStatus,
        now: datetime,
        action: str,
        spec: SubscriptionSpec | None = None,
        activated_at: datetime | None | object = ...,
        backfill_from: datetime | None | object = ...,
        confirmed_by_user_id: str | None | object = ...,
        actor_user_id: str,
    ) -> SubscriptionRecord:
        _require_aware(now)
        next_record = SubscriptionRecord(
            subscription_id=current.subscription_id,
            version=current.version + 1,
            status=status,
            scope=current.scope,
            spec=spec or current.spec,
            created_at=current.created_at,
            updated_at=now,
            activated_at=(
                current.activated_at if activated_at is ... else activated_at
            ),
            backfill_from=(
                current.backfill_from if backfill_from is ... else backfill_from
            ),
            confirmed_by_user_id=(
                current.confirmed_by_user_id
                if confirmed_by_user_id is ...
                else confirmed_by_user_id
            ),
        )
        return self._append(
            next_record,
            action=action,
            actor_user_id=actor_user_id,
        )

    def _append(
        self,
        record: SubscriptionRecord,
        *,
        action: str,
        actor_user_id: str,
    ) -> SubscriptionRecord:
        payload = _record_to_json(record)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO subscription_versions VALUES (?, ?, ?)",
                (record.subscription_id, record.version, payload),
            )
            connection.execute(
                """
                INSERT INTO subscription_heads(subscription_id, latest_version)
                VALUES (?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET latest_version=excluded.latest_version
                """,
                (record.subscription_id, record.version),
            )
            connection.execute(
                """
                INSERT INTO subscription_audit(
                    subscription_id, version, action, actor_user_id, happened_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.subscription_id,
                    record.version,
                    action,
                    actor_user_id,
                    record.updated_at.isoformat(),
                ),
            )
        return record

    @staticmethod
    def _require_same_scope(
        record: SubscriptionRecord,
        scope: ConversationScope,
        *,
        allow_group_admin: bool = False,
    ) -> None:
        if record.scope == scope:
            return
        same_group_scope = (
            allow_group_admin
            and record.scope.chat_type == "group"
            and scope.chat_type == "group"
            and record.scope.bot_role == scope.bot_role
            and record.scope.chat_id == scope.chat_id
            and record.scope.thread_id == scope.thread_id
        )
        if not same_group_scope:
            raise SubscriptionScopeMismatch("subscription confirmation scope mismatch")


def _record_to_json(record: SubscriptionRecord) -> str:
    payload = asdict(record)
    for field in ("created_at", "updated_at", "activated_at", "backfill_from"):
        value = payload[field]
        payload[field] = value.isoformat() if value is not None else None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _record_from_json(payload: str) -> SubscriptionRecord:
    data = json.loads(payload)
    return SubscriptionRecord(
        subscription_id=data["subscription_id"],
        version=int(data["version"]),
        status=data["status"],
        scope=ConversationScope(**data["scope"]),
        spec=SubscriptionSpec(
            **{
                **data["spec"],
                "regions": tuple(data["spec"]["regions"]),
            }
        ),
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        activated_at=(datetime.fromisoformat(data["activated_at"]) if data["activated_at"] else None),
        backfill_from=(datetime.fromisoformat(data["backfill_from"]) if data["backfill_from"] else None),
        confirmed_by_user_id=data.get("confirmed_by_user_id"),
    )


def _require_explicit(value: bool) -> None:
    if not value:
        raise ValueError("explicit subscription confirmation is required")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("subscription timestamps must be timezone-aware")


__all__ = [
    "ConversationScope",
    "SubscriptionRecord",
    "SubscriptionScopeMismatch",
    "SubscriptionSpec",
    "SubscriptionStore",
]
