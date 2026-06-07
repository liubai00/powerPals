from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class JsonlRecorder:
    def __init__(self, path: str):
        self.path = Path(path)

    def append(self, record: BaseModel) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump(mode="json")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
