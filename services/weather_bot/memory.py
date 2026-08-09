"""轻量持久记忆，默认落盘到容器外的 ``data/memory.db``。

对话轮次最多保留 7 天；待澄清和失败请求重试状态保留 10 分钟；成功天气查询按会话类型
分类保留（群聊 30 分钟、私聊 24 小时）。其他兼容状态仍使用默认 TTL。
调用方应在存储失败时降级为无记忆，不能影响业务主流程。
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Iterator

from services.weather_bot.logging_safety import redact_sensitive_text

DB_PATH = os.getenv("WEATHER_MEMORY_DB", "data/memory.db")
TURN_TTL_SECONDS = 30 * 60
TURN_MAX_PER_KEY = 6
MAX_TURN_TTL_SECONDS = 24 * 3600
MAX_TURN_MAX_PER_KEY = 12
STATE_TTL_SECONDS = 7 * 24 * 3600
PENDING_CLARIFICATION_TTL_SECONDS = 10 * 60
RETRY_REQUEST_TTL_SECONDS = 10 * 60
GROUP_QUERY_TTL_SECONDS = 30 * 60
DIRECT_QUERY_TTL_SECONDS = 24 * 3600
BRIEFING_CONTEXT_TTL_SECONDS = 36 * 3600
EVENT_TTL_SECONDS = 7 * 24 * 3600
MEMORY_SCHEMA_VERSION = 2
# 全国晨报的单次生成/等待上限是 600 秒。事件处理租期必须覆盖该窗口，
# 否则飞书重投同一 event_id 时可能产生第二个回复处理者。
EVENT_PROCESSING_TIMEOUT_SECONDS = 15 * 60
SEND_AUDIT_STATUSES = frozenset(
    {"attempted", "queued", "sent", "failed", "suppressed", "skipped", "dry_run"}
)
SEND_AUDIT_RETENTION_SECONDS = 90 * 24 * 3600
_SCOPE_KEY_PREFIX = "scope-sha256:v1:"
_IDENTIFIER_KEY_PREFIX = "id-sha256:v1:"


def _scope_storage_key(value: str) -> str:
    """Return a domain-separated stable key without retaining Feishu IDs."""

    normalized = str(value or "")
    if normalized.startswith(_SCOPE_KEY_PREFIX):
        return normalized
    digest = hashlib.sha256(
        f"weather-memory/scope/v1\x00{normalized}".encode("utf-8")
    ).hexdigest()
    return f"{_SCOPE_KEY_PREFIX}{digest}"


def _identifier_storage_key(namespace: str, value: str) -> str:
    """Return a domain-separated hash for one external identifier."""

    normalized = str(value or "").strip()
    if normalized.startswith(_IDENTIFIER_KEY_PREFIX):
        return normalized
    digest = hashlib.sha256(
        f"weather-memory/{namespace}/v1\x00{normalized}".encode("utf-8")
    ).hexdigest()
    return f"{_IDENTIFIER_KEY_PREFIX}{digest}"


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_identifier_storage(conn: sqlite3.Connection) -> None:
    """Re-key legacy memory rows without retaining external identifiers."""

    turns = conn.execute("SELECT k,role,content,ts FROM turns").fetchall()
    conn.execute("DELETE FROM turns")
    conn.executemany(
        "INSERT INTO turns(k,role,content,ts) VALUES(?,?,?,?)",
        [(_scope_storage_key(k), role, content, ts) for k, role, content, ts in turns],
    )

    preferences = conn.execute(
        "SELECT bot_role,sender_id,region,days,ts,hits FROM user_pref"
    ).fetchall()
    conn.execute("DELETE FROM user_pref")
    conn.executemany(
        "INSERT OR REPLACE INTO user_pref(bot_role,sender_id,region,days,ts,hits) "
        "VALUES(?,?,?,?,?,?)",
        [
            (
                bot_role,
                _identifier_storage_key("user-preference", sender_id),
                region,
                days,
                ts,
                hits,
            )
            for bot_role, sender_id, region, days, ts, hits in preferences
        ],
    )

    states = conn.execute(
        "SELECT k,payload,updated_at,expires_at,state_version,intent_version,"
        "entity_schema_version FROM conversation_state"
    ).fetchall()
    conn.execute("DELETE FROM conversation_state")
    conn.executemany(
        "INSERT OR REPLACE INTO conversation_state("
        "k,payload,updated_at,expires_at,state_version,intent_version,entity_schema_version"
        ") VALUES(?,?,?,?,?,?,?)",
        [
            (
                _scope_storage_key(k),
                payload,
                updated_at,
                expires_at,
                state_version,
                intent_version,
                entity_schema_version,
            )
            for (
                k,
                payload,
                updated_at,
                expires_at,
                state_version,
                intent_version,
                entity_schema_version,
            ) in states
            if not str(k).startswith("bot-reply|")
        ],
    )

    events = conn.execute(
        "SELECT bot_scope,event_id,status,response,updated_at,expires_at FROM event_ledger"
    ).fetchall()
    conn.execute("DELETE FROM event_ledger")
    conn.executemany(
        "INSERT OR REPLACE INTO event_ledger("
        "bot_scope,event_id,status,response,updated_at,expires_at"
        ") VALUES(?,?,?,?,?,?)",
        [
            (
                *_event_storage_identity(bot_scope, event_id),
                status,
                response,
                updated_at,
                expires_at,
            )
            for bot_scope, event_id, status, response, updated_at, expires_at in events
        ],
    )

    markers = conn.execute(
        "SELECT bot_role,chat_type,chat_id,thread_id,user_id,message_id,"
        "created_at,expires_at FROM bot_reply_markers"
    ).fetchall()
    conn.execute("DELETE FROM bot_reply_markers")
    conn.executemany(
        "INSERT OR REPLACE INTO bot_reply_markers("
        "bot_role,chat_type,chat_id,thread_id,user_id,message_id,created_at,expires_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        [
            (
                *_bot_reply_marker_storage_scope(
                    (bot_role, chat_type, chat_id, thread_id, user_id, message_id)
                ),
                created_at,
                expires_at,
            )
            for (
                bot_role,
                chat_type,
                chat_id,
                thread_id,
                user_id,
                message_id,
                created_at,
                expires_at,
            ) in markers
        ],
    )


def _conn() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA secure_delete = ON")
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
    _add_column_if_missing(conn, "conversation_state", "intent_version", "TEXT")
    _add_column_if_missing(
        conn,
        "conversation_state",
        "entity_schema_version",
        "TEXT",
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS event_ledger("
        "bot_scope TEXT NOT NULL, event_id TEXT NOT NULL, status TEXT NOT NULL, "
        "response TEXT, updated_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "PRIMARY KEY(bot_scope, event_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bot_reply_markers("
        "bot_role TEXT NOT NULL, chat_type TEXT NOT NULL, chat_id TEXT NOT NULL, "
        "thread_id TEXT NOT NULL, user_id TEXT NOT NULL, message_id TEXT NOT NULL, "
        "created_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "PRIMARY KEY(bot_role,chat_type,chat_id,thread_id,user_id,message_id))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_reply_markers_expiry "
        "ON bot_reply_markers(expires_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS send_audit("
        "audit_id INTEGER PRIMARY KEY AUTOINCREMENT, send_id_hash TEXT NOT NULL, "
        "scope_hash TEXT NOT NULL, bot_role TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_send_audit_lookup "
        "ON send_audit(send_id_hash,scope_hash,bot_role,created_at)"
    )
    current_schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_schema_version < MEMORY_SCHEMA_VERSION:
        _migrate_identifier_storage(conn)
        conn.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
    conn.commit()
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = _conn()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def record_turn(
    key: str,
    role: str,
    content: str,
    *,
    enabled: bool = False,
    ttl_seconds: int = TURN_TTL_SECONDS,
    max_per_key: int = TURN_MAX_PER_KEY,
) -> None:
    if not enabled:
        return
    now = time.time()
    retention = min(MAX_TURN_TTL_SECONDS, max(1, int(ttl_seconds)))
    turn_limit = min(MAX_TURN_MAX_PER_KEY, max(1, int(max_per_key)))
    safe_content = redact_sensitive_text(content)[:400]
    storage_key = _scope_storage_key(key)
    with _db() as conn:
        if storage_key != key:
            # Legacy rows expose the five-dimensional Feishu scope.  Drop them
            # instead of migrating free text into the new privacy boundary.
            conn.execute("DELETE FROM turns WHERE k=?", (key,))
        conn.execute(
            "INSERT INTO turns VALUES(?,?,?,?)",
            (storage_key, role, safe_content, now),
        )
        conn.execute("DELETE FROM turns WHERE ts < ?", (now - retention,))
        conn.execute(
            "DELETE FROM turns WHERE rowid IN ("
            "SELECT rowid FROM turns WHERE k=? ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (storage_key, turn_limit),
        )


def recent_turns(
    key: str,
    *,
    enabled: bool = False,
    ttl_seconds: int = TURN_TTL_SECONDS,
) -> list[dict[str, str]]:
    if not enabled:
        return []
    now = time.time()
    retention = min(MAX_TURN_TTL_SECONDS, max(1, int(ttl_seconds)))
    storage_key = _scope_storage_key(key)
    with _db() as conn:
        if storage_key != key:
            conn.execute("DELETE FROM turns WHERE k=?", (key,))
        conn.execute("DELETE FROM turns WHERE ts < ?", (now - retention,))
        rows = conn.execute(
            "SELECT role, content FROM turns WHERE k=? AND ts>=? ORDER BY ts",
            (storage_key, now - retention),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in rows]


def purge_conversation_history() -> int:
    """Physically remove all optional free-text conversation turns."""

    with _db() as conn:
        cursor = conn.execute("DELETE FROM turns")
    return max(0, int(cursor.rowcount))


def remember_query(bot_role: str, sender_id: str, region: str | None, days: int) -> None:
    if not sender_id or not region:
        return
    sender_storage_id = _identifier_storage_key("user-preference", sender_id)
    with _db() as conn:
        conn.execute(
            "DELETE FROM user_pref WHERE bot_role=? AND sender_id=?",
            (bot_role, sender_id),
        )
        conn.execute(
            "INSERT INTO user_pref VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(bot_role, sender_id) DO UPDATE SET "
            "region=excluded.region, days=excluded.days, ts=excluded.ts, hits=user_pref.hits+1",
            (
                bot_role,
                sender_storage_id,
                region,
                max(1, int(days or 1)),
                time.time(),
            ),
        )


def preferred_region(bot_role: str, sender_id: str) -> dict | None:
    if not sender_id:
        return None
    now = time.time()
    sender_storage_id = _identifier_storage_key("user-preference", sender_id)
    with _db() as conn:
        conn.execute(
            "DELETE FROM user_pref WHERE bot_role=? AND sender_id=?",
            (bot_role, sender_id),
        )
        row = conn.execute(
            "SELECT region, days, hits FROM user_pref "
            "WHERE bot_role=? AND sender_id=? AND ts>=?",
            (bot_role, sender_storage_id, now - STATE_TTL_SECONDS),
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
    storage_key = _scope_storage_key(key)
    with _db() as conn:
        row = conn.execute(
            "SELECT payload,updated_at,expires_at,state_version,"
            "intent_version,entity_schema_version FROM conversation_state "
            "WHERE k=? AND expires_at>?",
            (storage_key, now),
        ).fetchone()
        if row is None and storage_key != key:
            legacy = conn.execute(
                "SELECT payload,updated_at,expires_at,state_version,"
                "intent_version,entity_schema_version FROM conversation_state "
                "WHERE k=? AND expires_at>?",
                (key, now),
            ).fetchone()
            if legacy is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO conversation_state("
                    "k,payload,updated_at,expires_at,state_version,"
                    "intent_version,entity_schema_version) VALUES(?,?,?,?,?,?,?)",
                    (storage_key, *legacy),
                )
                conn.execute("DELETE FROM conversation_state WHERE k=?", (key,))
                row = legacy
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
    storage_key = _scope_storage_key(key)
    state_version = max(1, int(payload.get("state_version") or 1))
    intent_version = payload.get("intent_version")
    entity_schema_version = payload.get("entity_schema_version")
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    with _db() as conn:
        if storage_key != key:
            conn.execute("DELETE FROM conversation_state WHERE k=?", (key,))
        conn.execute(
            "INSERT INTO conversation_state("
            "k,payload,updated_at,expires_at,state_version,intent_version,entity_schema_version"
            ") VALUES(?,?,?,?,?,?,?) ON CONFLICT(k) DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at, "
            "expires_at=excluded.expires_at, state_version=excluded.state_version, "
            "intent_version=excluded.intent_version, "
            "entity_schema_version=excluded.entity_schema_version",
            (
                storage_key,
                encoded,
                now,
                now + max(1, ttl_seconds),
                state_version,
                str(intent_version) if intent_version is not None else None,
                str(entity_schema_version) if entity_schema_version is not None else None,
            ),
        )


def clear_conversation_state(key: str) -> None:
    storage_key = _scope_storage_key(key)
    with _db() as conn:
        conn.execute(
            "DELETE FROM conversation_state WHERE k IN (?,?)",
            (storage_key, key),
        )


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
    minimized: dict[str, Any] = {
        "command_type": "forecast",
        "target_date": str(payload.get("target_date") or "")[:32],
        "days": min(16, max(1, int(payload.get("days") or 1))),
        "metrics": [
            str(item)[:40]
            for item in (payload.get("metrics") or [])
            if isinstance(item, str) and item.strip()
        ][:10],
    }
    region = payload.get("region")
    if isinstance(region, str) and region.strip():
        minimized["region"] = region.strip()[:120]
    for name in ("latitude", "longitude"):
        value = payload.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimized[name] = float(value)
    save_conversation_state(
        f"retry-request|{key}",
        {
            "state_version": 2,
            "retry_request": minimized,
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


def _bot_reply_marker_scope(
    *,
    bot_role: str,
    chat_type: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    message_id: str,
) -> tuple[str, str, str, str, str, str] | None:
    values = tuple(
        str(value or "").strip()
        for value in (bot_role, chat_type, chat_id, thread_id, user_id, message_id)
    )
    if not all(values):
        return None
    return values  # type: ignore[return-value]


def _bot_reply_marker_storage_scope(
    scope: tuple[str, str, str, str, str, str],
) -> tuple[str, str, str, str, str, str]:
    bot_role, chat_type, chat_id, thread_id, user_id, message_id = scope
    return (
        bot_role,
        chat_type,
        _identifier_storage_key("reply-chat", chat_id),
        _identifier_storage_key("reply-thread", thread_id),
        _identifier_storage_key("reply-user", user_id),
        _identifier_storage_key("reply-message", message_id),
    )


def save_bot_reply_marker(
    *,
    bot_role: str,
    chat_type: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    message_id: str,
    ttl_seconds: int = BRIEFING_CONTEXT_TTL_SECONDS,
) -> None:
    """Record one bot-authored reply for an exact conversation scope."""
    scope = _bot_reply_marker_scope(
        bot_role=bot_role,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        message_id=message_id,
    )
    if scope is None:
        return
    storage_scope = _bot_reply_marker_storage_scope(scope)
    now = time.time()
    with _db() as conn:
        conn.execute("DELETE FROM bot_reply_markers WHERE expires_at<=?", (now,))
        conn.execute(
            "INSERT OR IGNORE INTO bot_reply_markers("
            "bot_role,chat_type,chat_id,thread_id,user_id,message_id,created_at,expires_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (*storage_scope, now, now + max(1, int(ttl_seconds))),
        )


def load_bot_reply_marker(
    *,
    bot_role: str,
    chat_type: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    message_id: str,
) -> dict[str, str] | None:
    """Load a non-expired marker only when every scope dimension matches."""
    scope = _bot_reply_marker_scope(
        bot_role=bot_role,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        message_id=message_id,
    )
    if scope is None:
        return None
    storage_scope = _bot_reply_marker_storage_scope(scope)
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM bot_reply_markers WHERE "
            "bot_role=? AND chat_type=? AND chat_id=? AND thread_id=? "
            "AND user_id=? AND message_id=? AND expires_at>?",
            (*storage_scope, now),
        ).fetchone()
        conn.execute("DELETE FROM bot_reply_markers WHERE expires_at<=?", (now,))
    if not row:
        return None
    return {
        "source": "recorded_bot_reply",
        "bot_role": scope[0],
        "chat_type": scope[1],
        "chat_id": scope[2],
        "thread_id": scope[3],
        "user_id": scope[4],
        "message_id": scope[5],
    }


def _send_audit_identity(
    *,
    send_id: str,
    bot_role: str,
    chat_type: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
) -> tuple[str, str, str]:
    values = tuple(
        str(value or "").strip()
        for value in (send_id, bot_role, chat_type, chat_id, thread_id, user_id)
    )
    if not all(values):
        raise ValueError("send audit identity requires every conversation scope field")
    normalized_send_id, normalized_bot_role, *scope = values
    send_id_hash = hashlib.sha256(
        f"weather-send-audit/v1/send-id\x00{normalized_send_id}".encode("utf-8")
    ).hexdigest()
    scope_hash = hashlib.sha256(
        json.dumps(
            [normalized_bot_role, *scope],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return send_id_hash, scope_hash, normalized_bot_role


def record_send_audit(
    *,
    send_id: str,
    bot_role: str,
    chat_type: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    status: str,
) -> None:
    """Append a delivery status without storing target IDs, content, or credentials."""
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in SEND_AUDIT_STATUSES:
        raise ValueError("unsupported send audit status")
    send_id_hash, scope_hash, normalized_bot_role = _send_audit_identity(
        send_id=send_id,
        bot_role=bot_role,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    with _db() as conn:
        conn.execute(
            "INSERT INTO send_audit("
            "send_id_hash,scope_hash,bot_role,status,created_at"
            ") VALUES(?,?,?,?,?)",
            (
                send_id_hash,
                scope_hash,
                normalized_bot_role,
                normalized_status,
                time.time(),
            ),
        )


def load_send_audit(
    *,
    send_id: str,
    bot_role: str,
    chat_type: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
) -> list[dict[str, str | float]]:
    """Return append-only statuses for one exact, hashed delivery identity."""
    send_id_hash, scope_hash, normalized_bot_role = _send_audit_identity(
        send_id=send_id,
        bot_role=bot_role,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
    )
    with _db() as conn:
        rows = conn.execute(
            "SELECT status,created_at FROM send_audit WHERE "
            "send_id_hash=? AND scope_hash=? AND bot_role=? AND created_at>=? "
            "ORDER BY audit_id",
            (
                send_id_hash,
                scope_hash,
                normalized_bot_role,
                time.time() - SEND_AUDIT_RETENTION_SECONDS,
            ),
        ).fetchall()
    return [
        {"status": str(status), "created_at": float(created_at)}
        for status, created_at in rows
    ]


def purge_expired_send_audit(
    *,
    retention_seconds: int = SEND_AUDIT_RETENTION_SECONDS,
) -> int:
    """Physically remove audit rows outside the finite retention window."""
    cutoff = time.time() - max(1, int(retention_seconds))
    with _db() as conn:
        cursor = conn.execute("DELETE FROM send_audit WHERE created_at<?", (cutoff,))
    return max(0, int(cursor.rowcount))


def _event_storage_identity(bot_scope: str, event_id: str) -> tuple[str, str]:
    return (
        _identifier_storage_key("event-bot-scope", bot_scope),
        _identifier_storage_key("event-id", event_id),
    )


def claim_event(bot_scope: str, event_id: str) -> bool:
    """Atomically claim an event.

    Completed events and fresh in-flight events are duplicates. Failed events
    and stale in-flight events are retryable.
    """
    if not event_id:
        return True
    storage_bot_scope, storage_event_id = _event_storage_identity(bot_scope, event_id)
    now = time.time()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM event_ledger WHERE expires_at<?", (now,))
        row = conn.execute(
            "SELECT status, updated_at FROM event_ledger WHERE bot_scope=? AND event_id=?",
            (storage_bot_scope, storage_event_id),
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
                (
                    now,
                    now + EVENT_TTL_SECONDS,
                    storage_bot_scope,
                    storage_event_id,
                ),
            )
            return True
        conn.execute(
            "INSERT INTO event_ledger(bot_scope,event_id,status,response,updated_at,expires_at) "
            "VALUES(?,?,'processing',NULL,?,?)",
            (
                storage_bot_scope,
                storage_event_id,
                now,
                now + EVENT_TTL_SECONDS,
            ),
        )
    return True


def complete_event(bot_scope: str, event_id: str, response: dict[str, Any] | None = None) -> None:
    if not event_id:
        return
    storage_bot_scope, storage_event_id = _event_storage_identity(bot_scope, event_id)
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
            (
                encoded,
                now,
                now + EVENT_TTL_SECONDS,
                storage_bot_scope,
                storage_event_id,
            ),
        )


def fail_event(bot_scope: str, event_id: str) -> None:
    if not event_id:
        return
    storage_bot_scope, storage_event_id = _event_storage_identity(bot_scope, event_id)
    now = time.time()
    with _db() as conn:
        conn.execute(
            "UPDATE event_ledger SET status='failed', response=NULL, updated_at=?, expires_at=? "
            "WHERE bot_scope=? AND event_id=?",
            (
                now,
                now + EVENT_TTL_SECONDS,
                storage_bot_scope,
                storage_event_id,
            ),
        )
