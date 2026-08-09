from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class JsonlRecorder:
    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def append(self, record: BaseModel) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Submission records can contain provider and aggregate hourly series.
        # Persistence keeps only the retention-policy-safe derivative/provenance copy.
        from services.weather_bot.models import SubmissionRecord

        if isinstance(record, SubmissionRecord):
            from services.weather_bot.data_minimization import minimize_submission_for_storage

            record = record.model_copy(
                update={"submission": minimize_submission_for_storage(record.submission)}
            )
        payload = record.model_dump(mode="json")
        if "submission" in payload:
            expiry = _submission_expiry(payload)
            if expiry is None or expiry <= _aware_now(self.clock):
                raise ValueError(
                    "submission persistence requires a future retention_expires_at"
                )
        self.read_json_objects()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_json_objects(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        rewrite_required = False
        now = _aware_now(self.clock)
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    rewrite_required = True
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    rewrite_required = True
                    continue
                if isinstance(payload, dict):
                    if "submission" in payload:
                        expiry = _submission_expiry(payload)
                        if expiry is None or expiry <= now:
                            rewrite_required = True
                            continue
                    records.append(payload)
                else:
                    rewrite_required = True
        if rewrite_required:
            with self.path.open("w", encoding="utf-8") as handle:
                for payload in records:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return records


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("JsonlRecorder clock must return a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _submission_expiry(payload: dict[str, Any]) -> datetime | None:
    submission = payload.get("submission")
    if not isinstance(submission, dict):
        return None
    value = submission.get("retention_expires_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return None
    return expiry.astimezone(timezone.utc)
