"""Side-effect-free subscription command coordinator for event/API adapters."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from services.weather_bot.subscription_commands import parse_subscription_command
from services.weather_bot.subscriptions import (
    ConversationScope,
    SubscriptionRecord,
    SubscriptionScopeMismatch,
    SubscriptionStore,
)


class SubscriptionCoordinator:
    def __init__(self, store: SubscriptionStore) -> None:
        self.store = store

    def handle(
        self,
        text: str,
        scope: ConversationScope,
        *,
        actor_is_admin: bool,
        now: datetime,
    ) -> dict[str, Any] | None:
        command = parse_subscription_command(text)
        if command is None:
            return None

        if command.action == "create_draft" and command.spec is not None:
            record = self.store.create_draft(scope, command.spec, now=now)
            return _result(
                "subscription_draft",
                record,
                "已生成订阅草稿，尚未启用。请使用“确认订阅”明确确认；当前不会发送任何消息。",
            )

        if command.action == "confirm":
            allow_admin_lookup = scope.chat_type == "group" and actor_is_admin
            if command.subscription_id:
                try:
                    current = self.store.get(command.subscription_id)
                except KeyError:
                    current = None
            elif allow_admin_lookup:
                candidates = self.store.find_all(
                    scope,
                    statuses=("draft", "pending_confirmation"),
                    allow_group_admin=True,
                )
                if len(candidates) > 1:
                    candidate_ids = [item.subscription_id for item in candidates]
                    return {
                        "status": "subscription_confirmation_ambiguous",
                        "clarification_required": True,
                        "send_performed": False,
                        "candidate_subscription_ids": candidate_ids,
                        "text": (
                            "当前群和线程中有多个待确认订阅，不能猜测确认目标。"
                            "请发送“确认订阅 <草稿标识>”：\n"
                            + "\n".join(f"- {candidate_id}" for candidate_id in candidate_ids)
                        ),
                    }
                current = candidates[0] if candidates else self.store.find_latest(
                    scope,
                    statuses=("active",),
                    allow_group_admin=True,
                )
            else:
                current = self.store.find_latest(
                    scope,
                    statuses=("draft", "pending_confirmation", "active"),
                )
            if current is None:
                return {
                    "status": "subscription_context_missing",
                    "send_performed": False,
                    "text": "当前群、线程、用户和机器人下没有可确认的订阅草稿。",
                }
            if scope.chat_type == "group" and not actor_is_admin:
                try:
                    record = self.store.request_activation(
                        current.subscription_id,
                        scope,
                        explicit_confirmation=command.explicit_confirmation,
                        actor_is_admin=False,
                        now=now,
                    )
                except SubscriptionScopeMismatch:
                    return _missing_context()
                return _result(
                    "subscription_pending_confirmation",
                    record,
                    "已记录成员确认，仍未启用；需本群审核管理员在同一线程再次发送“确认订阅”。",
                )
            try:
                record = self.store.confirm(
                    current.subscription_id,
                    scope,
                    explicit_confirmation=command.explicit_confirmation,
                    actor_is_admin=actor_is_admin,
                    now=now,
                )
            except SubscriptionScopeMismatch:
                return _missing_context()
            except PermissionError:
                return {
                    "status": "subscription_pending_confirmation",
                    "send_performed": False,
                    "text": "群订阅尚未完成管理员二次确认，当前不会发送任何消息。",
                }
            return _result(
                "subscription_active",
                record,
                "订阅已审核启用；不会补发启用前的历史预警。实际通知仍受全局发送和告警开关控制。",
            )

        if command.action == "update_threshold" and command.new_threshold is not None:
            current = self.store.find_latest(
                scope,
                statuses=("draft", "pending_confirmation", "active"),
            )
            if current is None or current.spec.kind != "threshold":
                return _missing_context()
            old_trigger = float(current.spec.trigger_threshold)
            old_recovery = float(current.spec.recovery_threshold)
            hysteresis = abs(old_trigger - old_recovery)
            increasing_rule = current.spec.operator in {">", ">="}
            recovery = (
                command.new_threshold - hysteresis
                if increasing_rule
                else command.new_threshold + hysteresis
            )
            record = self.store.update_spec(
                current.subscription_id,
                scope,
                replace(
                    current.spec,
                    trigger_threshold=command.new_threshold,
                    recovery_threshold=recovery,
                ),
                now=now,
            )
            return _result(
                "subscription_updated",
                record,
                "订阅阈值已生成新版本；旧版本保留在审计记录中。",
            )

        if command.action == "cancel":
            current = self.store.find_latest(scope)
            if current is None:
                return _missing_context()
            record = self.store.cancel(current.subscription_id, scope, now=now)
            return _result(
                "subscription_cancelled",
                record,
                "订阅已取消；重复取消不会产生额外动作。",
            )
        return None


def _missing_context() -> dict[str, Any]:
    return {
        "status": "subscription_context_missing",
        "send_performed": False,
        "text": "当前会话范围内没有可操作的订阅。",
    }


def _result(status: str, record: SubscriptionRecord, text: str) -> dict[str, Any]:
    payload = asdict(record)
    for field in ("created_at", "updated_at", "activated_at", "backfill_from"):
        value = payload[field]
        payload[field] = value.isoformat() if value is not None else None
    return {
        "status": status,
        "send_performed": False,
        "subscription": payload,
        "text": text,
    }


__all__ = ["SubscriptionCoordinator"]
