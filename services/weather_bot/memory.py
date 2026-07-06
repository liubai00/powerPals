"""轻量长期记忆: SQLite 落盘(data/memory.db, 挂载在容器外)。

L1 用户画像: 每人常查城市/天数; L2 对话记忆: 最近几轮, TTL 7 天。
所有读写由调用方 try 包裹, 失败降级为无记忆。
"""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.getenv("WEATHER_MEMORY_DB", "data/memory.db")
TURN_TTL_SECONDS = 7 * 24 * 3600
TURN_MAX_PER_KEY = 12


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
    return conn


def record_turn(key: str, role: str, content: str) -> None:
    now = time.time()
    with _conn() as conn:
        conn.execute("INSERT INTO turns VALUES(?,?,?,?)", (key, role, (content or "")[:400], now))
        conn.execute("DELETE FROM turns WHERE ts < ?", (now - TURN_TTL_SECONDS,))
        conn.execute(
            "DELETE FROM turns WHERE rowid IN ("
            "SELECT rowid FROM turns WHERE k=? ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (key, TURN_MAX_PER_KEY),
        )


def recent_turns(key: str) -> list[dict[str, str]]:
    now = time.time()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM turns WHERE k=? AND ts>=? ORDER BY ts",
            (key, now - TURN_TTL_SECONDS),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in rows]


def remember_query(bot_role: str, sender_id: str, region: str | None, days: int) -> None:
    if not sender_id or not region:
        return
    with _conn() as conn:
        conn.execute(
            "INSERT INTO user_pref VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(bot_role, sender_id) DO UPDATE SET "
            "region=excluded.region, days=excluded.days, ts=excluded.ts, hits=user_pref.hits+1",
            (bot_role, sender_id, region, max(1, int(days or 1)), time.time()),
        )


def preferred_region(bot_role: str, sender_id: str) -> dict | None:
    if not sender_id:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT region, days, hits FROM user_pref WHERE bot_role=? AND sender_id=?",
            (bot_role, sender_id),
        ).fetchone()
    if not row:
        return None
    return {"region": row[0], "days": row[1], "hits": row[2]}


def recent_chat_turns(key_prefix: str, limit: int = 12) -> list[dict[str, str]]:
    """按前缀(同话题跨发言人)取最近对话。chat_id/thread_id 含下划线是 LIKE 通配符, 需转义。"""
    now = time.time()
    escaped = key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content, ts FROM turns WHERE k LIKE ? ESCAPE '\\' AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (escaped + "%", now - TURN_TTL_SECONDS, limit),
        ).fetchall()
    return [{"role": role, "content": content} for role, content, _ts in reversed(rows)]
