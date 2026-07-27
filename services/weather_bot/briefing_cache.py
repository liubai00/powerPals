"""跨进程共享的电力气象晨报快照缓存。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator


def _is_valid_card(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("msg_type") != "interactive":
        return False
    card = value.get("card")
    header = card.get("header") if isinstance(card, dict) else None
    title = header.get("title") if isinstance(header, dict) else None
    return (
        isinstance(card, dict)
        and isinstance(header, dict)
        and isinstance(title, dict)
        and isinstance(title.get("content"), str)
        and bool(title["content"].strip())
        and isinstance(card.get("elements"), list)
    )


def _is_valid_snapshot(
    payload: Any,
    *,
    cache_key: str,
    generator_version: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != 1 or payload.get("cache_key") != cache_key:
        return False
    required_text_fields = (
        "report_date",
        "market_config_version",
        "report_version",
        "generated_at",
        "expires_at",
    )
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required_text_fields):
        return False
    if payload.get("report_version") != generator_version:
        return False
    key_parts = cache_key.rsplit(":", 2)
    if len(key_parts) == 3:
        report_date, market_config_version, report_version = key_parts
        if (
            payload.get("report_date") != report_date
            or payload.get("market_config_version") != market_config_version
            or payload.get("report_version") != report_version
        ):
            return False
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        return False
    for section in ("provincial_areas", "markets", "points", "baseline_points"):
        values = coverage.get(section)
        if (
            not isinstance(values, dict)
            or not isinstance(values.get("covered"), int)
            or not isinstance(values.get("total"), int)
        ):
            return False
    statistics = payload.get("statistics")
    if (
        not isinstance(statistics, dict)
        or not isinstance(statistics.get("configured_markets"), int)
        or not isinstance(statistics.get("classified_markets"), int)
    ):
        return False
    return _is_valid_card(payload.get("summary_card")) and _is_valid_card(payload.get("detail_card"))


class BriefingCache:
    def __init__(self, db_path: str, *, ttl_seconds: int = 3600) -> None:
        self.db_path = db_path
        self.ttl_seconds = max(1, int(ttl_seconds))

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS briefing_cache("
            "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, generated_at REAL NOT NULL, "
            "expires_at REAL NOT NULL, generator_version TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS briefing_generation("
            "cache_key TEXT PRIMARY KEY, owner_token TEXT NOT NULL, lease_until REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def load_fresh(self, cache_key: str, *, now: float | None = None) -> dict[str, Any] | None:
        current = time.time() if now is None else float(now)
        with self._db() as conn:
            row = conn.execute(
                "SELECT payload, expires_at, generator_version FROM briefing_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            conn.execute("DELETE FROM briefing_cache WHERE expires_at<?", (current - 86400,))
        if not row or float(row[1]) < current:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if not _is_valid_snapshot(
            payload,
            cache_key=cache_key,
            generator_version=str(row[2]),
        ):
            return None
        return payload

    def claim_generation(
        self,
        cache_key: str,
        owner_token: str,
        *,
        lease_seconds: int = 600,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        lease_until = current + max(1, int(lease_seconds))
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_token, lease_until FROM briefing_generation WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if row and float(row[1]) >= current and str(row[0]) != owner_token:
                return False
            conn.execute(
                "INSERT INTO briefing_generation(cache_key,owner_token,lease_until,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "owner_token=excluded.owner_token, lease_until=excluded.lease_until, "
                "updated_at=excluded.updated_at",
                (cache_key, owner_token, lease_until, current),
            )
        return True

    def save_and_release(
        self,
        cache_key: str,
        owner_token: str,
        payload: dict[str, Any],
        *,
        generator_version: str,
        generated_at: float | None = None,
    ) -> None:
        current = time.time() if generated_at is None else float(generated_at)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute(
                "SELECT owner_token FROM briefing_generation WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if lease and str(lease[0]) != owner_token:
                raise RuntimeError("briefing generation lease is owned by another process")
            conn.execute(
                "INSERT INTO briefing_cache(cache_key,payload,generated_at,expires_at,generator_version) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload=excluded.payload, generated_at=excluded.generated_at, "
                "expires_at=excluded.expires_at, generator_version=excluded.generator_version",
                (
                    cache_key,
                    encoded,
                    current,
                    current + self.ttl_seconds,
                    generator_version,
                ),
            )
            conn.execute(
                "DELETE FROM briefing_generation WHERE cache_key=? AND owner_token=?",
                (cache_key, owner_token),
            )

    def release_generation(self, cache_key: str, owner_token: str) -> None:
        with self._db() as conn:
            conn.execute(
                "DELETE FROM briefing_generation WHERE cache_key=? AND owner_token=?",
                (cache_key, owner_token),
            )
