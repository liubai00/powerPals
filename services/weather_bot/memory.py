"""轻量持久记忆，默认落盘到容器外的 ``data/memory.db``。

对话轮次最多保留 7 天；待澄清和失败请求重试状态保留 10 分钟；成功天气查询按会话类型
分类保留（群聊 30 分钟、私聊 24 小时）。其他兼容状态仍使用默认 TTL。
调用方应在存储失败时降级为无记忆，不能影响业务主流程。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
from typing import Any, Iterator

from services.weather_bot.logging_safety import redact_sensitive_text

DB_PATH = os.getenv("WEATHER_MEMORY_DB", "data/memory.db")
TURN_TTL_SECONDS = 7 * 24 * 3600
TURN_MAX_PER_KEY = 12
STATE_TTL_SECONDS = 7 * 24 * 3600
PENDING_CLARIFICATION_TTL_SECONDS = 10 * 60
RETRY_REQUEST_TTL_SECONDS = 10 * 60
GROUP_QUERY_TTL_SECONDS = 30 * 60
DIRECT_QUERY_TTL_SECONDS = 24 * 3600
BRIEFING_CONTEXT_TTL_SECONDS = 36 * 3600
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
    safe_content = redact_sensitive_text(content)[:400]
    with _db() as conn:
        conn.execute("INSERT INTO turns VALUES(?,?,?,?)", (key, role, safe_content, now))
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
            "SELECT payload FROM conversation_state WHERE k=? AND expires_at>?",
            (key, now),
        ).fetchone()
        conn.execute("DELETE FROM conversation_state WHERE expires_at<=?", (now,))
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


def conversation_query_ttl_seconds(chat_type: str) -> int:
    """Return the lifetime for an ordinary successful query context."""
    if str(chat_type or "").strip().lower() in {"group", "group_chat"}:
        return GROUP_QUERY_TTL_SECONDS
    return DIRECT_QUERY_TTL_SECONDS


def save_pending_clarification(key: str, payload: dict[str, Any]) -> None:
    """Persist privacy-minimized clarification state independently of query state."""
    save_conversation_state(
        f"pending-clarification|{key}",
        {
            "state_version": 2,
            "pending_clarification": payload,
        },
        ttl_seconds=PENDING_CLARIFICATION_TTL_SECONDS,
    )


def load_pending_clarification(key: str) -> dict[str, Any] | None:
    state = load_conversation_state(f"pending-clarification|{key}")
    pending = state.get("pending_clarification") if state else None
    return pending if isinstance(pending, dict) else None


def clear_pending_clarification(key: str) -> None:
    clear_conversation_state(f"pending-clarification|{key}")


def save_retry_request(key: str, payload: dict[str, Any]) -> None:
    """Persist only the structured, retryable weather turn for a short window."""
    save_conversation_state(
        f"retry-request|{key}",
        {
            "state_version": 2,
            "retry_request": payload,
        },
        ttl_seconds=RETRY_REQUEST_TTL_SECONDS,
    )


def load_retry_request(key: str) -> dict[str, Any] | None:
    state = load_conversation_state(f"retry-request|{key}")
    retry_request = state.get("retry_request") if state else None
    return retry_request if isinstance(retry_request, dict) else None


def clear_retry_request(key: str) -> None:
    clear_conversation_state(f"retry-request|{key}")


def save_briefing_context(key: str, payload: dict[str, Any]) -> None:
    """Persist a published briefing pointer without extending query context."""
    save_conversation_state(
        f"briefing-context|{key}",
        payload,
        ttl_seconds=BRIEFING_CONTEXT_TTL_SECONDS,
    )


def load_briefing_context(key: str) -> dict[str, Any] | None:
    return load_conversation_state(f"briefing-context|{key}")


def clear_briefing_context(key: str) -> None:
    clear_conversation_state(f"briefing-context|{key}")


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
    result = response if isinstance(response, dict) else {}
    # Duplicate suppression needs only completion metadata.  Storing cards,
    # user text or provider point series would turn the ledger into an
    # unnecessary seven-day data copy without improving idempotency.
    safe_result = {
        key: result[key]
        for key in ("status", "bot_role", "mode")
        if isinstance(result.get(key), (str, int, float, bool))
    }
    encoded = json.dumps(safe_result, ensure_ascii=False, default=str)
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
