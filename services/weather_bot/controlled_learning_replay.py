"""Deterministic replay probes for intent, entities, context, and group gates."""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
from typing import Any

from services.weather_bot import memory as weather_memory
from services.weather_bot.controlled_learning import (
    ControlledLearningStore,
    ReplayActual,
    ReplayCase,
    ReplayCaseResult,
    ReplayExpectation,
    ReplayStateSeed,
)
from services.weather_bot.feishu import FeishuBotAccount
from services.weather_bot.main import (
    FEISHU_TASK_BOT,
    WEATHER_FORECAST_BOT_ROLE,
    WEATHER_TASK_BOT_ROLE,
    _comparison_regions_from_text,
    _contextual_weather_text,
    _conversation_state_key,
    _days_from_text,
    _explicit_region_from_text,
    _is_addressed_to_bot,
    _is_group_chat,
    _is_help_command,
    _is_power_briefing_command,
    _is_supported_group_message_type,
    _is_task_command,
    _is_task_submission_command,
    _is_weather_command,
    _is_weather_knowledge_question,
    _normalize_event_text,
    _target_date_from_text,
)
from services.weather_bot.location import BUILTIN_LOCATIONS
from services.weather_bot.weather_metrics import (
    unsupported_weather_metric_labels,
    weather_metrics_from_text,
)


_REPLAY_WEATHER_BOT_OPEN_ID = "ou_replay_weather_bot"
_REPLAY_TASK_BOT_OPEN_ID = "ou_replay_task_bot"


def generate_replay_cases(today: date | None = None) -> list[ReplayCase]:
    current = today or date.today()
    cases: list[ReplayCase] = [
        _case("intent-help", "intent", "云云能做什么", intent="help"),
        _case(
            "intent-briefing",
            "intent",
            "请生成今天的电力气象决策晨报 2.0",
            intent="power_briefing",
        ),
        _case(
            "intent-task",
            "task_routing",
            "发布辽宁未来3天气象任务",
            intent="task",
            region="辽宁省",
            days=3,
        ),
        _case(
            "intent-task-query",
            "task_routing",
            "查询刚才的气象任务",
            intent="task",
        ),
        _case("intent-general", "intent", "给我讲个笑话", intent="general"),
        _case(
            "metric-unsupported-humidity",
            "metric",
            "辽宁湿度",
            intent="unsupported_metric",
            region="辽宁省",
            unsupported_metrics=["湿度"],
        ),
        _case(
            "metric-rain",
            "metric",
            "广州未来3天只看降雨",
            intent="weather",
            region="广东省广州市",
            days=3,
            metrics=["rain"],
        ),
        _case(
            "location-province-scope",
            "location",
            "辽宁整个地区未来7天天气情况",
            intent="weather",
            region="辽宁省",
            days=7,
        ),
        _case(
            "location-province-all",
            "location",
            "辽宁全省未来7天",
            intent="weather",
            region="辽宁省",
            days=7,
        ),
        _case(
            "location-district",
            "location",
            "上海浦东新区天气",
            intent="weather",
            region="上海浦东新区",
            days=1,
        ),
        _case(
            "location-prefecture",
            "location",
            "西藏阿里地区天气",
            intent="weather",
            region="西藏阿里地区",
            days=1,
        ),
        _case(
            "location-city-after-province",
            "location",
            "辽宁盘锦未来3天",
            intent="weather",
            region="辽宁盘锦",
            days=3,
        ),
        _case(
            "location-comparison",
            "location",
            "广东、深圳、上海未来3天对比",
            intent="weather_comparison",
            regions=["广东省", "广东省深圳市", "上海市"],
            days=3,
        ),
        _case(
            "date-tomorrow",
            "date",
            "广州明天天气",
            intent="weather",
            region="广东省广州市",
            days=1,
            target_date_offset=1,
        ),
        _case(
            "date-next-seven",
            "date",
            "广州未来7天天气",
            intent="weather",
            region="广东省广州市",
            days=7,
            target_date_offset=0,
        ),
        _case(
            "date-fragment-not-location",
            "date",
            "7月下旬各地区天气情况",
            intent="weather",
            region_absent=True,
        ),
        _group_case(
            "group-unmentioned",
            "辽宁未来3天天气",
            addressed=False,
            should_reply=False,
            intent="ignored",
        ),
        _group_case(
            "group-fake-at",
            "@云云 辽宁未来3天天气",
            addressed=False,
            should_reply=False,
            intent="ignored",
        ),
        _group_case(
            "group-real-mention",
            "@云云 辽宁未来3天天气",
            addressed=True,
            should_reply=True,
            intent="weather",
            region="辽宁省",
            days=3,
        ),
        ReplayCase(
            case_id="group-nontext",
            category="group_gate",
            text="@云云 看这个卡片",
            chat_type="group",
            addressed=True,
            message_type="interactive",
            expectation=ReplayExpectation(should_reply=False, intent="ignored"),
        ),
    ]

    seed_guangzhou = ReplayStateSeed(
        last_successful_request={
            "region": "广东省广州市",
            "target_date": current.isoformat(),
            "days": 3,
            "metrics": [],
        }
    )
    cases.extend(
        [
            _case(
                "context-tomorrow",
                "context",
                "那明天呢",
                intent="weather",
                region="广东省广州市",
                days=1,
                target_date_offset=1,
                state_seeds=[seed_guangzhou],
            ),
            _case(
                "context-change-city",
                "context",
                "换成盘锦",
                intent="weather",
                region="辽宁盘锦",
                days=3,
                state_seeds=[seed_guangzhou],
            ),
            _case(
                "context-metric",
                "context",
                "只看降雨",
                intent="weather",
                region="广东省广州市",
                days=3,
                metrics=["rain"],
                state_seeds=[seed_guangzhou],
            ),
            _case(
                "context-window",
                "context",
                "改成未来7天",
                intent="weather",
                region="广东省广州市",
                days=7,
                state_seeds=[seed_guangzhou],
            ),
            _case(
                "context-reset",
                "context",
                "重新查，不要沿用刚才的",
                intent="context_reset",
                state_seeds=[seed_guangzhou],
            ),
            _case(
                "context-user-isolation",
                "context",
                "明天呢",
                intent="weather",
                region="广东省广州市",
                days=1,
                target_date_offset=1,
                state_seeds=[
                    seed_guangzhou,
                    ReplayStateSeed(
                        user_id="user-b",
                        last_successful_request={
                            "region": "上海市",
                            "target_date": current.isoformat(),
                            "days": 1,
                            "metrics": [],
                        },
                    ),
                ],
            ),
            _case(
                "context-chat-isolation",
                "context",
                "明天呢",
                intent="weather",
                region="广东省广州市",
                days=1,
                target_date_offset=1,
                state_seeds=[
                    seed_guangzhou,
                    ReplayStateSeed(
                        chat_id="chat-b",
                        last_successful_request={
                            "region": "北京市",
                            "target_date": current.isoformat(),
                            "days": 1,
                            "metrics": [],
                        },
                    ),
                ],
            ),
            _case(
                "context-thread-isolation",
                "context",
                "明天呢",
                intent="weather",
                region="广东省广州市",
                days=1,
                target_date_offset=1,
                thread_id="thread-a",
                state_seeds=[
                    ReplayStateSeed(
                        thread_id="thread-a",
                        last_successful_request={
                            "region": "广东省广州市",
                            "target_date": current.isoformat(),
                            "days": 1,
                            "metrics": [],
                        },
                    ),
                    ReplayStateSeed(
                        thread_id="thread-b",
                        last_successful_request={
                            "region": "北京市",
                            "target_date": current.isoformat(),
                            "days": 1,
                            "metrics": [],
                        },
                    ),
                ],
            ),
            _case(
                "context-bot-isolation",
                "context",
                "明天呢",
                intent="general",
                state_seeds=[
                    ReplayStateSeed(
                        bot_role="weather_task_bot",
                        last_successful_request={
                            "region": "北京市",
                            "target_date": current.isoformat(),
                            "days": 1,
                            "metrics": [],
                        },
                    )
                ],
            ),
        ]
    )
    return cases


def run_deterministic_replay(
    store: ControlledLearningStore,
    *,
    today: date | None = None,
) -> tuple[str, list[ReplayCaseResult]]:
    generated = generate_replay_cases(today=today)
    custom = store.list_replay_cases(enabled_only=True)
    merged = {case.case_id: case for case in generated}
    merged.update({case.case_id: case for case in custom})
    original_db_path = weather_memory.DB_PATH
    results: list[ReplayCaseResult] = []
    try:
        with tempfile.TemporaryDirectory(prefix="weather-learning-replay-") as temp_dir:
            for index, case in enumerate(merged.values()):
                weather_memory.DB_PATH = str(Path(temp_dir) / f"case-{index}.db")
                results.append(run_replay_case(case, today=today))
    finally:
        weather_memory.DB_PATH = original_db_path
    run_id = store.record_replay_run(results)
    return run_id, results


def run_replay_case(case: ReplayCase, *, today: date | None = None) -> ReplayCaseResult:
    event = _event_for_case(case)
    account = _replay_bot_account(case.bot_scope)
    for seed in case.state_seeds:
        seed_event = _event_for_seed(seed)
        key = _conversation_state_key(seed_event, seed.bot_role)
        if key:
            weather_memory.save_conversation_state(
                key,
                {
                    "state_version": 1,
                    "last_successful_request": seed.last_successful_request,
                },
            )

    if _is_group_chat(event) and not _is_supported_group_message_type(event):
        actual = ReplayActual(should_reply=False, intent="ignored")
        return _compare(case, actual, today=today)
    if _is_group_chat(event) and not _is_addressed_to_bot(
        case.text,
        event,
        case.bot_scope,
        account,
    ):
        actual = ReplayActual(should_reply=False, intent="ignored")
        return _compare(case, actual, today=today)

    normalized = _normalize_event_text(case.text, event, case.bot_scope)
    context_bot_role = (
        WEATHER_TASK_BOT_ROLE
        if case.bot_scope == FEISHU_TASK_BOT
        else WEATHER_FORECAST_BOT_ROLE
    )
    contextual, action = _contextual_weather_text(
        normalized,
        event,
        bot_role=context_bot_role,
    )
    if action == "reset":
        actual = ReplayActual(
            should_reply=True,
            intent="context_reset",
            normalized_text=normalized,
            contextual_text=contextual,
        )
        return _compare(case, actual, today=today)

    intent = _diagnostic_intent(contextual)
    if (
        _is_group_chat(event)
        and intent not in {"help", "power_briefing"}
        and not (
            _is_task_submission_command(contextual)
            or _is_task_command(contextual)
            or _is_weather_command(contextual)
            or _is_weather_knowledge_question(contextual)
        )
    ):
        actual = ReplayActual(
            should_reply=False,
            intent="ignored",
            normalized_text=normalized,
            contextual_text=contextual,
        )
        return _compare(case, actual, today=today)
    regions = _comparison_regions_from_text(contextual)
    explicit_region = _explicit_region_from_text(contextual)
    metrics = weather_metrics_from_text(contextual) or []
    unsupported = unsupported_weather_metric_labels(contextual)
    actual = ReplayActual(
        should_reply=True,
        intent=intent,
        normalized_text=normalized,
        contextual_text=contextual,
        region=_normalized_region_entity(explicit_region),
        regions=regions,
        days=_days_from_text(contextual),
        metrics=metrics,
        unsupported_metrics=unsupported,
        target_date=_target_date_from_text(contextual) if intent in {"weather", "weather_comparison"} else None,
    )
    return _compare(case, actual, today=today)


def _diagnostic_intent(text: str) -> str:
    if _is_help_command(text):
        return "help"
    if _is_power_briefing_command(text):
        return "power_briefing"
    if _is_task_submission_command(text):
        return "weather_task_submission"
    if _is_task_command(text):
        return "task"
    if _is_weather_command(text):
        supported = weather_metrics_from_text(text)
        unsupported = unsupported_weather_metric_labels(text)
        if unsupported and not supported:
            return "unsupported_metric"
        if len(_comparison_regions_from_text(text)) >= 2:
            return "weather_comparison"
        return "weather"
    return "general"


def _compare(case: ReplayCase, actual: ReplayActual, *, today: date | None) -> ReplayCaseResult:
    expected = case.expectation
    mismatches: list[str] = []
    for field in ("should_reply", "intent", "region", "days"):
        expected_value = getattr(expected, field)
        if field in {"region", "days"} and expected_value is None:
            continue
        if getattr(actual, field) != expected_value:
            mismatches.append(field)
    for field in ("regions", "metrics", "unsupported_metrics"):
        expected_value = getattr(expected, field)
        if expected_value is None:
            continue
        if getattr(actual, field) != expected_value:
            mismatches.append(field)
    if expected.target_date_offset is not None:
        base = today or date.today()
        expected_date = (base + timedelta(days=expected.target_date_offset)).isoformat()
        if actual.target_date != expected_date:
            mismatches.append("target_date")
    if expected.region_absent and actual.region is not None:
        mismatches.append("region_absent")
    return ReplayCaseResult(
        case_id=case.case_id,
        category=case.category,
        passed=not mismatches,
        mismatches=mismatches,
        expected=expected,
        actual=actual,
    )


def _case(
    case_id: str,
    category: str,
    text: str,
    *,
    intent: str,
    region: str | None = None,
    region_absent: bool = False,
    regions: list[str] | None = None,
    days: int | None = None,
    metrics: list[str] | None = None,
    unsupported_metrics: list[str] | None = None,
    target_date_offset: int | None = None,
    state_seeds: list[ReplayStateSeed] | None = None,
    tags: list[str] | None = None,
    thread_id: str | None = None,
) -> ReplayCase:
    return ReplayCase(
        case_id=case_id,
        category=category,
        text=text,
        state_seeds=state_seeds or [],
        tags=tags or [],
        thread_id=thread_id,
        expectation=ReplayExpectation(
            intent=intent,
            region=region,
            region_absent=region_absent,
            regions=regions,
            days=days,
            metrics=metrics,
            unsupported_metrics=unsupported_metrics,
            target_date_offset=target_date_offset,
        ),
    )


def _group_case(
    case_id: str,
    text: str,
    *,
    addressed: bool,
    should_reply: bool,
    intent: str,
    region: str | None = None,
    days: int | None = None,
) -> ReplayCase:
    return ReplayCase(
        case_id=case_id,
        category="group_gate",
        text=text,
        chat_type="group",
        addressed=addressed,
        expectation=ReplayExpectation(
            should_reply=should_reply,
            intent=intent,
            region=region,
            days=days,
        ),
    )


def _event_for_case(case: ReplayCase) -> dict[str, Any]:
    alias = "点点" if case.bot_scope == FEISHU_TASK_BOT else "云云"
    bot_open_id = (
        _REPLAY_TASK_BOT_OPEN_ID
        if case.bot_scope == FEISHU_TASK_BOT
        else _REPLAY_WEATHER_BOT_OPEN_ID
    )
    mentions = (
        [{"key": "@_user_1", "name": alias, "id": {"open_id": bot_open_id}}]
        if case.addressed and case.chat_type == "group"
        else []
    )
    message: dict[str, Any] = {
        "message_id": f"replay-{case.case_id}",
        "chat_id": case.chat_id,
        "chat_type": case.chat_type,
        "message_type": case.message_type,
        "content": json.dumps({"text": case.text}, ensure_ascii=False),
        "mentions": mentions,
    }
    if case.thread_id:
        message["thread_id"] = case.thread_id
    return {
        "sender": {"sender_id": {"open_id": case.user_id}},
        "message": message,
    }


def _replay_bot_account(bot_scope: str) -> FeishuBotAccount:
    bot_open_id = (
        _REPLAY_TASK_BOT_OPEN_ID
        if bot_scope == FEISHU_TASK_BOT
        else _REPLAY_WEATHER_BOT_OPEN_ID
    )
    return FeishuBotAccount(bot_open_id=bot_open_id)


def _event_for_seed(seed: ReplayStateSeed) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": f"seed-{seed.chat_id}-{seed.user_id}",
        "chat_id": seed.chat_id,
        "chat_type": seed.chat_type,
        "message_type": "text",
        "content": '{"text":"seed"}',
    }
    if seed.thread_id:
        message["thread_id"] = seed.thread_id
    return {
        "sender": {"sender_id": {"open_id": seed.user_id}},
        "message": message,
    }


def _normalized_region_entity(region: str | None) -> str | None:
    if not region:
        return None
    location = BUILTIN_LOCATIONS.get(region)
    return location.name if location else region
