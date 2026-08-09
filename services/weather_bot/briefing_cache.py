"""跨进程共享的电力气象晨报快照缓存。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Iterator
from urllib.parse import urlsplit


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
    if payload.get("schema_version") not in {1, 2} or payload.get("cache_key") != cache_key:
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
    if payload.get("schema_version") == 2 and not _is_valid_v2_metadata(payload):
        return False
    return _is_valid_card(payload.get("summary_card")) and _is_valid_card(payload.get("detail_card"))


def _is_valid_v2_metadata(payload: dict[str, Any]) -> bool:
    if _version_identity(payload) is None:
        return False
    required_text = ("retrieved_at",)
    if any(not isinstance(payload.get(field), str) or not payload[field].strip() for field in required_text):
        return False
    if not isinstance(payload.get("provider_issued_at"), dict):
        return False
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources or any(not isinstance(item, str) for item in sources):
        return False
    valid_time = payload.get("valid_time")
    if not isinstance(valid_time, dict) or any(
        not isinstance(valid_time.get(field), str) or not valid_time[field].strip()
        for field in ("start", "end", "timezone")
    ):
        return False
    provider_metadata = payload.get("provider_run_metadata")
    if not isinstance(provider_metadata, list) or not provider_metadata:
        return False
    metadata_providers: set[str] = set()
    for item in provider_metadata:
        if not isinstance(item, dict) or "raw" in item:
            return False
        provider = item.get("provider")
        source_urls = item.get("source_urls")
        hashes = item.get("content_sha256s")
        record_coverage = item.get("record_coverage")
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(source_urls, list)
            or not source_urls
            or any(not _is_web_url(url) for url in source_urls)
            or not isinstance(hashes, list)
            or not hashes
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value) for value in hashes)
            or item.get("retention_policy") != "derived_only"
            or not isinstance(record_coverage, dict)
            or not isinstance(record_coverage.get("ok"), int)
            or record_coverage["ok"] < 1
            or record_coverage.get("source_url") != record_coverage["ok"]
            or record_coverage.get("content_sha256") != record_coverage["ok"]
        ):
            return False
        metadata_providers.add(provider)
    if metadata_providers != set(sources):
        return False
    quality = payload.get("quality")
    if not isinstance(quality, dict) or quality.get("status") not in {"good", "degraded"}:
        return False
    if not isinstance(payload.get("metric_coverage"), dict):
        return False
    confidence = payload.get("confidence")
    if not isinstance(confidence, dict) or not isinstance(confidence.get("level"), str):
        return False
    change = payload.get("version_change")
    if not isinstance(change, dict) or change.get("status") not in {"available", "unavailable"}:
        return False
    if not isinstance(payload.get("market_risk_snapshots"), list):
        return False
    previous_run_id = payload.get("previous_run_id")
    return previous_run_id is None or isinstance(previous_run_id, str)


def _is_web_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _version_identity(payload: Any) -> tuple[str, str, str, str, str] | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return None
    fields = (
        "forecast_run_id",
        "report_date",
        "release_slot",
        "market_config_version",
        "report_version",
    )
    values = tuple(payload.get(field) for field in fields)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    return values  # type: ignore[return-value]


class BriefingCache:
    def __init__(
        self,
        db_path: str,
        *,
        ttl_seconds: int = 3600,
        version_retention_days: int = 90,
    ) -> None:
        self.db_path = db_path
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.version_retention_seconds = max(1, int(version_retention_days)) * 86400

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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS briefing_versions("
            "run_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, release_slot TEXT NOT NULL, "
            "market_config_version TEXT NOT NULL, report_version TEXT NOT NULL, "
            "generated_at TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_briefing_versions_same_release "
            "ON briefing_versions(report_date,release_slot,market_config_version,report_version,generated_at)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_briefing_deliveries("
            "release_slot TEXT NOT NULL, target_chat_id TEXT NOT NULL, state TEXT NOT NULL, "
            "owner_token TEXT NOT NULL, send_uuid TEXT NOT NULL, message_id TEXT, "
            "lease_until REAL NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY(release_slot,target_chat_id))"
        )
        delivery_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(scheduled_briefing_deliveries)").fetchall()
        }
        if "lease_until" not in delivery_columns:
            conn.execute(
                "ALTER TABLE scheduled_briefing_deliveries "
                "ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"
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

    def load_version(self, forecast_run_id: str) -> dict[str, Any] | None:
        """Load an immutable derived briefing snapshot by its forecast run id."""

        with self._db() as conn:
            row = conn.execute(
                "SELECT payload FROM briefing_versions WHERE run_id=?",
                (forecast_run_id,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        identity = _version_identity(payload)
        if (
            identity is None
            or identity[0] != forecast_run_id
            or not _is_valid_snapshot(
                payload,
                cache_key=str(payload.get("cache_key") or ""),
                generator_version=identity[4],
            )
        ):
            return None
        return payload

    def load_previous_same_release(
        self,
        *,
        report_date: str,
        release_slot: str,
        market_config_version: str,
        report_version: str,
    ) -> dict[str, Any] | None:
        """Return yesterday's immutable run for the same declared release slot."""

        previous_date = (date.fromisoformat(report_date) - timedelta(days=1)).isoformat()
        with self._db() as conn:
            row = conn.execute(
                "SELECT run_id,payload FROM briefing_versions "
                "WHERE report_date=? AND release_slot=? AND market_config_version=? "
                "AND report_version=? ORDER BY generated_at DESC LIMIT 1",
                (
                    previous_date,
                    release_slot,
                    market_config_version,
                    report_version,
                ),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        identity = _version_identity(payload)
        if (
            identity is None
            or identity[0] != str(row[0])
            or not _is_valid_snapshot(
                payload,
                cache_key=str(payload.get("cache_key") or ""),
                generator_version=identity[4],
            )
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
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute(
                "SELECT owner_token FROM briefing_generation WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if lease and str(lease[0]) != owner_token:
                raise RuntimeError("briefing generation lease is owned by another process")
            identity = _version_identity(payload)
            if identity is not None and not _is_valid_snapshot(
                payload,
                cache_key=cache_key,
                generator_version=generator_version,
            ):
                identity = None
            version_already_saved = False
            if identity is not None:
                existing = conn.execute(
                    "SELECT payload FROM briefing_versions WHERE run_id=?",
                    (identity[0],),
                ).fetchone()
                if existing and str(existing[0]) != encoded:
                    raise ValueError("briefing version is immutable for an existing forecast_run_id")
                version_already_saved = existing is not None
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
            if identity is not None and not version_already_saved:
                run_id, report_date, release_slot, market_config_version, report_version = identity
                conn.execute(
                    "INSERT INTO briefing_versions("
                    "run_id,report_date,release_slot,market_config_version,report_version,"
                    "generated_at,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        report_date,
                        release_slot,
                        market_config_version,
                        report_version,
                        str(payload.get("generated_at") or ""),
                        encoded,
                        current,
                    ),
                )
            conn.execute(
                "DELETE FROM briefing_versions WHERE created_at<?",
                (current - self.version_retention_seconds,),
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

    def claim_scheduled_delivery(
        self,
        release_slot: str,
        target_chat_id: str,
        owner_token: str,
        send_uuid: str,
        *,
        lease_seconds: int = 600,
        now: float | None = None,
    ) -> bool:
        """Atomically reserve one scheduled release for one reviewed target."""

        current = time.time() if now is None else float(now)
        lease_until = current + max(1, int(lease_seconds))
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM scheduled_briefing_deliveries WHERE updated_at<?",
                (current - self.version_retention_seconds,),
            )
            row = conn.execute(
                "SELECT state,send_uuid,lease_until FROM scheduled_briefing_deliveries "
                "WHERE release_slot=? AND target_chat_id=?",
                (release_slot, target_chat_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO scheduled_briefing_deliveries("
                    "release_slot,target_chat_id,state,owner_token,send_uuid,message_id,"
                    "lease_until,updated_at) VALUES(?,?,?,?,?,NULL,?,?)",
                    (
                        release_slot,
                        target_chat_id,
                        "sending",
                        owner_token,
                        send_uuid,
                        lease_until,
                        current,
                    ),
                )
                return True
            if str(row[0]) == "sent" or float(row[2]) >= current:
                return False
            if str(row[1]) != send_uuid:
                raise ValueError("scheduled briefing retry must reuse the original send UUID")
            conn.execute(
                "UPDATE scheduled_briefing_deliveries "
                "SET state='sending',owner_token=?,lease_until=?,updated_at=? "
                "WHERE release_slot=? AND target_chat_id=?",
                (owner_token, lease_until, current, release_slot, target_chat_id),
            )
            return True

    def complete_scheduled_delivery(
        self,
        release_slot: str,
        target_chat_id: str,
        owner_token: str,
        message_id: str,
        *,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else float(now)
        with self._db() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_briefing_deliveries "
                "SET state='sent',message_id=?,lease_until=?,updated_at=? "
                "WHERE release_slot=? AND target_chat_id=? AND owner_token=? AND state='sending'",
                (message_id, current, current, release_slot, target_chat_id, owner_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("scheduled briefing delivery claim is not owned by this process")

    def release_failed_scheduled_delivery(
        self,
        release_slot: str,
        target_chat_id: str,
        owner_token: str,
    ) -> None:
        """Release only this process' unfinished claim so the stable UUID can retry."""

        with self._db() as conn:
            conn.execute(
                "DELETE FROM scheduled_briefing_deliveries "
                "WHERE release_slot=? AND target_chat_id=? AND owner_token=? AND state='sending'",
                (release_slot, target_chat_id, owner_token),
            )
