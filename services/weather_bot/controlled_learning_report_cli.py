"""Administrative entry point for the separate controlled-learning report publisher."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import sys
from typing import Any

from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning_reporting import (
    check_controlled_learning_report_target,
    publish_latest_controlled_learning_report,
)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    feishu_client: Any | None = None,
    now: datetime | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    active_settings = settings or Settings()
    if "--check-target" in arguments:
        checked = asyncio.run(
            check_controlled_learning_report_target(
                active_settings,
                feishu_client=feishu_client,
            )
        )
        sys.stdout.write(json.dumps(checked, ensure_ascii=False) + "\n")
        return 0 if checked.get("status") == "ready" else 2
    result = asyncio.run(
        publish_latest_controlled_learning_report(
            active_settings,
            feishu_client=feishu_client,
            now=now,
        )
    )
    safe_result = dict(result)
    safe_result["message_id"] = "recorded" if result.get("message_id") else ""
    sys.stdout.write(json.dumps(safe_result, ensure_ascii=False) + "\n")
    if result.get("status") == "sent" or result.get("reason") in {
        "already_sent_or_in_progress",
        "dry_run",
        "global_send_disabled",
        "learning_report_send_disabled",
    }:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
