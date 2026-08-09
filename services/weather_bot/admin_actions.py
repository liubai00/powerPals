"""Persistent idempotency and privacy-minimized audit for admin mutations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class InvalidIdempotencyKey(ValueError):
    pass


@dataclass(frozen=True)
class AdminActionClaim:
    status: Literal["claimed", "replay", "conflict", "in_progress"]
    owner_token: str | None
    send_uuid: str
    response: dict[str, object] | None = None


class AdminActionLedger:
    """Atomically claim and audit one external admin action.

    The caller-supplied key and request body are never persisted.  Only their
    SHA-256 digests, an internal owner token and a minimal replay receipt are
    retained.  A stable Feishu UUID survives retries after an expired lease.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        actor_id: str,
        role: str,
        lease_seconds: int = 300,
        retention_days: int = 90,
    ) -> None:
        self.path = Path(path)
        self.actor_id = actor_id
        self.role = role
        self.lease_seconds = max(1, int(lease_seconds))
        self.retention_days = max(1, int(retention_days))

    def claim(
        self,
        action: str,
        idempotency_key: str,
        request_fingerprint: str,
        *,
        now: float | None = None,
    ) -> AdminActionClaim:
        key = (idempotency_key or "").strip()
        if not _KEY_PATTERN.fullmatch(key):
            raise InvalidIdempotencyKey(
                "Idempotency-Key must be 8-128 opaque ASCII characters"
            )
        timestamp = float(time.time() if now is None else now)
        key_hash = _digest(key)
        send_uuid = str(uuid5(NAMESPACE_URL, f"weather-admin:{action}:{key_hash}"))
        owner_token = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM admin_actions WHERE action = ? AND key_hash = ?",
                (action, key_hash),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO admin_actions(
                        action, key_hash, request_fingerprint, state, owner_token,
                        send_uuid, response_json, response_schema_version,
                        lease_until, created_at, updated_at
                    ) VALUES (?, ?, ?, 'in_progress', ?, ?, NULL, 2, ?, ?, ?)
                    """,
                    (
                        action,
                        key_hash,
                        request_fingerprint,
                        owner_token,
                        send_uuid,
                        timestamp + self.lease_seconds,
                        timestamp,
                        timestamp,
                    ),
                )
                self._audit(connection, action, key_hash, "claimed", timestamp)
                return AdminActionClaim("claimed", owner_token, send_uuid)

            persisted_send_uuid = str(row["send_uuid"] or send_uuid)
            if str(row["request_fingerprint"]) != request_fingerprint:
                self._audit(connection, action, key_hash, "conflict", timestamp)
                return AdminActionClaim("conflict", None, persisted_send_uuid)
            if str(row["state"]) == "succeeded" and row["response_json"]:
                response = json.loads(str(row["response_json"]))
                self._audit(connection, action, key_hash, "replayed", timestamp)
                return AdminActionClaim(
                    "replay",
                    None,
                    persisted_send_uuid,
                    response if isinstance(response, dict) else None,
                )
            if str(row["state"]) == "in_progress" and float(row["lease_until"]) > timestamp:
                self._audit(connection, action, key_hash, "in_progress", timestamp)
                return AdminActionClaim("in_progress", None, persisted_send_uuid)

            connection.execute(
                """
                UPDATE admin_actions
                SET state='in_progress', owner_token=?, response_json=NULL,
                    response_schema_version=2, lease_until=?, updated_at=?
                WHERE action=? AND key_hash=?
                """,
                (
                    owner_token,
                    timestamp + self.lease_seconds,
                    timestamp,
                    action,
                    key_hash,
                ),
            )
            self._audit(connection, action, key_hash, "reclaimed", timestamp)
            return AdminActionClaim("claimed", owner_token, persisted_send_uuid)

    def complete(
        self,
        action: str,
        idempotency_key: str,
        owner_token: str,
        response: dict[str, object],
        *,
        now: float | None = None,
    ) -> None:
        timestamp = float(time.time() if now is None else now)
        key_hash = _digest(idempotency_key.strip())
        serialized = json.dumps(
            _minimal_replay_receipt(response),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE admin_actions
                SET state='succeeded', response_json=?, response_schema_version=2,
                    lease_until=0, updated_at=?
                WHERE action=? AND key_hash=? AND owner_token=? AND state='in_progress'
                """,
                (serialized, timestamp, action, key_hash, owner_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("admin action lease no longer owned")
            self._audit(connection, action, key_hash, "succeeded", timestamp)

    def fail(
        self,
        action: str,
        idempotency_key: str,
        owner_token: str,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = float(time.time() if now is None else now)
        key_hash = _digest(idempotency_key.strip())
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE admin_actions
                SET state='failed', response_json=NULL, lease_until=0, updated_at=?
                WHERE action=? AND key_hash=? AND owner_token=? AND state='in_progress'
                """,
                (timestamp, action, key_hash, owner_token),
            )
            self._audit(connection, action, key_hash, "failed", timestamp)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_actions(
                action TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                send_uuid TEXT NOT NULL,
                response_json TEXT,
                response_schema_version INTEGER NOT NULL DEFAULT 2,
                lease_until REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(action, key_hash)
            );
            CREATE TABLE IF NOT EXISTS admin_action_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                role TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_admin_action_audit_created
            ON admin_action_audit(created_at);
            """
        )
        audit_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(admin_action_audit)").fetchall()
        }
        if "actor_id" not in audit_columns:
            connection.execute(
                "ALTER TABLE admin_action_audit ADD COLUMN actor_id TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "role" not in audit_columns:
            connection.execute(
                "ALTER TABLE admin_action_audit ADD COLUMN role TEXT NOT NULL DEFAULT 'unknown'"
            )
        action_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(admin_actions)").fetchall()
        }
        if "response_schema_version" not in action_columns:
            connection.execute(
                "ALTER TABLE admin_actions "
                "ADD COLUMN response_schema_version INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            """
            UPDATE admin_actions
            SET response_json=NULL, state='failed', lease_until=0,
                response_schema_version=2
            WHERE response_schema_version < 2
            """
        )
        connection.commit()
        return connection

    def _purge(self, connection: sqlite3.Connection, now: float) -> None:
        cutoff = now - (self.retention_days * 86400)
        connection.execute(
            "DELETE FROM admin_actions WHERE updated_at < ? AND state != 'in_progress'",
            (cutoff,),
        )
        connection.execute(
            "DELETE FROM admin_action_audit WHERE created_at < ?",
            (cutoff,),
        )

    def _audit(
        self,
        connection: sqlite3.Connection,
        action: str,
        key_hash: str,
        outcome: str,
        timestamp: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO admin_action_audit(
                action, key_hash, actor_id, role, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action, key_hash, self.actor_id, self.role, outcome, timestamp),
        )


def canonical_request_fingerprint(payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(serialized)


def _minimal_replay_receipt(response: dict[str, object]) -> dict[str, object]:
    """Keep idempotency durable without turning the audit DB into a data cache."""

    receipt: dict[str, object] = {"idempotent_replay": True}
    status = response.get("status")
    if isinstance(status, str) and status.strip():
        receipt["status"] = status.strip()[:64]

    task_id = response.get("task_id")
    if not isinstance(task_id, str):
        for container_name in ("submission", "task"):
            container = response.get(container_name)
            candidate = container.get("task_id") if isinstance(container, dict) else None
            if isinstance(candidate, str):
                task_id = candidate
                break
    if isinstance(task_id, str) and task_id.strip():
        receipt["task_id"] = task_id.strip()[:160]

    delivery = response.get("delivery")
    if isinstance(delivery, dict):
        minimized_delivery: dict[str, object] = {}
        sent = delivery.get("sent")
        reason = delivery.get("reason")
        if isinstance(sent, bool):
            minimized_delivery["sent"] = sent
        if isinstance(reason, str) and reason.strip():
            minimized_delivery["reason"] = reason.strip()[:80]
        if minimized_delivery:
            receipt["delivery"] = minimized_delivery
    return receipt


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "AdminActionClaim",
    "AdminActionLedger",
    "InvalidIdempotencyKey",
    "canonical_request_fingerprint",
]
