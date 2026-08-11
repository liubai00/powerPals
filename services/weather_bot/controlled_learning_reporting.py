"""Audited, opt-in delivery of privacy-minimized controlled-learning summaries."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuBotAccount, FeishuClient


def build_controlled_learning_summary_card(report: dict[str, Any]) -> dict[str, Any]:
    replay = report.get("replay") if isinstance(report.get("replay"), dict) else {}
    core = report.get("core_gate") if isinstance(report.get("core_gate"), dict) else {}
    verification = (
        report.get("verification") if isinstance(report.get("verification"), dict) else {}
    )
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    pending_candidates = sum(
        1
        for item in candidates
        if isinstance(item, dict) and str(item.get("status") or "") == "pending"
    )
    generated_at = str(report.get("generated_at") or "未记录")
    gate_label = "通过" if core.get("gate_passed") is True else "未通过"
    content = "\n".join(
        [
            f"生成时间：{generated_at}",
            f"核心门禁：{_count(core, 'passed')}/{_count(core, 'total')} 通过（门禁{gate_label}）",
            f"自动回放：{_count(replay, 'passed')}/{_count(replay, 'total')} 通过",
            (
                "实况验证：到期 {due}，已评分 {evaluated}，延后 {deferred}，跳过 {skipped}"
            ).format(
                due=_count(verification, "due"),
                evaluated=_count(verification, "evaluated"),
                deferred=_count(verification, "deferred"),
                skipped=_count(verification, "skipped"),
            ),
            f"待审核候选：{pending_candidates} 个",
            "边界：仅汇报评测结果；不自动改规则、不自动部署。",
        ]
    )
    return {
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green" if core.get("gate_passed") is True else "red",
                "title": {"tag": "plain_text", "content": "🧪 云云受控学习运行摘要"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content},
                }
            ],
        }
    }


async def publish_controlled_learning_report(
    settings: Settings,
    report: dict[str, Any],
    *,
    feishu_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    target_name = str(settings.controlled_learning_report_chat_name or "").strip()
    blocked_reason = _send_block_reason(settings, target_name)
    if blocked_reason:
        return _result("not_sent", blocked_reason, target_name)
    replay = report.get("replay") if isinstance(report.get("replay"), dict) else {}
    run_id = str(replay.get("run_id") or "").strip()
    generated_at = str(report.get("generated_at") or "").strip()
    if not run_id or not generated_at:
        return _result("not_sent", "report_identity_missing", target_name)

    feishu = feishu_client or _weather_feishu_client(settings)
    try:
        chats = await feishu.list_chats()
    except Exception:  # noqa: BLE001 - do not expose provider error details
        return _result("not_sent", "target_resolution_failed", target_name)
    exact_matches = [
        item
        for item in chats
        if isinstance(item, dict)
        and str(item.get("name") or "") == target_name
        and str(item.get("chat_mode") or "") in {"group", "topic"}
        and str(item.get("chat_id") or "").strip()
    ]
    if not exact_matches:
        return _result("not_sent", "target_not_found", target_name)
    if len(exact_matches) != 1:
        return _result("not_sent", "target_name_ambiguous", target_name)

    chat_id = str(exact_matches[0]["chat_id"])
    idempotency_key = str(
        uuid5(NAMESPACE_URL, f"controlled-learning-report:{run_id}:{generated_at}:{chat_id}")
    )
    delivery_key = hashlib.sha256(
        f"controlled-learning-report:{run_id}:{generated_at}:{chat_id}".encode("utf-8")
    ).hexdigest()
    owner_token = uuid4().hex
    ledger = ControlledLearningReportDeliveryStore(
        settings.controlled_learning_report_delivery_db
    )
    if not ledger.claim(delivery_key, owner_token, now=now):
        return _result("not_sent", "already_sent_or_in_progress", target_name)
    try:
        message_id = await feishu.send_interactive_card(
            chat_id,
            build_controlled_learning_summary_card(report),
            idempotency_key=idempotency_key,
        )
    except Exception:  # noqa: BLE001 - external errors are reduced to a stable status
        ledger.fail(delivery_key, owner_token, now=now)
        return _result("not_sent", "send_failed", target_name)
    ledger.complete(delivery_key, owner_token, now=now)
    return {
        "status": "sent",
        "reason": "sent",
        "target_name": target_name,
        "message_id": str(message_id or ""),
    }


async def publish_latest_controlled_learning_report(
    settings: Settings,
    *,
    feishu_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    target_name = str(settings.controlled_learning_report_chat_name or "").strip()
    blocked_reason = _send_block_reason(settings, target_name)
    if blocked_reason:
        return _result("not_sent", blocked_reason, target_name)
    latest_path = Path(settings.controlled_learning_report_dir) / "latest.json"
    if not latest_path.is_file():
        return _result("not_sent", "report_missing", target_name)
    try:
        report = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _result("not_sent", "report_invalid", target_name)
    if not isinstance(report, dict):
        return _result("not_sent", "report_invalid", target_name)
    generated_at = _aware_datetime(report.get("generated_at"))
    current = now or datetime.now().astimezone()
    if generated_at is None or current.tzinfo is None or current.utcoffset() is None:
        return _result("not_sent", "report_time_invalid", target_name)
    age_seconds = (current - generated_at).total_seconds()
    if age_seconds < -300:
        return _result("not_sent", "report_time_in_future", target_name)
    if age_seconds > max(1, int(settings.controlled_learning_report_max_age_seconds)):
        return _result("not_sent", "report_stale", target_name)
    return await publish_controlled_learning_report(
        settings,
        report,
        feishu_client=feishu_client,
        now=current,
    )


async def check_controlled_learning_report_target(
    settings: Settings,
    *,
    feishu_client: Any | None = None,
) -> dict[str, object]:
    """Resolve the configured group name without sending or exposing chat IDs."""
    target_name = str(settings.controlled_learning_report_chat_name or "").strip()
    if not target_name:
        return {
            "status": "blocked",
            "reason": "target_name_not_configured",
            "target_name": "",
            "match_count": 0,
        }
    feishu = feishu_client or _weather_feishu_client(settings)
    try:
        chats = await feishu.list_chats()
    except Exception:  # noqa: BLE001 - preflight output must remain secret-safe
        return {
            "status": "blocked",
            "reason": "target_resolution_failed",
            "target_name": target_name,
            "match_count": 0,
        }
    matches = [
        item
        for item in chats
        if isinstance(item, dict)
        and str(item.get("name") or "") == target_name
        and str(item.get("chat_mode") or "") in {"group", "topic"}
        and str(item.get("chat_id") or "").strip()
    ]
    if len(matches) == 1:
        return {
            "status": "ready",
            "reason": "unique_exact_target",
            "target_name": target_name,
            "match_count": 1,
        }
    return {
        "status": "blocked",
        "reason": "target_not_found" if not matches else "target_name_ambiguous",
        "target_name": target_name,
        "match_count": len(matches),
    }


def _weather_feishu_client(settings: Settings) -> FeishuClient:
    account = FeishuBotAccount(
        app_id=settings.feishu_weather_app_id or settings.feishu_app_id,
        app_secret=settings.feishu_weather_app_secret or settings.feishu_app_secret,
        verification_token=settings.feishu_weather_verification_token,
        encrypt_key=settings.feishu_weather_encrypt_key,
        bot_open_id=settings.feishu_weather_bot_open_id,
        name="weather",
    )
    return FeishuClient(settings, account)


class ControlledLearningReportDeliveryStore:
    """Small hashed-identity ledger preventing duplicate external deliveries."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS controlled_learning_report_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def claim(
        self,
        delivery_key: str,
        owner_token: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> bool:
        current = _timestamp(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, lease_until FROM controlled_learning_report_deliveries "
                "WHERE delivery_key = ?",
                (delivery_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO controlled_learning_report_deliveries "
                    "(delivery_key, status, owner_token, lease_until, created_at, updated_at) "
                    "VALUES (?, 'in_progress', ?, ?, ?, ?)",
                    (delivery_key, owner_token, current + lease_seconds, current, current),
                )
                return True
            status, lease_until = str(row[0]), float(row[1])
            if status == "succeeded" or (status == "in_progress" and lease_until > current):
                return False
            conn.execute(
                "UPDATE controlled_learning_report_deliveries "
                "SET status = 'in_progress', owner_token = ?, lease_until = ?, updated_at = ? "
                "WHERE delivery_key = ?",
                (owner_token, current + lease_seconds, current, delivery_key),
            )
            return True

    def complete(
        self,
        delivery_key: str,
        owner_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE controlled_learning_report_deliveries "
                "SET status = 'succeeded', lease_until = 0, updated_at = ? "
                "WHERE delivery_key = ? AND owner_token = ? AND status = 'in_progress'",
                (_timestamp(now), delivery_key, owner_token),
            )

    def fail(
        self,
        delivery_key: str,
        owner_token: str,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE controlled_learning_report_deliveries "
                "SET status = 'failed', lease_until = 0, updated_at = ? "
                "WHERE delivery_key = ? AND owner_token = ? AND status = 'in_progress'",
                (_timestamp(now), delivery_key, owner_token),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)


def _send_block_reason(settings: Settings, target_name: str) -> str | None:
    if settings.dry_run:
        return "dry_run"
    if not settings.global_feishu_send_enabled:
        return "global_send_disabled"
    if not settings.controlled_learning_report_send_enabled:
        return "learning_report_send_disabled"
    if not target_name:
        return "target_name_not_configured"
    return None


def _count(values: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(values.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _result(status: str, reason: str, target_name: str) -> dict[str, str]:
    return {
        "status": status,
        "reason": reason,
        "target_name": target_name,
        "message_id": "",
    }


def _timestamp(value: datetime | None) -> float:
    return (value or datetime.now().astimezone()).timestamp()


def _aware_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed
