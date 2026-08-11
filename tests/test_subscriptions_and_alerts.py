from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from services.weather_bot.alerts import AlertEngine, AlertObservation, AlertOutbox
from services.weather_bot.subscriptions import (
    ConversationScope,
    SubscriptionScopeMismatch,
    SubscriptionSpec,
    SubscriptionStore,
)


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 9, 8, 0, tzinfo=TZ)


def _scope(*, thread_id: str = "thread-a", chat_type: str = "p2p") -> ConversationScope:
    return ConversationScope(
        bot_role="weather_forecast_bot",
        chat_type=chat_type,
        chat_id="chat-a",
        thread_id=thread_id,
        user_id="user-a",
    )


def _admin_scope() -> ConversationScope:
    return ConversationScope(
        bot_role="weather_forecast_bot",
        chat_type="group",
        chat_id="chat-a",
        thread_id="thread-a",
        user_id="group-admin",
    )


def _threshold_spec(trigger: float = 38.0) -> SubscriptionSpec:
    return SubscriptionSpec(
        kind="threshold",
        regions=("广东",),
        metric="apparent_temperature",
        operator=">=",
        trigger_threshold=trigger,
        recovery_threshold=trigger - 2,
        consecutive_hits=2,
        cooldown_seconds=6 * 3600,
    )


def test_private_schedule_request_creates_draft_only(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")

    draft = store.create_draft(
        _scope(),
        SubscriptionSpec(
            kind="scheduled_briefing",
            regions=("山东", "河南", "河北"),
            schedule_time="08:30",
            timezone="Asia/Shanghai",
        ),
        now=NOW,
    )

    assert draft.status == "draft"
    assert draft.version == 1
    assert store.get(draft.subscription_id).status == "draft"


def test_confirmation_from_another_thread_cannot_activate_draft(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    draft = store.create_draft(_scope(), _threshold_spec(), now=NOW)

    with pytest.raises(SubscriptionScopeMismatch):
        store.confirm(
            draft.subscription_id,
            _scope(thread_id="thread-b"),
            explicit_confirmation=True,
            actor_is_admin=False,
            now=NOW,
        )

    assert store.get(draft.subscription_id).status == "draft"


def test_group_member_cannot_activate_but_admin_second_confirmation_can(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    group_scope = _scope(chat_type="group")
    draft = store.create_draft(group_scope, _threshold_spec(), now=NOW)

    pending = store.request_activation(
        draft.subscription_id,
        group_scope,
        explicit_confirmation=True,
        actor_is_admin=False,
        now=NOW,
    )
    assert pending.status == "pending_confirmation"

    active = store.confirm(
        draft.subscription_id,
        _admin_scope(),
        explicit_confirmation=True,
        actor_is_admin=True,
        now=NOW + timedelta(minutes=1),
    )
    assert active.status == "active"
    assert active.activated_at == NOW + timedelta(minutes=1)
    assert active.backfill_from is None
    assert active.confirmed_by_user_id == "group-admin"


def test_threshold_update_keeps_immutable_previous_version(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    draft = store.create_draft(_scope(), _threshold_spec(38), now=NOW)
    active = store.confirm(
        draft.subscription_id,
        _scope(),
        explicit_confirmation=True,
        actor_is_admin=False,
        now=NOW,
    )

    updated = store.update_spec(
        active.subscription_id,
        _scope(),
        _threshold_spec(39),
        now=NOW + timedelta(minutes=2),
    )

    assert updated.version == active.version + 1
    assert updated.spec.trigger_threshold == 39
    history = store.history(active.subscription_id)
    assert [item.spec.trigger_threshold for item in history] == [38, 38, 39]
    assert history[-1].status == "active"


def test_repeated_cancel_is_idempotent(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    draft = store.create_draft(_scope(), _threshold_spec(), now=NOW)

    first = store.cancel(draft.subscription_id, _scope(), now=NOW)
    second = store.cancel(draft.subscription_id, _scope(), now=NOW + timedelta(minutes=1))

    assert first.status == "cancelled"
    assert second == first
    assert len(store.history(draft.subscription_id)) == 2


def _active_rule(store: SubscriptionStore, spec: SubscriptionSpec | None = None):
    draft = store.create_draft(_scope(), spec or _threshold_spec(), now=NOW)
    return store.confirm(
        draft.subscription_id,
        _scope(),
        explicit_confirmation=True,
        actor_is_admin=False,
        now=NOW,
    )


def _observation(
    run: str,
    value: float,
    *,
    severity: str = "high",
    at: datetime = NOW,
    data_available: bool = True,
) -> AlertObservation:
    return AlertObservation(
        source_run_id=run,
        provenance_ref=f"forecast-run:{run}" if data_available else None,
        availability_status=("allowed_for_calculation" if data_available else "rejected"),
        observed_at=at,
        risk_window="2026-08-09T14:00:00+08:00/2026-08-09T18:00:00+08:00",
        value=value,
        severity=severity,
        data_available=data_available,
    )


def test_ungated_observation_cannot_enter_alert_evaluation(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(subscriptions)
    observation = AlertObservation(
        source_run_id="untrusted-run",
        provenance_ref=None,
        availability_status="rejected",
        observed_at=NOW,
        risk_window="2026-08-09T14:00:00+08:00/2026-08-09T18:00:00+08:00",
        value=999,
        severity="critical",
        data_available=True,
    )

    result = engine.evaluate(rule, observation, now=NOW)

    assert result.action == "data_unavailable"
    assert outbox.pending() == []


def test_consecutive_hits_create_exactly_one_idempotent_outbox_item(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(subscriptions)

    first = engine.evaluate(rule, _observation("run-1", 39), now=NOW)
    second = engine.evaluate(rule, _observation("run-2", 40), now=NOW + timedelta(minutes=5))
    third = engine.evaluate(rule, _observation("run-3", 41), now=NOW + timedelta(minutes=10))
    duplicate = engine.evaluate(rule, _observation("run-3", 41), now=NOW + timedelta(minutes=10))

    assert first.action == "pending_trigger"
    assert second.action == "triggered"
    assert third.action == "suppressed_active"
    assert duplicate.action == "duplicate_observation"
    assert len(outbox.pending()) == 1


def test_cooldown_blocks_repeat_but_severity_escalation_can_emit(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(subscriptions)
    engine.evaluate(rule, _observation("run-1", 39, severity="medium"), now=NOW)
    engine.evaluate(rule, _observation("run-2", 40, severity="medium"), now=NOW + timedelta(minutes=1))

    suppressed = engine.evaluate(
        rule,
        _observation("run-3", 40, severity="medium"),
        now=NOW + timedelta(minutes=2),
    )
    escalated = engine.evaluate(
        rule,
        _observation("run-4", 41, severity="critical"),
        now=NOW + timedelta(minutes=3),
    )

    assert suppressed.action == "suppressed_active"
    assert escalated.action == "escalated"
    assert len(outbox.pending()) == 2


def test_recovery_requires_consecutive_hits_and_is_emitted_once(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(subscriptions)
    engine.evaluate(rule, _observation("run-1", 39), now=NOW)
    engine.evaluate(rule, _observation("run-2", 40), now=NOW + timedelta(minutes=1))

    first = engine.evaluate(rule, _observation("run-3", 35), now=NOW + timedelta(hours=1))
    second = engine.evaluate(rule, _observation("run-4", 35), now=NOW + timedelta(hours=2))
    third = engine.evaluate(rule, _observation("run-5", 34), now=NOW + timedelta(hours=3))

    assert first.action == "pending_recovery"
    assert second.action == "recovered"
    assert third.action == "inactive"
    assert [item.kind for item in outbox.pending()] == ["trigger", "recovery"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send_enabled", "dry_run"),
    [(False, False), (True, True)],
)
async def test_send_kill_switch_and_dry_run_never_call_sender(
    tmp_path,
    send_enabled,
    dry_run,
) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(subscriptions)
    engine.evaluate(rule, _observation("run-1", 39), now=NOW)
    engine.evaluate(rule, _observation("run-2", 40), now=NOW + timedelta(minutes=1))
    calls: list[str] = []

    async def sender(item):
        calls.append(item.outbox_id)

    result = await outbox.deliver(
        sender,
        send_enabled=send_enabled,
        dry_run=dry_run,
        now=NOW + timedelta(minutes=2),
    )

    assert result.sent == 0
    assert calls == []
    assert len(outbox.pending()) == 1


@pytest.mark.asyncio
async def test_quiet_hours_leave_outbox_pending_without_calling_sender(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(
        subscriptions,
        replace(_threshold_spec(), quiet_hours_start="07:00", quiet_hours_end="09:00"),
    )
    engine.evaluate(rule, _observation("run-1", 39), now=NOW)
    engine.evaluate(rule, _observation("run-2", 40), now=NOW + timedelta(minutes=1))
    calls: list[str] = []

    async def sender(item):
        calls.append(item.outbox_id)

    result = await outbox.deliver(sender, send_enabled=True, dry_run=False, now=NOW)

    assert result.sent == 0
    assert result.reason == "policy_suppressed"
    assert calls == []
    assert len(outbox.pending()) == 1


@pytest.mark.asyncio
async def test_hourly_send_cap_leaves_excess_items_pending(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(
        subscriptions,
        replace(_threshold_spec(), max_sends_per_hour=1, max_sends_per_day=3),
    )
    engine.evaluate(rule, _observation("run-1", 39, severity="medium"), now=NOW)
    engine.evaluate(rule, _observation("run-2", 40, severity="medium"), now=NOW + timedelta(minutes=1))
    engine.evaluate(rule, _observation("run-3", 41, severity="critical"), now=NOW + timedelta(minutes=2))
    calls: list[str] = []

    async def sender(item):
        calls.append(item.outbox_id)

    result = await outbox.deliver(
        sender,
        send_enabled=True,
        dry_run=False,
        now=NOW + timedelta(minutes=3),
    )

    assert result.sent == 1
    assert result.pending == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_default_alert_policy_has_a_hard_daily_cap_of_three_messages(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(
        subscriptions,
        replace(
            _threshold_spec(),
            consecutive_hits=1,
            cooldown_seconds=1,
            max_sends_per_hour=10,
        ),
    )
    engine.evaluate(rule, _observation("run-1", 39, severity="medium"), now=NOW)
    engine.evaluate(
        rule,
        _observation("run-2", 40, severity="high"),
        now=NOW + timedelta(minutes=1),
    )
    engine.evaluate(
        rule,
        _observation("run-3", 40, severity="high"),
        now=NOW + timedelta(minutes=2),
    )
    engine.evaluate(
        rule,
        _observation("run-4", 35, severity="low"),
        now=NOW + timedelta(minutes=3),
    )
    calls: list[str] = []

    async def sender(item):
        calls.append(item.outbox_id)

    result = await outbox.deliver(
        sender,
        send_enabled=True,
        dry_run=False,
        now=NOW + timedelta(minutes=4),
    )

    assert result.sent == 3
    assert result.pending == 1
    assert len(calls) == 3


def test_total_external_data_failure_neither_alerts_nor_mutates_hit_state(tmp_path) -> None:
    subscriptions = SubscriptionStore(tmp_path / "subscriptions.db")
    outbox = AlertOutbox(tmp_path / "alerts.db")
    engine = AlertEngine(outbox)
    rule = _active_rule(subscriptions)

    unavailable = engine.evaluate(
        rule,
        _observation("failed-run", 999, data_available=False),
        now=NOW,
    )
    next_valid = engine.evaluate(rule, _observation("run-1", 39), now=NOW + timedelta(minutes=1))

    assert unavailable.action == "data_unavailable"
    assert next_valid.action == "pending_trigger"
    assert outbox.pending() == []
