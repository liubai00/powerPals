from __future__ import annotations

import json
import sqlite3

import pytest

from services.weather_bot import memory as weather_memory


def _create_v1_memory_database(path, *, now: float) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "CREATE TABLE conversation_state("
            "k TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL, "
            "expires_at REAL NOT NULL, state_version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO conversation_state(k,payload,updated_at,expires_at,state_version) "
            "VALUES(?,?,?,?,?)",
            (
                "weather|group|chat-a|thread-a|user-a",
                json.dumps(
                    {
                        "state_version": 1,
                        "last_successful_request": {"region": "广州", "days": 3},
                    },
                    ensure_ascii=False,
                ),
                now - 10,
                now + 600,
                1,
            ),
        )
        connection.execute("CREATE TABLE legacy_sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_sentinel(value) VALUES('keep-me')")


def test_v1_database_upgrades_in_place_and_keeps_old_conversation_readable(
    monkeypatch,
    tmp_path,
) -> None:
    now = 1_000_000.0
    database = tmp_path / "memory-v1.db"
    _create_v1_memory_database(database, now=now)
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    monkeypatch.setattr(weather_memory.time, "time", lambda: now)

    state = weather_memory.load_conversation_state(
        "weather|group|chat-a|thread-a|user-a"
    )

    assert state == {
        "state_version": 1,
        "last_successful_request": {"region": "广州", "days": 3},
    }
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(conversation_state)")
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert columns["intent_version"][3] == 0
        assert columns["entity_schema_version"][3] == 0
        assert connection.execute("SELECT value FROM legacy_sentinel").fetchone() == (
            "keep-me",
        )


def test_save_conversation_state_indexes_intent_and_entity_schema_versions(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "memory-v2.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    payload = {
        "state_version": 2,
        "intent_version": "intent-router-v3",
        "entity_schema_version": "weather-entities-v2",
        "last_successful_request": {"region": "深圳", "days": 7},
    }

    weather_memory.save_conversation_state("conversation-a", payload)

    assert weather_memory.load_conversation_state("conversation-a") == payload
    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT intent_version, entity_schema_version FROM conversation_state"
        ).fetchone()
    assert versions == ("intent-router-v3", "weather-entities-v2")


def test_v2_migration_is_repeatable_and_does_not_copy_independent_domain_tables(
    monkeypatch,
    tmp_path,
) -> None:
    now = 1_000_000.0
    database = tmp_path / "repeatable-memory-v1.db"
    _create_v1_memory_database(database, now=now)
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    monkeypatch.setattr(weather_memory.time, "time", lambda: now)

    assert weather_memory.load_conversation_state(
        "weather|group|chat-a|thread-a|user-a"
    )
    assert weather_memory.load_conversation_state(
        "weather|group|chat-a|thread-a|user-a"
    )

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        sentinel = connection.execute("SELECT value FROM legacy_sentinel").fetchone()
    assert {"bot_reply_markers", "send_audit"}.issubset(tables)
    assert {
        "subscriptions",
        "alert_rules",
        "alert_state",
        "notification_outbox",
    }.isdisjoint(tables)
    assert sentinel == ("keep-me",)


def test_v2_migration_removes_unscoped_legacy_bot_reply_markers(
    monkeypatch,
    tmp_path,
) -> None:
    now = 1_000_000.0
    database = tmp_path / "legacy-marker-memory-v1.db"
    _create_v1_memory_database(database, now=now)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO conversation_state(k,payload,updated_at,expires_at,state_version) "
            "VALUES(?,?,?,?,?)",
            (
                "bot-reply|raw-chat-id|raw-message-id",
                json.dumps(
                    {
                        "source": "recorded_bot_reply",
                        "chat_id": "raw-chat-id",
                        "message_id": "raw-message-id",
                        "bot_role": "weather_forecast_bot",
                    }
                ),
                now - 10,
                now + 600,
                1,
            ),
        )
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    monkeypatch.setattr(weather_memory.time, "time", lambda: now)

    assert weather_memory.load_conversation_state(
        "weather|group|chat-a|thread-a|user-a"
    )

    with sqlite3.connect(database) as connection:
        legacy_marker_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_state WHERE k LIKE 'bot-reply|%'"
        ).fetchone()[0]
    assert legacy_marker_count == 0


def test_bot_reply_marker_is_ttl_bounded_and_isolated_by_full_conversation_scope(
    monkeypatch,
    tmp_path,
) -> None:
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "markers.db"))
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    scope = {
        "bot_role": "weather_forecast_bot",
        "chat_type": "group",
        "chat_id": "chat-a",
        "thread_id": "thread-a",
        "user_id": "user-a",
        "message_id": "message-a",
    }

    weather_memory.save_bot_reply_marker(**scope, ttl_seconds=30)

    assert weather_memory.load_bot_reply_marker(**scope) == {
        "source": "recorded_bot_reply",
        **scope,
    }
    for field, different in (
        ("bot_role", "weather_task_bot"),
        ("chat_type", "p2p"),
        ("chat_id", "chat-b"),
        ("thread_id", "thread-b"),
        ("user_id", "user-b"),
        ("message_id", "message-b"),
    ):
        other_scope = {**scope, field: different}
        assert weather_memory.load_bot_reply_marker(**other_scope) is None

    clock["now"] += 31
    assert weather_memory.load_bot_reply_marker(**scope) is None


def test_expired_bot_reply_marker_is_physically_removed_after_ttl(
    monkeypatch,
    tmp_path,
) -> None:
    clock = {"now": 1_000_000.0}
    database = tmp_path / "append-only-markers.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    scope = {
        "bot_role": "weather_forecast_bot",
        "chat_type": "group",
        "chat_id": "chat-a",
        "thread_id": "thread-a",
        "user_id": "user-a",
        "message_id": "message-a",
    }
    weather_memory.save_bot_reply_marker(**scope, ttl_seconds=30)

    clock["now"] += 31

    assert weather_memory.load_bot_reply_marker(**scope) is None
    with sqlite3.connect(database) as connection:
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM bot_reply_markers"
        ).fetchone()[0]
    assert marker_count == 0


def test_send_audit_appends_status_only_and_hashes_delivery_identifiers(
    monkeypatch,
    tmp_path,
) -> None:
    clock = {"now": 1_000_000.0}
    database = tmp_path / "send-audit.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    delivery = {
        "send_id": "om_sensitive_message_identifier",
        "bot_role": "weather_forecast_bot",
        "chat_type": "group",
        "chat_id": "oc_sensitive_chat_identifier",
        "thread_id": "omt_sensitive_thread_identifier",
        "user_id": "ou_sensitive_user_identifier",
    }

    weather_memory.record_send_audit(
        **delivery,
        status="attempted",
    )
    clock["now"] += 1
    weather_memory.record_send_audit(
        **delivery,
        status="sent",
    )

    assert weather_memory.load_send_audit(**delivery) == [
        {
            "status": "attempted",
            "created_at": 1_000_000.0,
        },
        {
            "status": "sent",
            "created_at": 1_000_001.0,
        },
    ]
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT send_id_hash,scope_hash,status FROM send_audit ORDER BY audit_id"
        ).fetchall()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(send_audit)")
        }
    assert len(rows) == 2
    assert all(len(send_hash) == 64 and len(scope_hash) == 64 for send_hash, scope_hash, _ in rows)
    assert {
        "message",
        "payload",
        "token",
        "reason_code",
        "chat_id",
        "thread_id",
        "user_id",
    }.isdisjoint(columns)
    database_bytes = database.read_bytes()
    for plaintext_identifier in delivery.values():
        if plaintext_identifier != delivery["bot_role"]:
            assert plaintext_identifier.encode("utf-8") not in database_bytes
    with pytest.raises(TypeError):
        weather_memory.record_send_audit(
            **delivery,
            status="failed",
            message="用户消息正文不允许进入审计",
        )


def test_send_audit_default_retention_purges_rows_older_than_ninety_days(
    monkeypatch,
    tmp_path,
) -> None:
    clock = {"now": 1_000_000.0}
    database = tmp_path / "send-audit-retention.db"
    monkeypatch.setattr(weather_memory, "DB_PATH", str(database))
    monkeypatch.setattr(weather_memory.time, "time", lambda: clock["now"])
    delivery = {
        "send_id": "send-a",
        "bot_role": "weather_forecast_bot",
        "chat_type": "group",
        "chat_id": "chat-a",
        "thread_id": "thread-a",
        "user_id": "user-a",
    }
    weather_memory.record_send_audit(**delivery, status="attempted")
    clock["now"] += 89 * 24 * 3600
    weather_memory.record_send_audit(**delivery, status="sent")
    clock["now"] += 2 * 24 * 3600
    expected_retained_status = [
        {
            "status": "sent",
            "created_at": 1_000_000.0 + 89 * 24 * 3600,
        }
    ]

    assert weather_memory.load_send_audit(**delivery) == expected_retained_status
    removed = weather_memory.purge_expired_send_audit()

    assert removed == 1
    assert weather_memory.load_send_audit(**delivery) == expected_retained_status
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM send_audit").fetchone()[0] == 1
