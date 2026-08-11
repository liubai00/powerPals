from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from services.weather_bot import controlled_learning_report_cli as report_cli
from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning_report_cli import main as report_cli_main
from services.weather_bot.controlled_learning_reporting import (
    publish_controlled_learning_report,
    publish_latest_controlled_learning_report,
)


class FakeFeishuClient:
    def __init__(self, chats: list[dict[str, str]]) -> None:
        self.chats = chats
        self.list_calls = 0
        self.sends: list[tuple[str, dict[str, Any], str | None]] = []

    async def list_chats(self) -> list[dict[str, str]]:
        self.list_calls += 1
        return self.chats

    async def send_interactive_card(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        self.sends.append((chat_id, card, idempotency_key))
        return "om-learning-report"


class FailingChatLookupClient(FakeFeishuClient):
    async def list_chats(self) -> list[dict[str, str]]:
        self.list_calls += 1
        raise RuntimeError("secret upstream response must not escape")


def _report() -> dict[str, Any]:
    return {
        "version": "controlled_learning_v1",
        "generated_at": "2026-08-10T02:31:00+08:00",
        "replay": {
            "run_id": "run-20260810",
            "total": 29,
            "passed": 29,
            "failed": 0,
            "failed_cases": [{"raw_text": "不得出现在群摘要中的原始消息"}],
        },
        "core_gate": {
            "total": 96,
            "passed": 96,
            "failed": 0,
            "not_implemented": 0,
            "blocked": 0,
            "gate_passed": True,
        },
        "verification": {"due": 3, "evaluated": 2, "deferred": 1, "skipped": 0},
        "snapshots": {"pending": 4, "evaluated": 20, "skipped": 1},
        "generated_candidates": [{"candidate_id": "cand-secret-payload"}],
        "candidates": [{"candidate_id": "cand-pending", "status": "pending"}],
    }


@pytest.mark.asyncio
async def test_unique_exact_test_group_receives_one_privacy_minimized_learning_summary(
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        dry_run=False,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FakeFeishuClient(
        [
            {"chat_id": "oc_other", "name": "Test", "chat_mode": "group"},
            {"chat_id": "oc_test", "name": "test", "chat_mode": "group"},
        ]
    )

    result = await publish_controlled_learning_report(
        settings,
        _report(),
        feishu_client=feishu,
        now=datetime.fromisoformat("2026-08-10T02:35:00+08:00"),
    )

    assert result == {
        "status": "sent",
        "reason": "sent",
        "target_name": "test",
        "message_id": "om-learning-report",
    }
    assert len(feishu.sends) == 1
    chat_id, card, idempotency_key = feishu.sends[0]
    assert chat_id == "oc_test"
    assert idempotency_key
    rendered = str(card)
    assert "核心门禁：96/96 通过" in rendered
    assert "自动回放：29/29 通过" in rendered
    assert "实况验证：到期 3，已评分 2，延后 1，跳过 0" in rendered
    assert "待审核候选：1 个" in rendered
    assert "不得出现在群摘要中的原始消息" not in rendered
    assert "cand-secret-payload" not in rendered


@pytest.mark.asyncio
async def test_stale_latest_learning_report_is_not_resolved_or_sent(tmp_path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    stale = _report() | {"generated_at": "2026-08-09T02:31:00+08:00"}
    (report_dir / "latest.json").write_text(
        json.dumps(stale, ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        dry_run=False,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_dir=str(report_dir),
        controlled_learning_report_max_age_seconds=2 * 60 * 60,
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FakeFeishuClient(
        [{"chat_id": "oc_test", "name": "test", "chat_mode": "group"}]
    )

    result = await publish_latest_controlled_learning_report(
        settings,
        feishu_client=feishu,
        now=datetime.fromisoformat("2026-08-10T03:00:00+08:00"),
    )

    assert result == {
        "status": "not_sent",
        "reason": "report_stale",
        "target_name": "test",
        "message_id": "",
    }
    assert feishu.list_calls == 0
    assert feishu.sends == []


@pytest.mark.asyncio
async def test_same_learning_run_is_sent_to_test_group_at_most_once(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        dry_run=False,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FakeFeishuClient(
        [{"chat_id": "oc_test", "name": "test", "chat_mode": "group"}]
    )

    first = await publish_controlled_learning_report(
        settings,
        _report(),
        feishu_client=feishu,
        now=datetime.fromisoformat("2026-08-10T02:35:00+08:00"),
    )
    repeated = await publish_controlled_learning_report(
        settings,
        _report(),
        feishu_client=feishu,
        now=datetime.fromisoformat("2026-08-10T02:36:00+08:00"),
    )

    assert first["status"] == "sent"
    assert repeated == {
        "status": "not_sent",
        "reason": "already_sent_or_in_progress",
        "target_name": "test",
        "message_id": "",
    }
    assert len(feishu.sends) == 1


def test_report_cli_publishes_only_the_fresh_latest_report(capsys, tmp_path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "latest.json").write_text(
        json.dumps(_report(), ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        dry_run=False,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_dir=str(report_dir),
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FakeFeishuClient(
        [{"chat_id": "oc_test", "name": "test", "chat_mode": "group"}]
    )

    exit_code = report_cli_main(
        settings=settings,
        feishu_client=feishu,
        now=datetime.fromisoformat("2026-08-10T02:35:00+08:00"),
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "sent",
        "reason": "sent",
        "target_name": "test",
        "message_id": "recorded",
    }
    assert len(feishu.sends) == 1


def test_report_cli_can_check_unique_test_target_without_reading_or_sending_report(
    capsys,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=False,
        dry_run=True,
        controlled_learning_report_send_enabled=False,
        controlled_learning_report_chat_name="test",
    )
    feishu = FakeFeishuClient(
        [
            {"chat_id": "oc_other", "name": "other", "chat_mode": "group"},
            {"chat_id": "oc_test", "name": "test", "chat_mode": "group"},
        ]
    )

    exit_code = report_cli_main(
        ["--check-target"],
        settings=settings,
        feishu_client=feishu,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "ready",
        "reason": "unique_exact_target",
        "target_name": "test",
        "match_count": 1,
    }
    assert feishu.list_calls == 1
    assert feishu.sends == []


def test_report_cli_reads_process_arguments_when_invoked_as_a_module(
    capsys,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        controlled_learning_report_chat_name="test",
    )
    feishu = FakeFeishuClient(
        [{"chat_id": "oc_test", "name": "test", "chat_mode": "group"}]
    )
    monkeypatch.setattr(report_cli.sys, "argv", ["controlled-learning-report", "--check-target"])

    exit_code = report_cli.main(settings=settings, feishu_client=feishu)

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "unique_exact_target"
    assert feishu.list_calls == 1
    assert feishu.sends == []


@pytest.mark.asyncio
async def test_chat_lookup_failure_is_fail_closed_without_leaking_provider_detail(
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        dry_run=False,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FailingChatLookupClient([])

    result = await publish_controlled_learning_report(
        settings,
        _report(),
        feishu_client=feishu,
        now=datetime.fromisoformat("2026-08-10T02:35:00+08:00"),
    )

    assert result == {
        "status": "not_sent",
        "reason": "target_resolution_failed",
        "target_name": "test",
        "message_id": "",
    }
    assert "secret upstream" not in str(result)
    assert feishu.sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"dry_run": True}, "dry_run"),
        ({"global_feishu_send_enabled": False}, "global_send_disabled"),
        (
            {"controlled_learning_report_send_enabled": False},
            "learning_report_send_disabled",
        ),
    ],
)
async def test_each_learning_report_send_gate_prevents_lookup_and_delivery(
    tmp_path,
    overrides,
    reason,
) -> None:
    values = {
        "app_env": "test",
        "global_feishu_send_enabled": True,
        "dry_run": False,
        "controlled_learning_report_send_enabled": True,
        "controlled_learning_report_chat_name": "test",
        "controlled_learning_report_delivery_db": str(tmp_path / "deliveries.db"),
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    feishu = FakeFeishuClient(
        [{"chat_id": "oc_test", "name": "test", "chat_mode": "group"}]
    )

    result = await publish_controlled_learning_report(
        settings,
        _report(),
        feishu_client=feishu,
    )

    assert result["reason"] == reason
    assert feishu.list_calls == 0
    assert feishu.sends == []


@pytest.mark.asyncio
async def test_duplicate_exact_test_group_names_fail_closed(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FakeFeishuClient(
        [
            {"chat_id": "oc_test_1", "name": "test", "chat_mode": "group"},
            {"chat_id": "oc_test_2", "name": "test", "chat_mode": "group"},
        ]
    )

    result = await publish_controlled_learning_report(
        settings,
        _report(),
        feishu_client=feishu,
    )

    assert result["reason"] == "target_name_ambiguous"
    assert feishu.sends == []


@pytest.mark.asyncio
async def test_report_without_stable_run_identity_makes_no_feishu_request(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        global_feishu_send_enabled=True,
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
        controlled_learning_report_delivery_db=str(tmp_path / "deliveries.db"),
    )
    feishu = FakeFeishuClient(
        [{"chat_id": "oc_test", "name": "test", "chat_mode": "group"}]
    )
    invalid_report = _report()
    invalid_report["replay"] = {"total": 29, "passed": 29, "failed": 0}

    result = await publish_controlled_learning_report(
        settings,
        invalid_report,
        feishu_client=feishu,
    )

    assert result["reason"] == "report_identity_missing"
    assert feishu.list_calls == 0
    assert feishu.sends == []


def test_learning_report_has_a_separate_schedule_without_overriding_send_gates() -> None:
    cron = Path("deploy/controlled_learning_report.cron").read_text(encoding="utf-8")

    assert "production host timezone: UTC" in cron
    assert "CRON_TZ=Asia/Shanghai" not in cron
    assert "0 3 * * *" in cron
    assert "controlled_learning_report_cli --check-target" not in cron
    assert "controlled_learning_report_cli" in cron
    assert "CONTROLLED_LEARNING_REPORT_SEND_ENABLED=true" not in cron
    assert "GLOBAL_FEISHU_SEND_ENABLED=true" not in cron
    assert "DRY_RUN=false" not in cron


def test_controlled_learning_schedule_is_explicitly_beijing_time() -> None:
    cron = Path("deploy/controlled_learning.cron").read_text(encoding="utf-8")

    assert "production host timezone: UTC" in cron
    assert "CRON_TZ=Asia/Shanghai" not in cron
    assert "30 2 * * *" in cron
