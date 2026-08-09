"""Administrative CLI for the offline controlled-learning cycle."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning import (
    LEARNING_VERSION,
    ControlledLearningStore,
    OpenMeteoTruthClient,
    ReplayCase,
    generate_improvement_candidates,
    iso_now,
    verify_due_snapshots,
    write_learning_report,
)
from services.weather_bot.controlled_learning_replay import run_deterministic_replay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="云云受控持续学习：只评测和生成候选，不发送消息或修改运行时行为。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="运行回放、到期实况评分和候选报告")
    run.add_argument("--skip-truth", action="store_true", help="跳过外部实况读取")
    run.add_argument("--strict", action="store_true", help="回放失败时返回非零退出码")
    run.add_argument("--truth-limit", type=int, default=100)

    add_case = subparsers.add_parser("add-case", help="从结构化 JSON 文件增加管理员案例")
    add_case.add_argument("--file", required=True, help="ReplayCase JSON 文件")

    list_cases = subparsers.add_parser("list-cases", help="列出管理员案例")
    list_cases.add_argument("--all", action="store_true", help="包括停用案例")

    candidates = subparsers.add_parser("candidates", help="列出改进候选")
    candidates.add_argument(
        "--status", choices=["pending", "approved", "rejected", "rolled_back"]
    )

    decide = subparsers.add_parser("decide", help="记录候选审批、拒绝或回滚")
    decide.add_argument("candidate_id")
    decide.add_argument("--status", required=True, choices=["approved", "rejected", "rolled_back"])
    decide.add_argument("--actor", required=True)
    decide.add_argument("--reason", default="")

    audit = subparsers.add_parser("audit", help="查看候选状态审计")
    audit.add_argument("candidate_id")
    return parser


async def run_cycle(settings: Settings, *, skip_truth: bool, truth_limit: int) -> dict[str, Any]:
    store = ControlledLearningStore(settings.controlled_learning_db)
    run_id, replay_results = run_deterministic_replay(store)
    failed = [item for item in replay_results if not item.passed]
    verification = {"due": 0, "evaluated": 0, "deferred": 0, "skipped": 0}
    if not skip_truth:
        verification = await verify_due_snapshots(
            store,
            OpenMeteoTruthClient(settings.controlled_learning_archive_api_url),
            truth_delay_days=settings.controlled_learning_truth_delay_days,
            limit=truth_limit,
        )
    generated_candidates = generate_improvement_candidates(
        store,
        replay_results,
        min_provider_samples=settings.controlled_learning_min_provider_samples,
    )
    provider_summaries = store.provider_summary()
    for item in provider_summaries:
        item["evidence_status"] = (
            "充足"
            if int(item.get("sample_count") or 0) >= settings.controlled_learning_min_provider_samples
            else "不足"
        )
    report = {
        "version": LEARNING_VERSION,
        "generated_at": iso_now(),
        "mode": "offline_review_gated",
        "safety": {
            "feishu_send": False,
            "runtime_mutation": False,
            "automatic_deploy": False,
            "candidate_auto_apply": False,
        },
        "replay": {
            "run_id": run_id,
            "total": len(replay_results),
            "passed": len(replay_results) - len(failed),
            "failed": len(failed),
            "failed_cases": [item.model_dump(mode="json") for item in failed],
        },
        "verification": verification,
        "reference_data": {
            "source": "Open-Meteo Historical Weather API",
            "kind": "historical_grid_or_reanalysis_reference",
            "station_observation": False,
        },
        "snapshots": store.snapshot_summary(),
        "signals": store.signal_summary(),
        "provider_summaries": provider_summaries,
        "generated_candidates": generated_candidates,
        "candidates": store.list_candidates(),
    }
    report["report_files"] = write_learning_report(
        report, settings.controlled_learning_report_dir
    )
    return report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if not settings.controlled_learning_enabled:
        _print_json({"status": "disabled", "reason": "CONTROLLED_LEARNING_ENABLED=false"})
        return 0
    store = ControlledLearningStore(settings.controlled_learning_db)

    if args.command == "run":
        report = asyncio.run(
            run_cycle(
                settings,
                skip_truth=bool(args.skip_truth),
                truth_limit=max(1, int(args.truth_limit)),
            )
        )
        _print_json(report)
        return 2 if args.strict and report["replay"]["failed"] else 0

    if args.command == "add-case":
        path = Path(args.file)
        case = ReplayCase.model_validate_json(path.read_text(encoding="utf-8"))
        case = case.model_copy(update={"source": "admin"})
        store.upsert_replay_case(case)
        _print_json({"status": "saved", "case": case.model_dump(mode="json")})
        return 0

    if args.command == "list-cases":
        _print_json(
            {
                "status": "ok",
                "cases": [
                    case.model_dump(mode="json")
                    for case in store.list_replay_cases(enabled_only=not args.all)
                ],
            }
        )
        return 0

    if args.command == "candidates":
        _print_json({"status": "ok", "candidates": store.list_candidates(args.status)})
        return 0

    if args.command == "decide":
        candidate = store.decide_candidate(
            args.candidate_id,
            args.status,
            actor=args.actor,
            reason=args.reason,
        )
        _print_json(
            {
                "status": "recorded",
                "candidate": candidate,
                "runtime_effect": "none",
            }
        )
        return 0

    if args.command == "audit":
        _print_json({"status": "ok", "audit": store.candidate_audit(args.candidate_id)})
        return 0
    return 1


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
