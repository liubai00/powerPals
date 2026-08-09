"""Offline, review-gated improvement loop for the weather bot.

This module deliberately has no Feishu dependency and no ability to modify
runtime rules.  It stores privacy-minimized evidence, scores immutable forecast
snapshots against observed weather, and emits non-executable candidates for a
human to review.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from statistics import mean
from typing import Any, Iterator, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from services.weather_bot.models import ProviderForecast, WeatherSubmission


SHANGHAI_TZ = timezone(timedelta(hours=8))
LEARNING_VERSION = "controlled_learning_v1"
CandidateStatus = Literal["pending", "approved", "rejected", "rolled_back"]


class ReplayStateSeed(BaseModel):
    bot_role: str = "weather_forecast_bot"
    chat_id: str = "chat-a"
    thread_id: str | None = None
    user_id: str = "user-a"
    chat_type: str = "p2p"
    last_successful_request: dict[str, Any]


class ReplayExpectation(BaseModel):
    should_reply: bool = True
    intent: str
    region: str | None = None
    region_absent: bool = False
    regions: list[str] | None = None
    days: int | None = None
    metrics: list[str] | None = None
    unsupported_metrics: list[str] | None = None
    target_date_offset: int | None = None


class ReplayCase(BaseModel):
    case_id: str
    category: str
    text: str
    bot_scope: Literal["weather", "task", "legacy"] = "weather"
    chat_type: Literal["p2p", "group"] = "p2p"
    addressed: bool = True
    message_type: str = "text"
    chat_id: str = "chat-a"
    thread_id: str | None = None
    user_id: str = "user-a"
    state_seeds: list[ReplayStateSeed] = Field(default_factory=list)
    expectation: ReplayExpectation
    source: str = "generated"
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class ReplayActual(BaseModel):
    should_reply: bool
    intent: str
    normalized_text: str = ""
    contextual_text: str = ""
    region: str | None = None
    regions: list[str] = Field(default_factory=list)
    days: int | None = None
    metrics: list[str] = Field(default_factory=list)
    unsupported_metrics: list[str] = Field(default_factory=list)
    target_date: str | None = None


class ReplayCaseResult(BaseModel):
    case_id: str
    category: str
    passed: bool
    mismatches: list[str] = Field(default_factory=list)
    expected: ReplayExpectation
    actual: ReplayActual


class ObservedWeather(BaseModel):
    target_date: str
    max_temperature: float
    min_temperature: float
    precipitation_sum: float
    rain_observed: bool
    wind_speed: float
    source: str = "open_meteo_historical_weather_grid"
    fetched_at: str


class ProviderScore(BaseModel):
    provider: str
    region: str
    target_date: str
    horizon_days: int
    temperature_mae: float | None = None
    max_temperature_error: float | None = None
    min_temperature_error: float | None = None
    rain_hit: bool | None = None
    wind_speed_error: float | None = None
    total_score: float
    truth_source: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(*values: Any) -> str:
    payload = "|".join(_canonical_json(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_SENSITIVE_KEY_RE = re.compile(r"password|passwd|secret|token|api[_-]?key|authorization|cookie", re.I)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|secret|password)=([^&\s]+))"
)


def sanitize_evidence(value: Any) -> Any:
    """Remove credentials and bound evidence size before persistence."""

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY_RE.search(str(key)) else sanitize_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value[:200]]
    if isinstance(value, tuple):
        return [sanitize_evidence(item) for item in value[:200]]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[REDACTED]", value)[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_evidence(str(value))


class ControlledLearningStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._initialize()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._db() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS learning_meta("
                "k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO learning_meta(k,v,updated_at) VALUES('schema_version','1',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at",
                (iso_now(),),
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS forecast_snapshots("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE, "
                "captured_at TEXT NOT NULL, target_date TEXT NOT NULL, region TEXT NOT NULL, "
                "latitude REAL, longitude REAL, submission_json TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', evaluated_at TEXT, truth_source TEXT, "
                "last_error_code TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_snapshots_due "
                "ON forecast_snapshots(status,target_date)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS provider_scores("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INTEGER NOT NULL, "
                "provider TEXT NOT NULL, region TEXT NOT NULL, target_date TEXT NOT NULL, "
                "horizon_days INTEGER NOT NULL, temperature_mae REAL, "
                "max_temperature_error REAL, min_temperature_error REAL, rain_hit INTEGER, "
                "wind_speed_error REAL, total_score REAL NOT NULL, truth_source TEXT NOT NULL, "
                "scored_at TEXT NOT NULL, UNIQUE(snapshot_id,provider), "
                "FOREIGN KEY(snapshot_id) REFERENCES forecast_snapshots(id))"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_scores_scope "
                "ON provider_scores(provider,region,horizon_days)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS learning_signals("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE, "
                "signal_type TEXT NOT NULL, source TEXT NOT NULL, severity TEXT NOT NULL, "
                "payload_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                "last_seen_at TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1)"
            )
            signal_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(learning_signals)").fetchall()
            }
            if "last_seen_at" not in signal_columns:
                conn.execute("ALTER TABLE learning_signals ADD COLUMN last_seen_at TEXT")
                conn.execute("UPDATE learning_signals SET last_seen_at=created_at WHERE last_seen_at IS NULL")
            if "occurrences" not in signal_columns:
                conn.execute(
                    "ALTER TABLE learning_signals ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replay_cases("
                "case_id TEXT PRIMARY KEY, source TEXT NOT NULL, category TEXT NOT NULL, "
                "payload_json TEXT NOT NULL, enabled INTEGER NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS replay_runs("
                "run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, total INTEGER NOT NULL, "
                "passed INTEGER NOT NULL, failed INTEGER NOT NULL, report_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS improvement_candidates("
                "candidate_id TEXT PRIMARY KEY, candidate_key TEXT NOT NULL UNIQUE, "
                "candidate_type TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "decided_at TEXT, decided_by TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS candidate_audit("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL, "
                "from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, "
                "reason TEXT, created_at TEXT NOT NULL)"
            )

    def record_forecast_snapshot(
        self,
        submission: WeatherSubmission,
        *,
        captured_at: datetime | None = None,
    ) -> int | None:
        captured = captured_at or utc_now()
        payload = submission.model_dump(mode="json")
        for result in payload.get("provider_results", []):
            result["raw"] = None
            if result.get("error_message"):
                result["error_message"] = "provider_error"
        payload = sanitize_evidence(payload)
        location = payload.get("scope", {}).get("location", {})
        snapshot_key = _fingerprint(
            payload.get("task_id"),
            payload.get("target_date"),
            payload.get("region"),
            payload.get("provider_results"),
            captured.astimezone(SHANGHAI_TZ).date().isoformat(),
        )
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO forecast_snapshots("
                "fingerprint,captured_at,target_date,region,latitude,longitude,submission_json,status) "
                "VALUES(?,?,?,?,?,?,?,'pending')",
                (
                    snapshot_key,
                    captured.isoformat(),
                    submission.target_date,
                    submission.region,
                    _as_float(location.get("latitude")),
                    _as_float(location.get("longitude")),
                    _canonical_json(payload),
                ),
            )
            row = conn.execute(
                "SELECT id FROM forecast_snapshots WHERE fingerprint=?", (snapshot_key,)
            ).fetchone()

        confidence_score = _as_float(submission.confidence.get("score")) or 0.0
        if confidence_score < 0.65:
            self.record_signal(
                "low_forecast_confidence",
                "forecast_service",
                "warning",
                {
                    "region": submission.region,
                    "target_date": submission.target_date,
                    "confidence_score": confidence_score,
                    "providers_used": submission.aggregated_forecast.providers_used,
                },
            )
        unavailable = [
            {"provider": item.provider, "status": item.status}
            for item in submission.provider_results
            if item.status != "ok"
        ]
        if unavailable:
            self.record_signal(
                "provider_unavailable",
                "forecast_service",
                "warning",
                {
                    "region": submission.region,
                    "target_date": submission.target_date,
                    "providers": unavailable,
                },
            )
        return int(row["id"]) if row else None

    def record_signal(
        self,
        signal_type: str,
        source: str,
        severity: str,
        payload: dict[str, Any],
    ) -> str:
        sanitized = sanitize_evidence(payload)
        signal_key = _fingerprint(signal_type, source, sanitized)
        now = iso_now()
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO learning_signals("
                "fingerprint,signal_type,source,severity,payload_json,created_at,last_seen_at,occurrences) "
                "VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(fingerprint) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,occurrences=learning_signals.occurrences+1",
                (signal_key, signal_type, source, severity, _canonical_json(sanitized), now, now),
            )
        return signal_key

    def pending_snapshots(self, due_on_or_before: date, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM forecast_snapshots WHERE status='pending' AND target_date<=? "
                "ORDER BY target_date,id LIMIT ?",
                (due_on_or_before.isoformat(), max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_snapshot_evaluated(
        self,
        snapshot_id: int,
        scores: list[ProviderScore],
        truth_source: str,
    ) -> None:
        now = iso_now()
        with self._db() as conn:
            for score in scores:
                conn.execute(
                    "INSERT OR REPLACE INTO provider_scores("
                    "snapshot_id,provider,region,target_date,horizon_days,temperature_mae,"
                    "max_temperature_error,min_temperature_error,rain_hit,wind_speed_error,"
                    "total_score,truth_source,scored_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        score.provider,
                        score.region,
                        score.target_date,
                        score.horizon_days,
                        score.temperature_mae,
                        score.max_temperature_error,
                        score.min_temperature_error,
                        None if score.rain_hit is None else int(score.rain_hit),
                        score.wind_speed_error,
                        score.total_score,
                        score.truth_source,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE forecast_snapshots SET status='evaluated',evaluated_at=?,truth_source=?,"
                "last_error_code=NULL WHERE id=?",
                (now, truth_source, snapshot_id),
            )

    def mark_snapshot_error(self, snapshot_id: int, error_code: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE forecast_snapshots SET last_error_code=? WHERE id=?",
                (str(error_code)[:120], snapshot_id),
            )

    def mark_snapshot_skipped(self, snapshot_id: int, error_code: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE forecast_snapshots SET status='skipped',evaluated_at=?,last_error_code=? "
                "WHERE id=?",
                (iso_now(), str(error_code)[:120], snapshot_id),
            )

    def upsert_replay_case(self, case: ReplayCase) -> None:
        now = iso_now()
        payload = sanitize_evidence(case.model_dump(mode="json"))
        with self._db() as conn:
            conn.execute(
                "INSERT INTO replay_cases(case_id,source,category,payload_json,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
                "source=excluded.source,category=excluded.category,payload_json=excluded.payload_json,"
                "enabled=excluded.enabled,updated_at=excluded.updated_at",
                (
                    case.case_id,
                    case.source,
                    case.category,
                    _canonical_json(payload),
                    int(case.enabled),
                    now,
                    now,
                ),
            )

    def list_replay_cases(self, *, enabled_only: bool = True) -> list[ReplayCase]:
        query = "SELECT payload_json FROM replay_cases"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY case_id"
        with self._db() as conn:
            rows = conn.execute(query).fetchall()
        cases = []
        for row in rows:
            try:
                cases.append(ReplayCase.model_validate_json(row["payload_json"]))
            except ValueError:
                continue
        return cases

    def record_replay_run(self, results: list[ReplayCaseResult]) -> str:
        run_id = f"replay-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        failed = [item for item in results if not item.passed]
        report = {
            "version": LEARNING_VERSION,
            "results": [item.model_dump(mode="json") for item in results],
        }
        with self._db() as conn:
            conn.execute(
                "INSERT INTO replay_runs(run_id,started_at,total,passed,failed,report_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    iso_now(),
                    len(results),
                    len(results) - len(failed),
                    len(failed),
                    _canonical_json(report),
                ),
            )
        for result in failed:
            self.record_signal(
                "replay_mismatch",
                "deterministic_replay",
                "error",
                {
                    "case_id": result.case_id,
                    "category": result.category,
                    "mismatches": result.mismatches,
                    "expected": result.expected.model_dump(mode="json"),
                    "actual": result.actual.model_dump(mode="json"),
                },
            )
        return run_id

    def signal_summary(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT signal_type,source,severity,SUM(occurrences) AS count,"
                "MAX(last_seen_at) AS latest_at "
                "FROM learning_signals GROUP BY signal_type,source,severity "
                "ORDER BY count DESC,signal_type"
            ).fetchall()
        return [dict(row) for row in rows]

    def provider_summary(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT provider,region,horizon_days,COUNT(*) AS sample_count,"
                "AVG(temperature_mae) AS temperature_mae,"
                "AVG(CASE WHEN rain_hit IS NULL THEN NULL ELSE rain_hit END) AS rain_accuracy,"
                "AVG(wind_speed_error) AS wind_speed_mae,AVG(total_score) AS total_score "
                "FROM provider_scores GROUP BY provider,region,horizon_days "
                "ORDER BY region,horizon_days,provider"
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_summary(self) -> dict[str, int]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS count FROM forecast_snapshots GROUP BY status"
            ).fetchall()
        summary = {"pending": 0, "evaluated": 0, "skipped": 0}
        summary.update({str(row["status"]): int(row["count"]) for row in rows})
        return summary

    def create_candidate(
        self,
        candidate_type: str,
        payload: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        safe_payload = sanitize_evidence(payload)
        safe_evidence = sanitize_evidence(evidence)
        candidate_key = _fingerprint(candidate_type, safe_payload)
        candidate_id = f"cand-{candidate_key[:16]}"
        now = iso_now()
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO improvement_candidates("
                "candidate_id,candidate_key,candidate_type,status,payload_json,evidence_json,"
                "created_at,updated_at) VALUES(?,?,?,'pending',?,?,?,?)",
                (
                    candidate_id,
                    candidate_key,
                    candidate_type,
                    _canonical_json(safe_payload),
                    _canonical_json(safe_evidence),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM improvement_candidates WHERE candidate_key=?", (candidate_key,)
            ).fetchone()
        return _candidate_row(row)

    def list_candidates(self, status: CandidateStatus | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM improvement_candidates"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at DESC,candidate_id"
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_candidate_row(row) for row in rows]

    def decide_candidate(
        self,
        candidate_id: str,
        status: CandidateStatus,
        *,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if status not in {"approved", "rejected", "rolled_back"}:
            raise ValueError("candidate decision must be approved, rejected, or rolled_back")
        if not actor.strip():
            raise ValueError("actor is required")
        safe_actor = str(sanitize_evidence(actor.strip()))[:120]
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM improvement_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not row:
                raise KeyError(candidate_id)
            current = str(row["status"])
            allowed = {
                "pending": {"approved", "rejected"},
                "approved": {"rolled_back"},
                "rejected": set(),
                "rolled_back": set(),
            }
            if status not in allowed.get(current, set()):
                raise ValueError(f"invalid candidate transition: {current} -> {status}")
            now = iso_now()
            conn.execute(
                "UPDATE improvement_candidates SET status=?,updated_at=?,decided_at=?,decided_by=? "
                "WHERE candidate_id=?",
                (status, now, now, safe_actor, candidate_id),
            )
            conn.execute(
                "INSERT INTO candidate_audit(candidate_id,from_status,to_status,actor,reason,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (candidate_id, current, status, safe_actor, sanitize_evidence(reason), now),
            )
            updated = conn.execute(
                "SELECT * FROM improvement_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        return _candidate_row(updated)

    def candidate_audit(self, candidate_id: str) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT candidate_id,from_status,to_status,actor,reason,created_at "
                "FROM candidate_audit WHERE candidate_id=? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        return [dict(row) for row in rows]


class OpenMeteoTruthClient:
    def __init__(self, api_url: str, timeout_seconds: float = 20.0):
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds

    async def fetch(self, latitude: float, longitude: float, target_date: str) -> ObservedWeather:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": target_date,
            "end_date": target_date,
            "daily": ",".join(
                [
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                ]
            ),
            "timezone": "Asia/Shanghai",
            "wind_speed_unit": "ms",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(self.api_url, params=params)
            response.raise_for_status()
            payload = response.json()
        daily = payload.get("daily") if isinstance(payload, dict) else None
        if not isinstance(daily, dict):
            raise ValueError("truth_daily_missing")
        times = daily.get("time") or []
        try:
            index = times.index(target_date)
        except (AttributeError, ValueError) as exc:
            raise ValueError("truth_target_date_missing") from exc
        max_temperature = _series_float(daily, "temperature_2m_max", index)
        min_temperature = _series_float(daily, "temperature_2m_min", index)
        precipitation_sum = _series_float(daily, "precipitation_sum", index)
        wind_speed = _series_float(daily, "wind_speed_10m_max", index)
        return ObservedWeather(
            target_date=target_date,
            max_temperature=max_temperature,
            min_temperature=min_temperature,
            precipitation_sum=precipitation_sum,
            rain_observed=precipitation_sum >= 0.1,
            wind_speed=wind_speed,
            fetched_at=iso_now(),
        )


def score_provider_forecast(
    provider_result: ProviderForecast,
    truth: ObservedWeather,
    *,
    region: str,
    target_date: str,
    horizon_days: int,
) -> ProviderScore | None:
    if provider_result.status != "ok" or not provider_result.points:
        return None
    temperatures = [point.temperature for point in provider_result.points if point.temperature is not None]
    rains = [
        point.precipitation_probability
        for point in provider_result.points
        if point.precipitation_probability is not None
    ]
    winds = [point.wind_speed for point in provider_result.points if point.wind_speed is not None]

    max_error = round(abs(max(temperatures) - truth.max_temperature), 3) if temperatures else None
    min_error = round(abs(min(temperatures) - truth.min_temperature), 3) if temperatures else None
    temperature_mae = (
        round(mean([max_error, min_error]), 3)
        if max_error is not None and min_error is not None
        else None
    )
    rain_hit = (max(rains) >= 50.0) == truth.rain_observed if rains else None
    wind_error = round(abs(max(winds) - truth.wind_speed), 3) if winds else None

    weighted_scores: list[tuple[float, float]] = []
    if temperature_mae is not None:
        weighted_scores.append((_bounded_score(temperature_mae, 10.0), 0.45))
    if rain_hit is not None:
        weighted_scores.append((100.0 if rain_hit else 40.0, 0.35))
    if wind_error is not None:
        weighted_scores.append((_bounded_score(wind_error, 15.0), 0.20))
    if not weighted_scores:
        return None
    weight_total = sum(weight for _score, weight in weighted_scores)
    total_score = round(sum(score * weight for score, weight in weighted_scores) / weight_total, 2)
    return ProviderScore(
        provider=provider_result.provider,
        region=region,
        target_date=target_date,
        horizon_days=max(0, horizon_days),
        temperature_mae=temperature_mae,
        max_temperature_error=max_error,
        min_temperature_error=min_error,
        rain_hit=rain_hit,
        wind_speed_error=wind_error,
        total_score=total_score,
        truth_source=truth.source,
    )


async def verify_due_snapshots(
    store: ControlledLearningStore,
    truth_client: OpenMeteoTruthClient,
    *,
    today: date | None = None,
    truth_delay_days: int = 1,
    limit: int = 100,
) -> dict[str, int]:
    current_day = today or datetime.now(SHANGHAI_TZ).date()
    due_date = current_day - timedelta(days=max(0, truth_delay_days))
    snapshots = store.pending_snapshots(due_date, limit=limit)
    evaluated = deferred = skipped = 0
    truth_cache: dict[tuple[float, float, str], ObservedWeather] = {}
    for snapshot in snapshots:
        snapshot_id = int(snapshot["id"])
        latitude = _as_float(snapshot.get("latitude"))
        longitude = _as_float(snapshot.get("longitude"))
        if latitude is None or longitude is None:
            store.mark_snapshot_skipped(snapshot_id, "coordinates_missing")
            store.record_signal(
                "truth_verification_deferred",
                "objective_verification",
                "warning",
                {
                    "snapshot_id": snapshot_id,
                    "target_date": snapshot["target_date"],
                    "reason": "coordinates_missing",
                },
            )
            skipped += 1
            continue
        try:
            truth_key = (round(latitude, 5), round(longitude, 5), str(snapshot["target_date"]))
            truth = truth_cache.get(truth_key)
            if truth is None:
                truth = await truth_client.fetch(latitude, longitude, str(snapshot["target_date"]))
                truth_cache[truth_key] = truth
            submission = WeatherSubmission.model_validate_json(snapshot["submission_json"])
            captured_date = datetime.fromisoformat(str(snapshot["captured_at"])).astimezone(SHANGHAI_TZ).date()
            horizon_days = (date.fromisoformat(submission.target_date) - captured_date).days
            scores = [
                score
                for result in submission.provider_results
                if (
                    score := score_provider_forecast(
                        result,
                        truth,
                        region=submission.region,
                        target_date=submission.target_date,
                        horizon_days=horizon_days,
                    )
                )
                is not None
            ]
            if not scores:
                store.mark_snapshot_skipped(snapshot_id, "provider_scores_missing")
                skipped += 1
                continue
            store.mark_snapshot_evaluated(snapshot_id, scores, truth.source)
            evaluated += 1
        except Exception as exc:  # noqa: BLE001 - delayed truth is retried on the next cycle
            error_code = type(exc).__name__
            store.mark_snapshot_error(snapshot_id, error_code)
            store.record_signal(
                "truth_verification_deferred",
                "objective_verification",
                "warning",
                {
                    "snapshot_id": snapshot_id,
                    "target_date": snapshot["target_date"],
                    "reason": error_code,
                },
            )
            deferred += 1
    return {
        "due": len(snapshots),
        "evaluated": evaluated,
        "deferred": deferred,
        "skipped": skipped,
    }


def generate_improvement_candidates(
    store: ControlledLearningStore,
    replay_results: list[ReplayCaseResult],
    *,
    min_provider_samples: int,
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    failed_by_category: dict[str, list[ReplayCaseResult]] = defaultdict(list)
    for result in replay_results:
        if not result.passed:
            failed_by_category[result.category].append(result)

    type_by_category = {
        "intent": "intent_priority_review",
        "location": "location_scope_review",
        "date": "date_parser_review",
        "metric": "metric_parser_review",
        "context": "context_isolation_review",
        "group_gate": "group_addressing_gate_review",
        "task_routing": "intent_priority_review",
    }
    recommendation_by_category = {
        "intent": "核对确定性意图优先级，并把失败输入加入结构化意图回归集",
        "location": "核对地点别名、行政区范围和原文依据，不允许未经验证的地点进入天气接口",
        "date": "核对统一日期窗口解析，避免日期片段被识别为地点",
        "metric": "核对支持与不支持指标路由，不支持指标不得调用预报接口",
        "context": "核对用户、群、线程和机器人隔离，以及仅继承最近成功请求的规则",
        "group_gate": "保持真实结构化 mention 或已记录机器人回复的严格寻址门禁",
        "task_routing": "核对天气查询与任务动作优先级，避免跨机器人抢占",
    }
    for category, failures in sorted(failed_by_category.items()):
        candidate_type = type_by_category.get(category, "replay_behavior_review")
        case_ids = sorted(item.case_id for item in failures)
        created.append(
            store.create_candidate(
                candidate_type,
                {
                    "category": category,
                    "action": "review_failed_replay_cases",
                    "evidence_revision": _fingerprint(
                        case_ids,
                        sorted({m for item in failures for m in item.mismatches}),
                    )[:12],
                    "suggested_change": recommendation_by_category.get(
                        category, "核对失败案例的首个错误层级并补充回归保护"
                    ),
                    "runtime_effect": "none",
                },
                {
                    "case_ids": case_ids,
                    "failure_count": len(failures),
                    "mismatches": sorted({m for item in failures for m in item.mismatches}),
                    "examples": [
                        {
                            "case_id": item.case_id,
                            "expected": item.expected.model_dump(mode="json"),
                            "actual": item.actual.model_dump(mode="json"),
                        }
                        for item in failures[:10]
                    ],
                },
            )
        )

    summaries = store.provider_summary()
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in summaries:
        groups[(str(item["region"]), int(item["horizon_days"]))].append(item)
    for (region, horizon_days), group in sorted(groups.items()):
        eligible = [item for item in group if int(item["sample_count"]) >= min_provider_samples]
        if len(eligible) < 2:
            continue
        proposed: dict[str, dict[str, float]] = {}
        for metric, quality_field, lower_is_better in (
            ("temperature", "temperature_mae", True),
            ("precipitation_probability", "rain_accuracy", False),
            ("wind_speed", "wind_speed_mae", True),
        ):
            qualities: dict[str, float] = {}
            for item in eligible:
                value = _as_float(item.get(quality_field))
                if value is None:
                    continue
                qualities[str(item["provider"])] = 1.0 / (0.25 + value) if lower_is_better else max(0.05, value)
            if len(qualities) >= 2:
                proposed[metric] = _normalize_weights(qualities)
        if not proposed:
            continue
        created.append(
            store.create_candidate(
                "provider_weight_review",
                {
                    "region": region,
                    "horizon_days": horizon_days,
                    "proposed_weights": proposed,
                    "runtime_effect": "none",
                },
                {
                    "minimum_samples": min_provider_samples,
                    "provider_summaries": eligible,
                    "warning": "candidate requires independent regression and explicit release",
                },
            )
        )
    return created


def render_learning_report(report: dict[str, Any]) -> str:
    replay = report.get("replay", {})
    verification = report.get("verification", {})
    snapshots = report.get("snapshots", {})
    provider_summaries = report.get("provider_summaries", [])
    candidates = report.get("candidates", [])
    lines = [
        "# 云云受控持续学习报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 版本：{report.get('version', LEARNING_VERSION)}",
        "- 运行边界：只归档、评测和生成候选；不发飞书、不改规则、不部署",
        "",
        "## 自动回放",
        "",
        f"- 总数：{replay.get('total', 0)}",
        f"- 通过：{replay.get('passed', 0)}",
        f"- 失败：{replay.get('failed', 0)}",
        "",
        "## 预报实况验证",
        "",
        "- 参考口径：Open-Meteo 历史格点/再分析参考天气，不等同于官方站点实况",
        f"- 本轮到期：{verification.get('due', 0)}",
        f"- 已评分：{verification.get('evaluated', 0)}",
        f"- 延后重试：{verification.get('deferred', 0)}",
        f"- 无法评分：{verification.get('skipped', 0)}",
        f"- 快照累计：待评估 {snapshots.get('pending', 0)}，已评估 {snapshots.get('evaluated', 0)}，已跳过 {snapshots.get('skipped', 0)}",
        "",
        "## 数据源证据",
        "",
    ]
    if provider_summaries:
        for item in provider_summaries:
            display_item = dict(item)
            display_item.setdefault("evidence_status", "不足")
            lines.append(
                "- {provider}｜{region}｜提前 {horizon_days} 天｜样本 {sample_count}｜"
                "温度 MAE {temperature_mae}｜降水命中率 {rain_accuracy}｜风速 MAE {wind_speed_mae}｜"
                "综合 {total_score}｜证据 {evidence_status}".format(
                    **{key: _display_number(value) for key, value in display_item.items()}
                )
            )
    else:
        lines.append("- 暂无足够的到期预报与实况配对数据。")
    lines.extend(["", "## 待审核候选", ""])
    pending = [item for item in candidates if item.get("status") == "pending"]
    if pending:
        for item in pending:
            lines.append(f"- {item['candidate_id']}｜{item['candidate_type']}｜仅供审核，未生效")
    else:
        lines.append("- 本轮没有待审核候选。")
    lines.extend(
        [
            "",
            "## 安全声明",
            "",
            "本报告不会触发飞书消息、配置变更、代码修改或部署。候选即使被标记为 approved，也仍需单独实施、测试和发布。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_learning_report(report: dict[str, Any], report_dir: str) -> dict[str, str]:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    json_path = directory / f"controlled-learning-{stamp}.json"
    markdown_path = directory / f"controlled-learning-{stamp}.md"
    json_text = json.dumps(sanitize_evidence(report), ensure_ascii=False, indent=2)
    markdown_text = render_learning_report(report)
    json_path.write_text(json_text + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_text, encoding="utf-8")
    (directory / "latest.json").write_text(json_text + "\n", encoding="utf-8")
    (directory / "latest.md").write_text(markdown_text, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _candidate_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "candidate_id": row["candidate_id"],
        "candidate_type": row["candidate_type"],
        "status": row["status"],
        "payload": json.loads(row["payload_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
    }


def _series_float(daily: dict[str, Any], key: str, index: int) -> float:
    series = daily.get(key)
    if not isinstance(series, list) or index >= len(series) or series[index] is None:
        raise ValueError(f"truth_{key}_missing")
    return float(series[index])


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bounded_score(error: float, penalty_per_unit: float) -> float:
    return round(max(0.0, 100.0 - error * penalty_per_unit), 2)


def _normalize_weights(qualities: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, value) for value in qualities.values())
    if total <= 0:
        return {}
    return {provider: round(max(0.0, value) / total, 4) for provider, value in sorted(qualities.items())}


def _display_number(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    if value is None:
        return "证据不足"
    return value
