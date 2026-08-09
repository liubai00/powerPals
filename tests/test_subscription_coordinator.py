from datetime import datetime, timedelta, timezone

import pytest

from services.weather_bot.subscription_runtime import SubscriptionCoordinator
from services.weather_bot.subscriptions import ConversationScope, SubscriptionStore


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 9, 8, 0, tzinfo=TZ)


def _scope(
    *,
    user_id: str = "user-a",
    thread_id: str = "thread-a",
    chat_type: str = "p2p",
    chat_id: str = "chat-a",
) -> ConversationScope:
    return ConversationScope(
        bot_role="weather_forecast_bot",
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
    )


def test_private_draft_then_explicit_confirmation_activates_without_sending(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)

    draft = coordinator.handle(
        "每天8:30给我看山东、河南和河北",
        _scope(),
        actor_is_admin=False,
        now=NOW,
    )
    active = coordinator.handle(
        "确认订阅",
        _scope(),
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1),
    )

    assert draft is not None and draft["status"] == "subscription_draft"
    assert active is not None and active["status"] == "subscription_active"
    assert active["send_performed"] is False


def test_other_thread_has_no_authority_over_draft(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)
    coordinator.handle(
        "广东体感温度超过38℃时提醒我",
        _scope(),
        actor_is_admin=False,
        now=NOW,
    )

    result = coordinator.handle(
        "确认订阅",
        _scope(thread_id="thread-b"),
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1),
    )

    assert result is not None
    assert result["status"] == "subscription_context_missing"
    assert store.find_latest(_scope()).status == "draft"


def test_group_requires_member_request_then_different_admin_confirmation(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)
    member_scope = _scope(chat_type="group")
    admin_scope = _scope(user_id="group-admin", chat_type="group")

    draft = coordinator.handle(
        "广东体感温度超过38℃时提醒我",
        member_scope,
        actor_is_admin=False,
        now=NOW,
    )
    pending = coordinator.handle(
        "确认订阅",
        member_scope,
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1),
    )
    active = coordinator.handle(
        "确认订阅",
        admin_scope,
        actor_is_admin=True,
        now=NOW + timedelta(minutes=2),
    )

    assert draft is not None and draft["status"] == "subscription_draft"
    assert pending is not None and pending["status"] == "subscription_pending_confirmation"
    assert active is not None and active["status"] == "subscription_active"
    assert active["subscription"]["confirmed_by_user_id"] == "group-admin"


def test_group_admin_must_disambiguate_concurrent_drafts_in_main_thread(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)
    member_a = _scope(user_id="user-a", thread_id="", chat_type="group")
    member_b = _scope(user_id="user-b", thread_id="", chat_type="group")
    admin = _scope(user_id="group-admin", thread_id="", chat_type="group")

    draft_a = coordinator.handle(
        "每天8:30给我看山东",
        member_a,
        actor_is_admin=False,
        now=NOW,
    )
    draft_b = coordinator.handle(
        "每天8:30给我看广东",
        member_b,
        actor_is_admin=False,
        now=NOW + timedelta(seconds=1),
    )
    coordinator.handle(
        "确认订阅",
        member_a,
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1),
    )
    coordinator.handle(
        "确认订阅",
        member_b,
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1, seconds=1),
    )

    result = coordinator.handle(
        "确认订阅",
        admin,
        actor_is_admin=True,
        now=NOW + timedelta(minutes=2),
    )

    assert result is not None
    assert result["status"] == "subscription_confirmation_ambiguous"
    assert result["clarification_required"] is True
    candidate_ids = {
        draft_a["subscription"]["subscription_id"],
        draft_b["subscription"]["subscription_id"],
    }
    assert set(result["candidate_subscription_ids"]) == candidate_ids
    assert all(candidate_id in result["text"] for candidate_id in candidate_ids)
    assert store.get(draft_a["subscription"]["subscription_id"]).status == "pending_confirmation"
    assert store.get(draft_b["subscription"]["subscription_id"]).status == "pending_confirmation"


def test_group_admin_explicit_draft_id_activates_only_target(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)
    member_a = _scope(user_id="user-a", thread_id="", chat_type="group")
    member_b = _scope(user_id="user-b", thread_id="", chat_type="group")
    admin = _scope(user_id="group-admin", thread_id="", chat_type="group")

    draft_a = coordinator.handle(
        "每天8:30给我看山东",
        member_a,
        actor_is_admin=False,
        now=NOW,
    )
    draft_b = coordinator.handle(
        "每天8:30给我看广东",
        member_b,
        actor_is_admin=False,
        now=NOW + timedelta(seconds=1),
    )
    coordinator.handle(
        "确认订阅",
        member_a,
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1),
    )
    coordinator.handle(
        "确认订阅",
        member_b,
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    target_id = draft_a["subscription"]["subscription_id"]

    result = coordinator.handle(
        f"确认订阅 {target_id}",
        admin,
        actor_is_admin=True,
        now=NOW + timedelta(minutes=2),
    )

    assert result is not None
    assert result["status"] == "subscription_active"
    assert result["subscription"]["subscription_id"] == target_id
    assert store.get(target_id).status == "active"
    assert store.get(draft_b["subscription"]["subscription_id"]).status == "pending_confirmation"


@pytest.mark.parametrize(
    "admin_scope",
    [
        _scope(user_id="group-admin", thread_id="thread-b", chat_type="group"),
        _scope(user_id="group-admin", thread_id="", chat_type="group", chat_id="chat-b"),
    ],
    ids=["different-thread", "different-chat"],
)
def test_group_admin_explicit_draft_id_cannot_cross_chat_or_thread(
    tmp_path,
    admin_scope,
) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)
    member = _scope(user_id="user-a", thread_id="", chat_type="group")
    draft = coordinator.handle(
        "每天8:30给我看山东",
        member,
        actor_is_admin=False,
        now=NOW,
    )
    coordinator.handle(
        "确认订阅",
        member,
        actor_is_admin=False,
        now=NOW + timedelta(minutes=1),
    )
    target_id = draft["subscription"]["subscription_id"]

    result = coordinator.handle(
        f"确认订阅 {target_id}",
        admin_scope,
        actor_is_admin=True,
        now=NOW + timedelta(minutes=2),
    )

    assert result is not None
    assert result["status"] == "subscription_context_missing"
    assert store.get(target_id).status == "pending_confirmation"


def test_vague_acknowledgement_never_changes_subscription_state(tmp_path) -> None:
    store = SubscriptionStore(tmp_path / "subscriptions.db")
    coordinator = SubscriptionCoordinator(store)
    coordinator.handle(
        "每天8:30给我看山东",
        _scope(),
        actor_is_admin=False,
        now=NOW,
    )

    assert coordinator.handle("可以", _scope(), actor_is_admin=False, now=NOW) is None
    assert store.find_latest(_scope()).status == "draft"
