from __future__ import annotations

import sqlite3

from services.weather_bot.admin_actions import AdminActionLedger


def test_admin_idempotency_ledger_never_persists_external_weather_response(tmp_path) -> None:
    ledger = AdminActionLedger(
        tmp_path / "admin-actions.db",
        actor_id="weather-ops",
        role="administrator",
    )
    action = "weather.publish"
    key = "weather-publish-20260810-001"
    fingerprint = "f" * 64
    claim = ledger.claim(action, key, fingerprint, now=100.0)
    response = {
        "status": "published",
        "submission": {
            "task_id": "WEATHER-CN-440300-20260810-DAYAHEAD-001",
            "region": "sensitive-external-region",
            "provider_results": [
                {
                    "provider": "third-party-weather",
                    "points": [{"time": "2026-08-10T00:00:00+08:00", "temperature": 31.2}],
                }
            ],
        },
        "card": {"body": "full external forecast"},
        "delivery": {"sent": True, "reason": "allowed"},
    }

    ledger.complete(action, key, claim.owner_token or "", response, now=101.0)

    with sqlite3.connect(tmp_path / "admin-actions.db") as connection:
        stored_response = connection.execute(
            "SELECT response_json FROM admin_actions WHERE action = ?",
            (action,),
        ).fetchone()[0]
    assert "sensitive-external-region" not in stored_response
    assert "third-party-weather" not in stored_response
    assert "full external forecast" not in stored_response
    replay = ledger.claim(action, key, fingerprint, now=102.0)
    assert replay.status == "replay"
    assert replay.response == {
        "status": "published",
        "task_id": "WEATHER-CN-440300-20260810-DAYAHEAD-001",
        "delivery": {"sent": True, "reason": "allowed"},
        "idempotent_replay": True,
    }


def test_admin_ledger_invalidates_legacy_full_response_rows_on_expand_migration(tmp_path) -> None:
    database = tmp_path / "legacy-admin-actions.db"
    action = "weather.publish"
    key = "weather-publish-legacy-001"
    fingerprint = "a" * 64
    from hashlib import sha256

    key_hash = sha256(key.encode("utf-8")).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE admin_actions(
                action TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                send_uuid TEXT NOT NULL,
                response_json TEXT,
                lease_until REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(action, key_hash)
            );
            CREATE TABLE admin_action_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                role TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO admin_actions(
                action, key_hash, request_fingerprint, state, owner_token,
                send_uuid, response_json, lease_until, created_at, updated_at
            ) VALUES (?, ?, ?, 'succeeded', 'old-owner', 'old-uuid', ?, 0, 1, 1)
            """,
            (action, key_hash, fingerprint, '{"submission":"legacy-sensitive-weather"}'),
        )

    ledger = AdminActionLedger(database, actor_id="weather-ops", role="administrator")
    claim = ledger.claim(action, key, fingerprint, now=100.0)

    assert claim.status == "claimed"
    with sqlite3.connect(database) as connection:
        response_json, schema_version = connection.execute(
            "SELECT response_json, response_schema_version FROM admin_actions"
        ).fetchone()
    assert response_json is None
    assert schema_version == 2
