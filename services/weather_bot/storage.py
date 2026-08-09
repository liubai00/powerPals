from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class JsonlRecorder:
    def __init__(self, path: str):
        self.path = Path(path)

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
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_json_objects(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        return records
