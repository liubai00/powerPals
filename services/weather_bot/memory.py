"""轻量长期记忆: SQLite 落盘(data/memory.db, 挂载在容器外)。

L1 用户画像: 每人常查城市/天数; L2 对话记忆: 最近几轮, TTL 7 天。
所有读写由调用方 try 包裹, 失败降级为无记忆。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
from typing import Any, Iterator

DB_PATH = os.getenv("WEATHER_MEMORY_DB", "data/memory.db")
TURN_TTL_SECONDS = 7 * 24 * 3600
TURN_MAX_PER_KEY = 12
STATE_TTL_SECONDS = 7 * 24 * 3600
EVENT_TTL_SECONDS = 7 * 24 * 3600
# 全国晨报的单次生成/等待上限是 600 秒。事件处理租期必须覆盖该窗口，
# 否则飞书重投同一 event_id 时可能产生第二个回复处理者。
EVENT_PROCESSING_TIMEOUT_SECONDS = 15 * 60


def _conn() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS turns(k TEXT, role TEXT, content TEXT, ts REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_k_ts ON turns(k, ts)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_pref("
        "bot_role TEXT, sender_id TEXT, region TEXT, days INTEGER, ts REAL, hits INTEGER, "
        "PRIMARY KEY(bot_role, sender_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_state("
        "k TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL, "
        "expires_at REAL NOT NULL, state_version INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS event_ledger("
        "bot_scope TEXT NOT NULL, event_id TEXT NOT NULL, status TEXT NOT NULL, "
        "response TEXT, updated_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "PRIMARY KEY(bot_scope, event_id))"
    )
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = _conn()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def record_turn(key: str, role: str, content: str) -> None:
    now = time.time()
    with _db() as conn:
        conn.execute("INSERT INTO turns VALUES(?,?,?,?)", (key, role, (content or "")[:400], now))
        conn.execute("DELETE FROM turns WHERE ts < ?", (now - TURN_TTL_SECONDS,))
        conn.execute(
            "DELETE FROM turns WHERE rowid IN ("
            "SELECT rowid FROM turns WHERE k=? ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (key, TURN_MAX_PER_KEY),
        )


def recent_turns(key: str) -> list[dict[str, str]]:
    now = time.time()
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM turns WHERE k=? AND ts>=? ORDER BY ts",
            (key, now - TURN_TTL_SECONDS),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in rows]


def remember_query(bot_role: str, sender_id: str, region: str | None, days: int) -> None:
    if not sender_id or not region:
        return
    with _db() as conn:
        conn.execute(
            "INSERT INTO user_pref VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(bot_role, sender_id) DO UPDATE SET "
            "region=excluded.region, days=excluded.days, ts=excluded.ts, hits=user_pref.hits+1",
            (bot_role, sender_id, region, max(1, int(days or 1)), time.time()),
        )


def preferred_region(bot_role: str, sender_id: str) -> dict | None:
    if not sender_id:
        return None
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT region, days, hits FROM user_pref "
            "WHERE bot_role=? AND sender_id=? AND ts>=?",
            (bot_role, sender_id, now - STATE_TTL_SECONDS),
        ).fetchone()
    if not row:
        return None
    return {"region": row[0], "days": row[1], "hits": row[2]}


def recent_chat_turns(key_prefix: str, limit: int = 12) -> list[dict[str, str]]:
    """按前缀(同话题跨发言人)取最近对话。chat_id/thread_id 含下划线是 LIKE 通配符, 需转义。"""
    now = time.time()
    escaped = key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content, ts FROM turns WHERE k LIKE ? ESCAPE '\\' AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (escaped + "%", now - TURN_TTL_SECONDS, limit),
        ).fetchall()
    return [{"role": role, "content": content} for role, content, _ts in reversed(rows)]


def load_conversation_state(key: str) -> dict[str, Any] | None:
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT payload FROM conversation_state WHERE k=? AND expires_at>=?",
            (key, now),
        ).fetchone()
        conn.execute("DELETE FROM conversation_state WHERE expires_at<?", (now,))
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def save_conversation_state(
    key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int = STATE_TTL_SECONDS,
) -> None:
    now = time.time()
    state_version = max(1, int(payload.get("state_version") or 1))
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    with _db() as conn:
        conn.execute(
            "INSERT INTO conversation_state(k,payload,updated_at,expires_at,state_version) "
            "VALUES(?,?,?,?,?) ON CONFLICT(k) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at, "
            "expires_at=excluded.expires_at, state_version=excluded.state_version",
            (key, encoded, now, now + max(1, ttl_seconds), state_version),
        )


def clear_conversation_state(key: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM conversation_state WHERE k=?", (key,))


def claim_event(bot_scope: str, event_id: str) -> bool:
    """Atomically claim an event.

    Completed events and fresh in-flight events are duplicates. Failed events
    and stale in-flight events are retryable.
    """
    if not event_id:
        return True
    now = time.time()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM event_ledger WHERE expires_at<?", (now,))
        row = conn.execute(
            "SELECT status, updated_at FROM event_ledger WHERE bot_scope=? AND event_id=?",
            (bot_scope, event_id),
        ).fetchone()
        if row:
            status, updated_at = row
            if status == "succeeded":
                return False
            if status == "processing" and float(updated_at) >= now - EVENT_PROCESSING_TIMEOUT_SECONDS:
                return False
            conn.execute(
                "UPDATE event_ledger SET status='processing', response=NULL, "
                "updated_at=?, expires_at=? WHERE bot_scope=? AND event_id=?",
                (now, now + EVENT_TTL_SECONDS, bot_scope, event_id),
            )
            return True
        conn.execute(
            "INSERT INTO event_ledger(bot_scope,event_id,status,response,updated_at,expires_at) "
            "VALUES(?,?,'processing',NULL,?,?)",
            (bot_scope, event_id, now, now + EVENT_TTL_SECONDS),
        )
    return True


def complete_event(bot_scope: str, event_id: str, response: dict[str, Any] | None = None) -> None:
    if not event_id:
        return
    now = time.time()
    encoded = json.dumps(response or {}, ensure_ascii=False, default=str)[:8000]
    with _db() as conn:
        conn.execute(
            "UPDATE event_ledger SET status='succeeded', response=?, updated_at=?, expires_at=? "
            "WHERE bot_scope=? AND event_id=?",
            (encoded, now, now + EVENT_TTL_SECONDS, bot_scope, event_id),
        )


def fail_event(bot_scope: str, event_id: str) -> None:
    if not event_id:
        return
    now = time.time()
    with _db() as conn:
        conn.execute(
            "UPDATE event_ledger SET status='failed', response=NULL, updated_at=?, expires_at=? "
            "WHERE bot_scope=? AND event_id=?",
            (now, now + EVENT_TTL_SECONDS, bot_scope, event_id),
        )
