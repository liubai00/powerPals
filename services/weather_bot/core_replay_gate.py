"""Auditable, fail-closed coverage gate for the 96 documented core cases.

The manifest mirrors section 13 of the upgrade plan one-for-one.  A case is
never counted as passed merely because it is listed: only an attached offline
executor may produce a ``passed`` outcome.  Missing product capability is
``not_implemented``; missing deterministic evidence is ``blocked``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import gc
from pathlib import Path
import tempfile
from typing import Any, Literal
from unittest import mock


ManifestStatus = Literal["implemented", "not_implemented", "blocked"]
OutcomeStatus = Literal["passed", "failed", "not_implemented", "blocked"]


@dataclass(frozen=True)
class CoreReplayItem:
    case_number: int
    section: str
    input_text: str
    expected: str
    status: ManifestStatus
    executor_id: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_DOCUMENT_CASES: tuple[tuple[str, str], ...] = (
    ("G0：山东明天天气", "静默，0 次天气调用"),
    ("Gfake：@云云 山东明天天气", "静默"),
    ("Gm：山东明天天气", "查询山东，日期 08-10"),
    ("Gr：为什么风险升高", "引用被回复报告解释"),
    ("回复普通成员：为什么", "静默"),
    ("mention 点点但进入云云路由", "静默"),
    ("Gm 图片消息", "静默，0 次天气调用"),
    ("发送者为机器人", "静默"),
    ("相同 event_id 重投", "只处理一次"),
    ("Gm：讲个笑话", "群内静默"),
    ("P：讲个笑话", "简短能力边界"),
    ("Gm：云云能做什么", "帮助，不查天气"),
    ("辽宁全省未来7天", "辽宁省级范围，7 天"),
    ("辽宁盘锦未来3天", "辽宁盘锦，不能截成辽宁"),
    ("西藏阿里地区天气", "正确解析阿里地区"),
    ("上海浦东新区天气", "正确解析浦东新区"),
    ("华东明天有哪些高温市场", "解析华东市场集合"),
    ("蒙西明日晚峰风险", "market_id=蒙西"),
    ("蒙东明日晚峰风险", "与蒙西隔离"),
    ("全国明日 Top 5", "使用默认分析区全集"),
    ("山东、河南、河北晚峰对比", "三市场横向比较"),
    ("各地区明天怎么样", "澄清口径，不调用天气"),
    ("火星市明天天气", "地点失败，澄清"),
    ("朝阳明天天气", "北京朝阳/辽宁朝阳澄清"),
    ("鲁明日晚峰", "映射山东并保留原文依据"),
    ("山东明日晚峰", "使用山东配置窗口"),
    ("浙江明天午间光伏", "使用午间光伏窗口"),
    ("今天谷段怎么样，谷段已过", "澄清历史或下一谷段"),
    ("广东日内剩余时段", "当前时刻至当日结束"),
    ("未来6小时强对流", "08:00–14:00"),
    ("山东未来3天负荷压力", "逐日趋势"),
    ("山东未来7天", "不误判为任务"),
    ("华东8–15天趋势", "概率化低确定性措辞"),
    ("下周三山东晚峰", "解析 2026-08-12"),
    ("明天17点到21点山东", "显式窗口覆盖默认"),
    ("山东明天全天", "不继承旧晚峰"),
    ("山东明天25:00", "非法时间，澄清"),
    ("7月下旬各地区天气", "日期词不得当地点"),
    ("山东晚峰负荷压力", "明确标‘代理’"),
    ("浙江光伏比今天好还是差", "输出光资源代理"),
    ("甘肃风资源怎么样", "10 米风代理及边界"),
    ("甘肃轮毂高度风速，无模型", "明确不支持"),
    ("内蒙古风电爬坡风险", "连续风险窗口"),
    ("四川降雨让水电增加多少", "不输出水电增量"),
    ("广东强对流电网风险", "不声称故障必然发生"),
    ("哪个省新能源预测偏差最大", "输出气象代理排行"),
    ("和昨天8点预报相比变了什么", "同有效时刻版本比较"),
    ("两个数据源分歧在哪里", "指明变量和时段"),
    ("为什么置信度中等", "解释覆盖、分歧、时效"),
    ("山东有官方高温预警吗", "只显示官方源和发布时间"),
    ("山东日前价格会涨吗", "拒绝方向判断"),
    ("应该买多少兆瓦时", "拒绝仓位建议"),
    ("浙江实际光伏出力多少", "缺数据，不以代理替代"),
    ("山东当前实际负荷多少", "缺数据，不编造"),
    ("广州未来3天 → 那明天呢", "继承广州，改为单日"),
    ("广州明天降雨 → 换成盘锦", "继承日期和指标"),
    ("广州未来3天 → 只看降雨", "继承地点和天数"),
    ("广州明天 → 改成未来7天", "地点广州，7 天"),
    ("广州明天 → 不是广州，是深圳", "深圳覆盖广州"),
    ("广州明天 → 不要沿用刚才的", "清空并澄清新需求"),
    ("地点澄清中 → 云云能做什么", "转帮助并清除旧澄清"),
    ("接口失败 → 重试", "使用短期重试，不覆盖成功上下文"),
    ("同群甲广州、乙上海、甲问明天", "甲继承广州"),
    ("同用户群A广州、群B北京、群A追问", "群A继承广州"),
    ("同用户线程A广州、线程B北京", "线程隔离"),
    ("云云广州，点点收到‘明天呢’", "点点不读取云云状态"),
    ("重启后状态仍在 TTL", "从 SQLite 恢复"),
    ("重启后状态已过 TTL", "不继承"),
    ("生成今日晨报3.0", "默认全集，不要求地区"),
    ("回复晨报：查看全部市场", "读取同一发布快照"),
    ("无报告上下文：查看全部市场", "提示先生成，不重新抓取"),
    ("稳定分析区数量 0", "整段不展示"),
    ("其余分析区 26", "自然语言说明"),
    ("P：每天8:30看三省", "只生成 DRAFT"),
    ("另一线程确认订阅", "不激活原草稿"),
    ("群普通成员要求启用", "可建草稿，不激活"),
    ("群管理员二次确认", "ACTIVE，不补发历史预警"),
    ("阈值 38℃改39℃", "新版本生效、旧版本留审计"),
    ("连续两次取消", "幂等"),
    ("同规则、窗口、等级连续命中", "outbox 仅 1 条"),
    ("冷却期风险未升级", "不重复发送"),
    ("连续满足恢复条件", "仅 1 条恢复通知"),
    ("ALERT_SEND_ENABLED=false/DRY_RUN", "可评估，0 次飞书调用"),
    ("全部数据源失败", "不造数据、不预警"),
    ("未鉴权 POST /api/weather/publish", "401/403，0 次发送"),
    ("管理鉴权有效但全局发送关闭", "只生成，0 次发送"),
    ("mention 同名普通用户，open_id 不同", "静默"),
    ("数据时间字段", "抓取、起报、有效、聚合、业务截止分开"),
    ("缺失部分小时", "降级或退出排行"),
    ("Top 5 同等级风险", "按强度、变化、持续性和置信度排序"),
    ("天气 API 全部失败，仅搜索到无原始链接的摘要", "返回数据不可用，不进入计算"),
    ("搜索结果可定位官方原文但没有结构化数值", "只提供带来源的文字线索，不生成数值代理"),
    ("只有过期缓存", "明确标记缓存时间和过期状态，不称为最新数据"),
    ("外部响应缺少单位、有效时间或来源", "质量门禁拒绝进入聚合"),
    ("询问实际负荷、出力或价格且无外部接口", "明确无可靠数据，不调用 LLM 补造"),
    ("许可不允许长期存储原始响应", "仅保存允许的最小元数据和派生结果，并按期清理"),
)


_EXECUTOR_BY_CASE: dict[int, str] = {
    **{
        number: "public_feishu_event"
        for number in (4, 5, 11, 23, 24, 42, 44, 45, 50, 61, 62, 69, 70, 71)
    },
    **{
        number: f"document_replay:{number}"
        for number in (
            1,
            2,
            3,
            7,
            10,
            12,
            14,
            15,
            16,
            32,
            55,
            56,
            57,
            58,
            59,
            60,
            63,
            64,
            65,
            66,
        )
    },
    6: "bot_identity:wrong_bot",
    8: "event_gate:automated_sender",
    9: "event_ledger:duplicate",
    67: "memory:restart_within_ttl",
    68: "memory:expired_state",
    85: "public_admin_api:missing_token",
    86: "public_admin_api:global_disabled",
    87: "bot_identity:same_name_wrong_open_id",
    88: "forecast_time:separate_fields",
    95: "boundary:no_external_power_data",
    52: "decision_boundary:position_request",
    **{number: f"briefing_card:{number}" for number in (39, 40, 41, 72, 73)},
    **{number: f"weather_risk_evidence:{number}" for number in (43, 46, 47, 48, 49)},
    **{number: f"subscription_coordinator:{number}" for number in range(74, 80)},
    **{number: f"alert_engine:{number}" for number in range(80, 85)},
    90: "briefing_risk_order:same_severity",
    **{number: f"data_availability_gate:{number}" for number in (89, 91, 92, 93, 94)},
    96: "source_retention_policy:derived_only",
    **{
        number: f"electricity_entities:{number}"
        for number in (
            13,
            17,
            18,
            19,
            20,
            21,
            22,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            33,
            34,
            35,
            36,
            37,
            38,
            51,
            53,
            54,
        )
    },
}

# These capabilities are absent from the current product, rather than merely
# missing a replay adapter.  Keeping them explicit prevents a strict gate from
# silently treating future-work placeholders as successes.
_NOT_IMPLEMENTED_CASES: frozenset[int] = frozenset()


def _section_for(case_number: int) -> str:
    if case_number <= 12:
        return "13.1_group_gate_and_routing"
    if case_number <= 38:
        return "13.2_location_market_time"
    if case_number <= 54:
        return "13.3_power_weather_boundary"
    if case_number <= 68:
        return "13.4_context_isolation"
    if case_number <= 84:
        return "13.5_briefing_subscription_send"
    if case_number <= 90:
        return "13.6_p0_regression"
    return "13.7_external_data_availability"


def core_replay_manifest() -> list[CoreReplayItem]:
    """Return the immutable one-to-one document matrix in numeric order."""
    items: list[CoreReplayItem] = []
    for number, (input_text, expected) in enumerate(_DOCUMENT_CASES, start=1):
        executor_id = _EXECUTOR_BY_CASE.get(number)
        if executor_id:
            status: ManifestStatus = "implemented"
            reason = "offline_executor_available"
        elif number in _NOT_IMPLEMENTED_CASES:
            status = "not_implemented"
            reason = "documented_capability_not_implemented"
        else:
            status = "blocked"
            reason = "product_behavior_exists_or_is_uncertain_but_no_faithful_offline_executor"
        items.append(
            CoreReplayItem(
                case_number=number,
                section=_section_for(number),
                input_text=input_text,
                expected=expected,
                status=status,
                executor_id=executor_id,
                reason=reason,
            )
        )
    return items


def run_core_replay_gate(*, today: date | None = None) -> dict[str, Any]:
    """Execute implemented cases and fail closed on every unresolved item."""
    fixed_today = today or date(2026, 8, 9)
    outcomes: list[dict[str, Any]] = []
    for item in core_replay_manifest():
        base = item.as_dict()
        if item.status != "implemented":
            outcomes.append({**base, "outcome": item.status, "evidence": {}})
            continue
        try:
            passed, evidence = _execute(item, today=fixed_today)
            outcomes.append(
                {
                    **base,
                    "outcome": "passed" if passed else "failed",
                    "evidence": evidence,
                }
            )
        except Exception as exc:  # noqa: BLE001 - gate errors must fail closed
            outcomes.append(
                {
                    **base,
                    "outcome": "failed",
                    "evidence": {"error_type": type(exc).__name__},
                }
            )

    counts = {
        status: sum(1 for item in outcomes if item["outcome"] == status)
        for status in ("passed", "failed", "not_implemented", "blocked")
    }
    return {
        "matrix_version": "weather_power_core_96_v1",
        "fixed_clock": f"{fixed_today.isoformat()}T08:00:00+08:00",
        "total": len(outcomes),
        **counts,
        "gate_passed": counts["passed"] == 96,
        "safety": {
            "external_calls": 0,
            "feishu_sends": 0,
            "runtime_mutations": 0,
        },
        "unresolved_case_numbers": [
            item["case_number"] for item in outcomes if item["outcome"] != "passed"
        ],
        "failed_cases": [item for item in outcomes if item["outcome"] == "failed"],
        "not_implemented_cases": [
            item for item in outcomes if item["outcome"] == "not_implemented"
        ],
        "blocked_cases": [item for item in outcomes if item["outcome"] == "blocked"],
        "items": outcomes,
    }


def _execute(item: CoreReplayItem, *, today: date) -> tuple[bool, dict[str, Any]]:
    executor_id = item.executor_id or ""
    if executor_id == "public_feishu_event":
        return _execute_public_feishu_event(item, today=today)
    if executor_id.startswith("document_replay:"):
        from services.weather_bot import memory as weather_memory
        from services.weather_bot.controlled_learning_replay import run_replay_case

        case = _document_replay_case(item.case_number, today=today)
        original_db_path = weather_memory.DB_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="weather-core-replay-") as temp_dir:
                weather_memory.DB_PATH = str(Path(temp_dir) / "memory.db")
                result = run_replay_case(case, today=today)
        finally:
            weather_memory.DB_PATH = original_db_path
        return result.passed, {
            "executor": executor_id,
            "mismatches": result.mismatches,
            "actual": result.actual.model_dump(mode="json"),
            "external_calls": 0,
            "feishu_sends": 0,
        }
    if executor_id.startswith("electricity_entities:"):
        return _execute_electricity_entities(item, today=today)
    if executor_id.startswith("bot_identity:"):
        from services.weather_bot.bot_identity import mentions_expected_bot

        mention = {
            "key": "@_user_1",
            "id": {"open_id": "ou_wrong_or_task_bot"},
            "name": "云云" if item.case_number == 87 else "点点",
        }
        passed = not mentions_expected_bot(
            [mention],
            expected_open_id="ou_weather_bot",
            aliases=("云云",),
            allow_name_fallback=False,
        )
        return passed, {"executor": executor_id, "accepted": not passed}
    if executor_id == "event_gate:automated_sender":
        from services.weather_bot.main import _is_automated_sender

        passed = _is_automated_sender({"sender": {"sender_type": "bot"}})
        return passed, {"executor": executor_id, "classified_automated": passed}
    if executor_id == "event_ledger:duplicate":
        return _execute_event_deduplication(executor_id)
    if executor_id == "memory:restart_within_ttl":
        return _execute_memory_restart(executor_id, expired=False)
    if executor_id == "memory:expired_state":
        return _execute_memory_restart(executor_id, expired=True)
    if executor_id.startswith("public_admin_api:"):
        return _execute_public_admin_api(item)
    if executor_id == "forecast_time:separate_fields":
        from services.weather_bot.models import TimeInfo

        required = {
            "retrieved_at",
            "provider_issued_at",
            "aggregation_completed_at",
            "valid_time",
            "business_submission_deadline",
        }
        passed = required.issubset(TimeInfo.model_fields)
        return passed, {"executor": executor_id, "fields": sorted(required)}
    if executor_id == "boundary:no_external_power_data":
        from services.weather_bot.decision_boundary import weather_only_boundary_answer

        prompts = (
            "山东当前实际负荷多少",
            "浙江实际光伏出力多少",
            "山东日前价格会涨吗",
        )
        answers = [weather_only_boundary_answer(prompt) for prompt in prompts]
        passed = all(answer and "不能" in answer or answer and "没有" in answer for answer in answers)
        return passed, {
            "executor": executor_id,
            "deterministic_pre_llm_blocks": sum(bool(answer) for answer in answers),
            "llm_calls": 0,
        }
    if executor_id == "decision_boundary:position_request":
        from services.weather_bot.decision_boundary import weather_only_boundary_answer

        answer = weather_only_boundary_answer(item.input_text)
        passed = bool(answer and "不能" in answer and "兆瓦时" in answer and "仓位" in answer)
        return passed, {
            "executor": "decision_boundary",
            "blocked_before_llm": bool(answer),
            "llm_calls": 0,
        }
    if executor_id.startswith("briefing_card:"):
        return _execute_briefing_card(item, today=today)
    if executor_id.startswith("weather_risk_evidence:"):
        return _execute_weather_risk_evidence(item, today=today)
    if executor_id.startswith("subscription_coordinator:"):
        return _execute_subscription_coordinator(item, today=today)
    if executor_id.startswith("alert_engine:"):
        return _execute_alert_engine(item, today=today)
    if executor_id == "briefing_risk_order:same_severity":
        return _execute_briefing_risk_order(item, today=today)
    if executor_id.startswith("data_availability_gate:"):
        return _execute_data_availability_gate(item, today=today)
    if executor_id == "source_retention_policy:derived_only":
        return _execute_source_retention_policy(item, today=today)
    raise KeyError(f"Unknown core replay executor: {executor_id}")


def _execute_public_feishu_event(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from fastapi.testclient import TestClient

    from services.weather_bot import memory as weather_memory
    from services.weather_bot.config import Settings
    from services.weather_bot.feishu import FeishuClient
    from services.weather_bot.llm import LlmClient
    from services.weather_bot.location import AmbiguousLocationError, LocationNotFoundError
    from services.weather_bot.main import create_app
    from services.weather_bot.models import (
        AggregatedForecast,
        ForecastPoint,
        ForecastSummary,
        ForecastWindow,
        ProviderForecast,
        TimeInfo,
        WeatherSubmission,
    )
    from services.weather_bot.search import TavilySearchClient
    from services.weather_bot.service import ForecastService
    from services.weather_bot.official_warnings import OfficialWarning, OfficialWarningFetchResult
    from services.weather_bot.typhoon import TyphoonClient

    class OfflineForecastService:
        def __init__(self) -> None:
            self.requests: list[Any] = []
            self.failures_remaining = 0

        async def forecast(self, request: Any) -> WeatherSubmission:
            self.requests.append(request)
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise RuntimeError("offline provider failure")
            points = [
                ForecastPoint(
                    time=f"{request.target_date}T12:00:00+08:00",
                    temperature=25.0,
                    precipitation_probability=10.0,
                    wind_speed=2.0,
                    cloud_cover=20.0,
                )
            ]
            retrieved_at = f"{request.target_date}T08:00:00+08:00"
            issued_at = f"{request.target_date}T07:00:00+08:00"
            return WeatherSubmission(
                task_id=f"offline-{len(self.requests)}",
                region=request.region,
                target_date=request.target_date,
                data_cutoff_time=f"{request.target_date}T16:00:00+08:00",
                time_info=TimeInfo(
                    retrieved_at=retrieved_at,
                    provider_issued_at={"offline_test": issued_at},
                    aggregation_completed_at=f"{request.target_date}T08:00:01+08:00",
                    valid_time=ForecastWindow(
                        start=f"{request.target_date}T00:00:00+08:00",
                        end=f"{request.target_date}T23:00:00+08:00",
                    ),
                    forecast_run_id=f"offline-run-{len(self.requests)}",
                ),
                provider_results=[
                    ProviderForecast(
                        provider="offline_test",
                        points=points,
                        retrieved_at=retrieved_at,
                        provider_issued_at=issued_at,
                        source_url="https://example.test/weather",
                        content_sha256="a" * 64,
                    )
                ],
                aggregated_forecast=AggregatedForecast(
                    providers_used=["offline_test"],
                    points=points,
                    summary=ForecastSummary(
                        max_temperature=28.0,
                        min_temperature=20.0,
                        rain_probability=10.0,
                        wind_speed=2.0,
                        cloud_cover=20.0,
                        main_weather="晴",
                        high_risk_period="无",
                    ),
                ),
                confidence={"score": 0.8, "description": "offline"},
                key_factors=["offline"],
                risk_notes=[],
            )

    class OfflineLocationResolver:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure
            self.calls = 0

        async def resolve(self, request: Any) -> Any:
            self.calls += 1
            raise self.failure

    class ForbiddenWeatherProvider:
        name = "open_meteo"

        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, request: Any) -> Any:
            self.calls += 1
            raise AssertionError("location clarification must happen before weather provider calls")

    def event(
        text: str,
        *,
        message_id: str,
        chat_type: str = "p2p",
        chat_id: str = "core-chat",
        sender_id: str = "core-user-a",
        thread_id: str | None = None,
        root_id: str | None = None,
        addressed: bool = False,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "message_type": "text",
            "content": {"text": text},
        }
        if chat_type == "group" and addressed:
            message["mentions"] = [
                {
                    "key": "@_user_1",
                    "name": "云云",
                    "id": {"open_id": "ou_core_weather_bot"},
                }
            ]
        if thread_id:
            message["thread_id"] = thread_id
        if root_id:
            message["root_id"] = root_id
        return {
            "header": {
                "event_id": f"event-{message_id}",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"open_id": sender_id},
                },
                "message": message,
            },
        }

    service = OfflineForecastService()
    mocked_sends: list[str] = []
    external_attempts: list[str] = []
    warning_adapter_calls: list[tuple[float, float, str]] = []

    async def fake_send(*args: Any, **kwargs: Any) -> str:
        message_id = f"mock-feishu-{len(mocked_sends) + 1}"
        mocked_sends.append(message_id)
        return message_id

    async def block_external(*args: Any, **kwargs: Any) -> Any:
        external_attempts.append("blocked")
        raise AssertionError("core replay attempted an external LLM/search/typhoon call")

    async def fake_warning_fetch(
        latitude: Any,
        longitude: Any,
        config: Any,
        *,
        source_registry: Any,
        source_policy: Any,
        **kwargs: Any,
    ) -> OfficialWarningFetchResult:
        del config, source_registry, kwargs
        if item.case_number != 50:
            external_attempts.append("unexpected_official_warning_adapter")
            raise AssertionError("unexpected official warning adapter call")
        warning_adapter_calls.append((float(latitude), float(longitude), source_policy.provider))
        issued = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).replace(minute=30)
        retrieved = issued.replace(hour=1, minute=0)
        source_url = (
            "https://warning-api.qweather.test/weatheralert/v1/current/"
            f"{latitude}/{longitude}"
        )
        return OfficialWarningFetchResult(
            status="ok",
            reason="active_warnings",
            source_tag="official-run-1",
            zero_result=False,
            attribution="QWeather；国家预警信息发布中心",
            retrieved_at=retrieved,
            source_url=source_url,
            content_sha256="d" * 64,
            warnings=(
                OfficialWarning(
                    warning_id="warning-1",
                    headline="山东省气象台发布高温橙色预警",
                    original_issuer="山东省气象台",
                    published_at=issued,
                    retrieved_at=retrieved,
                    effective_at=issued,
                    expires_at=issued + timedelta(hours=10),
                    source_url=source_url,
                    content_sha256="d" * 64,
                    source_tag="official-run-1",
                    message_type="Alert",
                    attribution="QWeather；国家预警信息发布中心",
                ),
            ),
        )

    original_db_path = weather_memory.DB_PATH
    try:
        with tempfile.TemporaryDirectory(prefix=f"weather-public-entry-{item.case_number}-") as temp_dir:
            temp_path = Path(temp_dir)
            weather_memory.DB_PATH = str(temp_path / "memory.db")
            settings = Settings(
                _env_file=None,
                app_env="test",
                feishu_allow_unsigned_events=True,
                feishu_passive_reply_enabled=True,
                dry_run=False,
                electricity_weather_analysis_enabled=True,
                manual_power_briefing_enabled=True,
                subscriptions_enabled=True,
                alert_evaluation_enabled=True,
                feishu_app_id=None,
                feishu_app_secret=None,
                feishu_verification_token=None,
                feishu_bot_open_id="ou_core_weather_bot",
                feishu_weather_bot_open_id="ou_core_weather_bot",
                llm_api_key=None,
                tavily_api_key=None,
                qweather_api_key="offline-warning-key" if item.case_number == 50 else None,
                qweather_api_host=(
                    "warning-api.qweather.test"
                    if item.case_number == 50
                    else "devapi.qweather.com"
                ),
                weather_source_policies_json=(
                    '[{"provider":"qweather_official_warning","environment":"test",'
                    '"profile":"warning-core-replay","license_status":"verified",'
                    '"allowed_uses":["text_reference","derived_storage"],'
                    '"terms_version":"test-only",'
                    '"source_url_prefixes":["https://warning-api.qweather.test/weatheralert/v1/current/"],'
                    '"unit_manifest":"warning_id:text;headline:text;original_issuer:text;published_at:iso8601;effective_at:iso8601;expires_at:iso8601;message_type:text;source_tag:text",'
                    '"required_metrics":["warning_id","headline","original_issuer","published_at","effective_at","expires_at","message_type","source_tag"],'
                    '"coverage_model":"latitude-longitude-point","timezone":"Asia/Shanghai",'
                    '"max_age_seconds":600,"retention_policy":"metadata_only",'
                    '"retention_seconds":86400,'
                    '"attribution_required":true,"attribution_text":"QWeather"}]'
                    if item.case_number == 50
                    else "[]"
                ),
                power_briefing_cache_db=str(temp_path / "briefing.db"),
                subscriptions_db=str(temp_path / "subscriptions.db"),
                alerts_db=str(temp_path / "alerts.db"),
            )
            if item.case_number in {23, 24}:
                failure = (
                    LocationNotFoundError("火星市")
                    if item.case_number == 23
                    else AmbiguousLocationError("朝阳", ("北京市朝阳区", "辽宁省朝阳市"))
                )
                resolver = OfflineLocationResolver(failure)
                provider = ForbiddenWeatherProvider()
                service = ForecastService(
                    providers={"open_meteo": provider},
                    settings=settings,
                    location_resolver=resolver,
                )
                service._core_replay_provider = provider  # type: ignore[attr-defined]
                service._core_replay_location_resolver = resolver  # type: ignore[attr-defined]
            with (
                mock.patch.object(FeishuClient, "send_text_message", fake_send),
                mock.patch.object(FeishuClient, "send_interactive_card", fake_send),
                mock.patch.object(FeishuClient, "reply_text_message", fake_send),
                mock.patch.object(FeishuClient, "reply_interactive_card", fake_send),
                mock.patch.object(LlmClient, "chat", block_external),
                mock.patch.object(TavilySearchClient, "search", block_external),
                mock.patch.object(TyphoonClient, "brief_for_text", block_external),
                mock.patch.object(TyphoonClient, "active_storms", block_external),
                mock.patch("services.weather_bot.main.fetch_official_warnings", fake_warning_fetch),
            ):
                app = create_app(settings=settings, forecast_service=service)
                with TestClient(app) as client:
                    def post(payload: dict[str, Any]) -> dict[str, Any]:
                        response = client.post("/feishu/events/weather", json=payload)
                        if response.status_code != 200:
                            return {"status": "http_error", "status_code": response.status_code}
                        body = response.json()
                        return body if isinstance(body, dict) else {"status": "invalid_body"}

                    passed, details = _run_public_event_scenario(
                        item.case_number,
                        post=post,
                        event=event,
                        service=service,
                        warning_adapter_calls=warning_adapter_calls,
                    )
    finally:
        weather_memory.DB_PATH = original_db_path

    evidence = {
        "executor": "public_feishu_event",
        "external_calls": len(external_attempts),
        "real_feishu_sends": 0,
        "mocked_feishu_boundary_calls": len(mocked_sends),
        **details,
    }
    if item.case_number == 5 and mocked_sends:
        passed = False
        evidence["unexpected_mocked_reply"] = True
    return passed and not external_attempts, evidence


def _run_public_event_scenario(
    case_number: int,
    *,
    post: Any,
    event: Any,
    service: Any,
    warning_adapter_calls: list[tuple[float, float, str]],
) -> tuple[bool, dict[str, Any]]:
    if case_number == 4:
        generated = post(
            event(
                "@云云 生成今天的电力气象决策晨报 2.0",
                message_id="case-4-generate",
                chat_type="group",
                addressed=True,
            )
        )
        requests_after_generation = len(service.requests)
        replied = post(
            event(
                "为什么风险升高",
                message_id="case-4-reply",
                chat_type="group",
                root_id=generated.get("event_reply_message_id"),
            )
        )
        passed = bool(
            generated.get("status") == "handled"
            and replied.get("status") == "handled"
            and replied.get("mode") == "power_briefing_explain"
            and replied.get("cache_hit") is True
            and replied.get("briefing_cache_key") == generated.get("briefing_cache_key")
            and len(service.requests) == requests_after_generation
        )
        return passed, {
            "generated_status": generated.get("status"),
            "reply_status": replied.get("status"),
            "reply_mode": replied.get("mode"),
            "same_snapshot": replied.get("briefing_cache_key") == generated.get("briefing_cache_key"),
            "weather_refetches": len(service.requests) - requests_after_generation,
        }
    if case_number == 5:
        replied = post(
            event(
                "为什么",
                message_id="case-5-reply",
                chat_type="group",
                root_id="ordinary-member-message",
            )
        )
        passed = replied.get("status") == "ignored" and len(service.requests) == 0
        return passed, {"reply_status": replied.get("status"), "reason": replied.get("reason")}
    if case_number == 11:
        result = post(event("讲个笑话", message_id="case-11-private"))
        passed = bool(
            result.get("status") == "handled"
            and result.get("mode") == "capability_boundary"
            and "天气" in str(result.get("text") or "")
            and len(service.requests) == 0
        )
        return passed, {"status": result.get("status"), "mode": result.get("mode")}
    if case_number in {23, 24}:
        result = post(
            event(
                _DOCUMENT_CASES[case_number - 1][0],
                message_id=f"case-{case_number}-location",
            )
        )
        provider_calls = service._core_replay_provider.calls
        resolver_calls = service._core_replay_location_resolver.calls
        expected_entity = "火星市" if case_number == 23 else "朝阳"
        expected_candidates = [] if case_number == 23 else ["北京市朝阳区", "辽宁省朝阳市"]
        expected_reason = "location_not_found" if case_number == 23 else "location_ambiguous"
        expected_prompt = "省、市或区县" if case_number == 23 else "请选择"
        passed = bool(
            result.get("status") == "needs_location_clarification"
            and result.get("mode") == "location_clarification"
            and result.get("reason") == expected_reason
            and result.get("location_entity") == expected_entity
            and result.get("location_candidates") == expected_candidates
            and expected_prompt in str(result.get("text") or "")
            and resolver_calls == 1
            and provider_calls == 0
        )
        return passed, {
            "status": result.get("status"),
            "mode": result.get("mode"),
            "reason": result.get("reason"),
            "location_entity": result.get("location_entity"),
            "location_candidates": result.get("location_candidates"),
            "location_resolver_calls": resolver_calls,
            "forecast_provider_calls": provider_calls,
            "llm_calls": 0,
            "search_calls": 0,
        }
    if case_number in {42, 44, 45}:
        result = post(event(_DOCUMENT_CASES[case_number - 1][0], message_id=f"case-{case_number}-boundary"))
        text = str(result.get("text") or "")
        required_fragments = {
            42: ("10米", "轮毂高度", "不能"),
            44: ("水电", "不能换算", "不会补造"),
            45: ("电网", "不能断言", "官方预警"),
        }[case_number]
        passed = bool(
            result.get("status") == "data_unavailable"
            and result.get("mode") == "external_power_data_required"
            and all(fragment in text for fragment in required_fragments)
            and len(service.requests) == 0
        )
        return passed, {
            "status": result.get("status"),
            "mode": result.get("mode"),
            "required_fragments_present": all(fragment in text for fragment in required_fragments),
            "weather_requests": len(service.requests),
            "llm_calls": 0,
        }
    if case_number == 50:
        result = post(
            event(
                _DOCUMENT_CASES[case_number - 1][0],
                message_id="case-50-official-warning",
            )
        )
        text = str(result.get("text") or "")
        passed = bool(
            result.get("status") == "handled"
            and result.get("mode") == "official_weather_warning"
            and result.get("source_kind") == "official_structured_api"
            and result.get("source_tag") == "official-run-1"
            and result.get("warning_count") == 1
            and "山东省气象台发布高温橙色预警" in text
            and "山东省气象台" in text
            and "发布时间" in text
            and "抓取时间" in text
            and "https://warning-api.qweather.test/weatheralert/v1/current/" in text
            and "搜索摘要" not in text
            and len(service.requests) == 0
            and len(warning_adapter_calls) == 1
            and warning_adapter_calls[0][2] == "qweather_official_warning"
        )
        return passed, {
            "status": result.get("status"),
            "mode": result.get("mode"),
            "source_kind": result.get("source_kind"),
            "source_run_id": result.get("source_tag"),
            "source_url": result.get("source_url"),
            "warning_adapter_calls": len(warning_adapter_calls),
            "forecast_provider_calls": len(service.requests),
            "llm_calls": 0,
            "search_calls": 0,
        }
    if case_number == 61:
        first_a = post(
            event(
                "@云云 预测下最近四天的气象数据",
                message_id="case-61-a-pending",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-a",
                addressed=True,
            )
        )
        first_b = post(
            event(
                "@云云 预测下最近四天的气象数据",
                message_id="case-61-b-pending",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-b",
                addressed=True,
            )
        )
        help_a = post(
            event(
                "@云云 云云能做什么",
                message_id="case-61-a-help",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-a",
                addressed=True,
            )
        )
        after_help_a = post(
            event(
                "@云云 广州",
                message_id="case-61-a-city",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-a",
                addressed=True,
            )
        )
        still_pending_b = post(
            event(
                "@云云 广州",
                message_id="case-61-b-city",
                chat_type="group",
                thread_id="thread-a",
                sender_id="user-b",
                addressed=True,
            )
        )
        passed = bool(
            first_a.get("status") == "needs_region"
            and first_b.get("status") == "needs_region"
            and help_a.get("status") == "handled"
            and after_help_a.get("status") == "ignored"
            and still_pending_b.get("status") == "handled"
            and still_pending_b.get("days") == 4
            and len(service.requests) == 4
        )
        return passed, {
            "cleared_scope_status": after_help_a.get("status"),
            "isolated_scope_status": still_pending_b.get("status"),
            "forecast_requests": len(service.requests),
        }
    if case_number == 62:
        post(event("广州天气", message_id="case-62-success"))
        service.failures_remaining = 2
        failed = post(event("换成北京", message_id="case-62-failed"))
        retried = post(event("重试一下", message_id="case-62-retry"))
        inherited = post(event("那明天呢", message_id="case-62-followup"))
        regions = [request.region for request in service.requests]
        passed = bool(
            failed.get("status") == "error_fallback"
            and retried.get("status") == "error_fallback"
            and inherited.get("status") == "handled"
            and len(regions) == 4
            and "广州" in regions[0]
            and "北京" in regions[1]
            and "北京" in regions[2]
            and "广州" in regions[3]
        )
        return passed, {
            "failed_status": failed.get("status"),
            "retry_status": retried.get("status"),
            "followup_status": inherited.get("status"),
            "request_regions": regions,
        }
    if case_number == 69:
        generated = post(
            event(
                "生成今天的电力气象决策晨报 3.0",
                message_id="case-69-generate",
            )
        )
        card = generated.get("card") or {}
        title = (
            ((card.get("card") or {}).get("header") or {}).get("title") or {}
        ).get("content")
        passed = bool(
            generated.get("status") == "handled"
            and generated.get("mode") == "power_briefing"
            and "电力气象交易晨报" in str(title or "")
            and (generated.get("coverage") or {}).get("markets", {}).get("total") == 33
            and service.requests
        )
        return passed, {
            "status": generated.get("status"),
            "mode": generated.get("mode"),
            "title": title,
            "configured_markets": (generated.get("coverage") or {}).get("markets", {}).get("total"),
            "weather_requests": len(service.requests),
            "user_region_required": False,
        }
    if case_number == 70:
        generated = post(
            event(
                "生成今天的电力气象决策晨报 3.0",
                message_id="case-70-generate",
            )
        )
        requests_after_generation = len(service.requests)
        expanded = post(
            event(
                "查看全部市场",
                message_id="case-70-expand",
            )
        )
        passed = bool(
            generated.get("status") == "handled"
            and expanded.get("status") == "handled"
            and expanded.get("mode") == "power_briefing_expand"
            and expanded.get("cache_hit") is True
            and expanded.get("briefing_cache_key") == generated.get("briefing_cache_key")
            and len(service.requests) == requests_after_generation
        )
        return passed, {
            "generated_status": generated.get("status"),
            "expanded_status": expanded.get("status"),
            "same_snapshot": expanded.get("briefing_cache_key") == generated.get("briefing_cache_key"),
            "weather_refetches": len(service.requests) - requests_after_generation,
        }
    if case_number == 71:
        expanded = post(
            event(
                "查看全部市场",
                message_id="case-71-expand-without-context",
            )
        )
        passed = bool(
            expanded.get("status") == "needs_briefing_context"
            and expanded.get("mode") == "power_briefing_expand"
            and len(service.requests) == 0
        )
        return passed, {
            "status": expanded.get("status"),
            "mode": expanded.get("mode"),
            "weather_requests": len(service.requests),
        }
    raise KeyError(f"Unsupported public event core case: {case_number}")


def _document_replay_case(case_number: int, *, today: date):
    from services.weather_bot.controlled_learning import (
        ReplayCase,
        ReplayExpectation,
        ReplayStateSeed,
    )

    def seed(
        region: str,
        *,
        days: int = 1,
        metrics: list[str] | None = None,
        chat_id: str = "chat-a",
        thread_id: str | None = None,
        user_id: str = "user-a",
        chat_type: str = "p2p",
        bot_role: str = "weather_forecast_bot",
        target_offset: int = 0,
    ) -> ReplayStateSeed:
        return ReplayStateSeed(
            bot_role=bot_role,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            chat_type=chat_type,
            last_successful_request={
                "region": region,
                "target_date": (today + timedelta(days=target_offset)).isoformat(),
                "days": days,
                "metrics": metrics or [],
            },
        )

    definitions: dict[int, dict[str, Any]] = {
        1: dict(text="山东明天天气", chat_type="group", addressed=False, expectation=ReplayExpectation(should_reply=False, intent="ignored")),
        2: dict(text="@云云 山东明天天气", chat_type="group", addressed=False, expectation=ReplayExpectation(should_reply=False, intent="ignored")),
        3: dict(text="@云云 山东明天天气", chat_type="group", addressed=True, expectation=ReplayExpectation(intent="weather", region="山东省", days=1, target_date_offset=1)),
        7: dict(text="@云云 看这个图片", chat_type="group", addressed=True, message_type="image", expectation=ReplayExpectation(should_reply=False, intent="ignored")),
        10: dict(text="@云云 讲个笑话", chat_type="group", addressed=True, expectation=ReplayExpectation(should_reply=False, intent="ignored")),
        12: dict(text="@云云 云云能做什么", chat_type="group", addressed=True, expectation=ReplayExpectation(intent="help")),
        14: dict(text="辽宁盘锦未来3天", expectation=ReplayExpectation(intent="weather", region="辽宁盘锦", days=3)),
        15: dict(text="西藏阿里地区天气", expectation=ReplayExpectation(intent="weather", region="西藏阿里地区", days=1)),
        16: dict(text="上海浦东新区天气", expectation=ReplayExpectation(intent="weather", region="上海浦东新区", days=1)),
        32: dict(text="山东未来7天", expectation=ReplayExpectation(intent="weather", region="山东省", days=7)),
        55: dict(text="那明天呢", state_seeds=[seed("广东省广州市", days=3)], expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=1, target_date_offset=1)),
        56: dict(text="换成盘锦", state_seeds=[seed("广东省广州市", metrics=["rain"], target_offset=1)], expectation=ReplayExpectation(intent="weather", region="辽宁盘锦", days=1, metrics=["rain"], target_date_offset=1)),
        57: dict(text="只看降雨", state_seeds=[seed("广东省广州市", days=3)], expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=3, metrics=["rain"])),
        58: dict(text="改成未来7天", state_seeds=[seed("广东省广州市", target_offset=1)], expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=7)),
        59: dict(text="不是广州，是深圳", state_seeds=[seed("广东省广州市", target_offset=1)], expectation=ReplayExpectation(intent="weather", region="广东省深圳市", days=1, target_date_offset=1)),
        60: dict(text="不要沿用刚才的", state_seeds=[seed("广东省广州市", target_offset=1)], expectation=ReplayExpectation(intent="context_reset")),
        63: dict(text="@云云 明天呢", chat_type="group", addressed=True, state_seeds=[seed("广东省广州市", chat_type="group"), seed("上海市", chat_type="group", user_id="user-b")], expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=1, target_date_offset=1)),
        64: dict(text="@云云 明天呢", chat_type="group", addressed=True, chat_id="group-a", state_seeds=[seed("广东省广州市", chat_id="group-a", chat_type="group"), seed("北京市", chat_id="group-b", chat_type="group")], expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=1, target_date_offset=1)),
        65: dict(text="@云云 明天呢", chat_type="group", addressed=True, thread_id="thread-a", state_seeds=[seed("广东省广州市", thread_id="thread-a", chat_type="group"), seed("北京市", thread_id="thread-b", chat_type="group")], expectation=ReplayExpectation(intent="weather", region="广东省广州市", days=1, target_date_offset=1)),
        66: dict(text="明天呢", bot_scope="task", state_seeds=[seed("广东省广州市")], expectation=ReplayExpectation(intent="general")),
    }
    definition = definitions[case_number]
    return ReplayCase(
        case_id=f"doc-{case_number:03d}",
        category=_section_for(case_number),
        **definition,
    )


def _execute_event_deduplication(executor_id: str) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot import memory as weather_memory

    original_db_path = weather_memory.DB_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="weather-event-ledger-") as temp_dir:
            weather_memory.DB_PATH = str(Path(temp_dir) / "memory.db")
            first = weather_memory.claim_event("weather", "event-9")
            weather_memory.complete_event("weather", "event-9")
            second = weather_memory.claim_event("weather", "event-9")
    finally:
        weather_memory.DB_PATH = original_db_path
    return first and not second, {"executor": executor_id, "first_claim": first, "duplicate_claim": second}


def _execute_memory_restart(executor_id: str, *, expired: bool) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot import memory as weather_memory

    original_db_path = weather_memory.DB_PATH
    clock = {"now": 1_000.0}
    try:
        with tempfile.TemporaryDirectory(prefix="weather-memory-ttl-") as temp_dir:
            weather_memory.DB_PATH = str(Path(temp_dir) / "memory.db")
            with mock.patch.object(weather_memory.time, "time", side_effect=lambda: clock["now"]):
                weather_memory.save_conversation_state(
                    "weather_forecast_bot|p2p|chat-a|main|user-a",
                    {"state_version": 2, "last_successful_request": {"region": "广州"}},
                    ttl_seconds=600,
                )
                clock["now"] += 600 if expired else 599
                loaded = weather_memory.load_conversation_state(
                    "weather_forecast_bot|p2p|chat-a|main|user-a"
                )
    finally:
        weather_memory.DB_PATH = original_db_path
    passed = loaded is None if expired else bool(loaded and loaded.get("last_successful_request"))
    return passed, {"executor": executor_id, "expired": expired, "state_loaded": bool(loaded)}


def _execute_public_admin_api(item: CoreReplayItem) -> tuple[bool, dict[str, Any]]:
    from fastapi.testclient import TestClient

    from services.weather_bot.config import Settings
    from services.weather_bot.feishu import FeishuClient
    from services.weather_bot.main import create_app
    from services.weather_bot.models import (
        AggregatedForecast,
        ForecastPoint,
        ForecastSummary,
        WeatherSubmission,
    )

    class OfflineForecastService:
        def __init__(self) -> None:
            self.calls = 0

        async def forecast(self, request: Any) -> WeatherSubmission:
            self.calls += 1
            return WeatherSubmission(
                task_id="WEATHER-CN-440300-20260810-DAYAHEAD-001",
                region="Shenzhen",
                target_date="2026-08-10",
                data_cutoff_time="2026-08-09T16:00:00+08:00",
                provider_results=[],
                aggregated_forecast=AggregatedForecast(
                    providers_used=["offline_fixture"],
                    points=[
                        ForecastPoint(
                            time="2026-08-10T00:00:00+08:00",
                            temperature=28.0,
                            precipitation_probability=20.0,
                            wind_speed=2.0,
                            cloud_cover=60.0,
                        )
                    ],
                    summary=ForecastSummary(
                        max_temperature=28.0,
                        min_temperature=28.0,
                        rain_probability=20.0,
                        wind_speed=2.0,
                        cloud_cover=60.0,
                        main_weather="cloudy",
                        high_risk_period="none",
                    ),
                ),
                confidence={"score": 0.7, "description": "medium"},
                key_factors=["offline replay fixture"],
                risk_notes=["offline replay fixture"],
                disclaimer="weather information only",
            )

    async def record_send(*args: Any, **kwargs: Any) -> str:
        sends.append("feishu")
        return "should-not-send"

    async def record_write(*args: Any, **kwargs: Any) -> None:
        writes.append("bitable")

    sends: list[str] = []
    writes: list[str] = []
    service = OfflineForecastService()
    with tempfile.TemporaryDirectory(prefix="weather-admin-api-replay-") as temp_dir:
        root = Path(temp_dir)
        settings = Settings(
            _env_file=None,
            admin_api_token="offline-admin-token",
            admin_api_send_enabled=True,
            admin_api_send_targets_json='["oc_reviewed"]',
            admin_api_audit_db=str(root / "admin_actions.db"),
            global_feishu_send_enabled=False,
            feishu_weather_default_chat_id="oc_reviewed",
            local_jsonl_path=str(root / "submissions.jsonl"),
            local_task_jsonl_path=str(root / "tasks.jsonl"),
            local_locations_path=str(root / "locations.json"),
            local_news_jsonl_path=str(root / "news.jsonl"),
            local_hydrology_jsonl_path=str(root / "hydrology.jsonl"),
            subscriptions_db=str(root / "subscriptions.db"),
            alerts_db=str(root / "alerts.db"),
            power_briefing_cache_db=str(root / "briefing.db"),
        )
        with (
            mock.patch.object(FeishuClient, "send_interactive_card", record_send),
            mock.patch.object(FeishuClient, "write_bitable_record", record_write),
        ):
            client = TestClient(create_app(forecast_service=service, settings=settings))
            headers = (
                {"Authorization": "Bearer offline-admin-token"}
                if item.case_number == 86
                else {}
            )
            response = client.post(
                "/api/weather/publish",
                headers=headers,
                json={"region": "Shenzhen", "target_date": "2026-08-10"},
            )

    body = response.json() if response.status_code == 200 else {}
    delivery_reason = (body.get("delivery") or {}).get("reason")
    passed = (
        response.status_code in {401, 403}
        and service.calls == 0
        and not sends
        and not writes
        if item.case_number == 85
        else response.status_code == 200
        and service.calls == 1
        and delivery_reason == "global_send_disabled"
        and not sends
        and not writes
    )
    return passed, {
        "executor": "public_admin_api",
        "status_code": response.status_code,
        "delivery_reason": delivery_reason,
        "forecast_calls": service.calls,
        "feishu_sends": len(sends),
        "bitable_writes": len(writes),
        "external_calls": 0,
    }


def _execute_electricity_entities(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.electricity_entities import parse_electricity_entities

    shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.combine(today, datetime.min.time(), tzinfo=shanghai).replace(hour=8)
    entities = parse_electricity_entities(item.input_text, now=now)
    areas = entities.analysis_areas
    period = entities.forecast_period
    window = entities.trading_window
    boundary = entities.data_boundary

    checks: dict[int, bool] = {
        13: bool(areas and areas[0].name == "辽宁" and period and period.days == 7),
        17: bool(areas and areas[0].kind == "regional_collection" and areas[0].name == "华东"),
        18: bool(areas and areas[0].area_id == "cn-15-mengxi"),
        19: bool(areas and areas[0].area_id == "cn-15-mengdong"),
        20: bool(areas and areas[0].kind == "national" and len(areas[0].analysis_zone_ids) == 33),
        21: [area.name for area in areas] == ["山东", "河南", "河北"],
        22: bool(
            not areas
            and entities.clarification_required
            and "ambiguous_analysis_scope" in entities.clarification_reasons
        ),
        25: bool(
            areas
            and areas[0].name == "山东"
            and areas[0].evidence.raw_text == "鲁"
            and areas[0].evidence.rule_id == "province_abbreviation"
        ),
        26: bool(
            areas
            and areas[0].name == "山东"
            and window
            and window.kind == "evening_peak"
            and window.start_time.hour == 17
            and window.end_time.hour == 21
        ),
        27: bool(window and window.kind == "midday_solar" and window.start_time.hour == 11 and window.end_time.hour == 16),
        28: bool(
            entities.clarification_required
            and "elapsed_trading_window" in entities.clarification_reasons
            and window is None
        ),
        29: bool(
            window
            and window.kind == "intraday_remaining"
            and window.start_at == now
            and window.end_at
            and window.end_at.isoformat() == "2026-08-10T00:00:00+08:00"
        ),
        30: bool(
            window
            and window.kind == "relative_hours"
            and window.start_at == now
            and window.end_at == now + timedelta(hours=6)
        ),
        31: bool(period and period.days == 3 and period.horizon_kind == "multi_day_trend"),
        33: bool(
            areas
            and areas[0].kind == "regional_collection"
            and areas[0].name == "华东"
            and period
            and period.start_date.isoformat() == "2026-08-16"
            and period.end_date.isoformat() == "2026-08-23"
            and period.horizon_kind == "extended_outlook"
        ),
        34: bool(
            period
            and period.start_date.isoformat() == "2026-08-12"
            and window
            and window.start_at
            and window.start_at.isoformat() == "2026-08-12T17:00:00+08:00"
        ),
        35: bool(
            window
            and window.kind == "explicit_clock_range"
            and window.start_time.hour == 17
            and window.end_time.hour == 21
            and window.window_source == "explicit_user_text"
        ),
        36: bool(
            areas
            and areas[0].name == "山东"
            and period
            and period.start_date.isoformat() == "2026-08-10"
            and window
            and window.kind == "full_day"
            and window.window_source == "explicit_user_text"
            and window.start_at
            and window.start_at.isoformat() == "2026-08-10T00:00:00+08:00"
            and window.end_at
            and window.end_at.isoformat() == "2026-08-11T00:00:00+08:00"
        ),
        37: bool(
            window is None
            and entities.clarification_required
            and "invalid_clock_time" in entities.clarification_reasons
        ),
        38: bool(
            not areas
            and period
            and period.evidence.raw_text == "7月下旬"
            and entities.clarification_required
        ),
        51: boundary.blocked_fact_types == ("price",),
        53: boundary.blocked_fact_types == ("actual_generation",),
        54: boundary.blocked_fact_types == ("actual_load",),
    }
    passed = checks.get(item.case_number, False)
    return passed, {
        "executor": "electricity_entities",
        "analysis_area_ids": [area.area_id for area in areas],
        "forecast_days": period.days if period else None,
        "trading_window": window.kind if window else None,
        "clarification_reasons": list(entities.clarification_reasons),
        "blocked_fact_types": list(boundary.blocked_fact_types),
        "external_data_used": False,
    }


def _execute_briefing_card(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.models import (
        AggregatedForecast,
        ForecastPoint,
        ForecastSummary,
        ForecastWindow,
        ProviderForecast,
        TimeInfo,
        WeatherSubmission,
    )
    from services.weather_bot.power_briefing import build_briefing_card

    tomorrow = today + timedelta(days=1)

    def submission(
        target: date,
        *,
        hot_hours: frozenset[int] = frozenset(),
        cloudy_hours: frozenset[int] = frozenset(),
        windy_hours: frozenset[int] = frozenset(),
    ) -> WeatherSubmission:
        points = [
            ForecastPoint(
                time=f"{target.isoformat()}T{hour:02d}:00:00+08:00",
                temperature=37.0 if hour in hot_hours else 25.0,
                apparent_temperature=39.0 if hour in hot_hours else 26.0,
                precipitation_probability=20.0,
                cloud_cover=85.0 if hour in cloudy_hours else 50.0,
                wind_speed=11.0 if hour in windy_hours else 4.0,
            )
            for hour in range(24)
        ]
        retrieved_at = f"{today.isoformat()}T08:00:00+08:00"
        return WeatherSubmission(
            task_id=f"briefing-card-{target.isoformat()}",
            region="离线代表点",
            target_date=target.isoformat(),
            data_cutoff_time=retrieved_at,
            time_info=TimeInfo(
                retrieved_at=retrieved_at,
                provider_issued_at={"offline_test": f"{today.isoformat()}T07:00:00+08:00"},
                aggregation_completed_at=f"{today.isoformat()}T08:00:01+08:00",
                valid_time=ForecastWindow(
                    start=f"{target.isoformat()}T00:00:00+08:00",
                    end=f"{target.isoformat()}T23:00:00+08:00",
                    timezone="Asia/Shanghai",
                ),
                forecast_run_id=f"offline-{target.isoformat()}",
            ),
            provider_results=[
                ProviderForecast(
                    provider="offline_test",
                    status="ok",
                    points=points,
                    retrieved_at=retrieved_at,
                    provider_issued_at=f"{today.isoformat()}T07:00:00+08:00",
                    source_url="https://example.test/weather",
                    content_sha256="a" * 64,
                )
            ],
            aggregated_forecast=AggregatedForecast(
                providers_used=["offline_test"],
                points=points,
                summary=ForecastSummary(
                    max_temperature=max(point.temperature or 0 for point in points),
                    min_temperature=25.0,
                    rain_probability=20.0,
                    wind_speed=max(point.wind_speed or 0 for point in points),
                    cloud_cover=max(point.cloud_cover or 0 for point in points),
                    main_weather="多云",
                    high_risk_period="无明显风险",
                ),
            ),
            confidence={"score": 0.8, "description": "离线确定性夹具"},
            key_factors=[],
            risk_notes=[],
        )

    def row(
        index: int,
        *,
        roles: list[str],
        hot_hours: frozenset[int] = frozenset(),
        cloudy_hours: frozenset[int] = frozenset(),
        windy_hours: frozenset[int] = frozenset(),
    ) -> dict[str, Any]:
        return {
            "market_id": f"offline-market-{index:02d}",
            "market": f"分析区{index:02d}",
            "province": f"省级地区{index:02d}",
            "point_id": f"point-{index:02d}",
            "city": f"代表点{index:02d}",
            "roles": roles,
            "submissions": {
                today.isoformat(): submission(today),
                tomorrow.isoformat(): submission(
                    tomorrow,
                    hot_hours=hot_hours,
                    cloudy_hours=cloudy_hours,
                    windy_hours=windy_hours,
                ),
            },
            "errors": [],
        }

    if item.case_number == 39:
        rows = [row(1, roles=["load"], hot_hours=frozenset(range(17, 22)))]
    elif item.case_number == 40:
        rows = [row(1, roles=["solar"], cloudy_hours=frozenset(range(6, 20)))]
    elif item.case_number == 41:
        rows = [row(1, roles=["wind"], windy_hours=frozenset(range(16, 23)))]
    elif item.case_number == 72:
        rows = [row(1, roles=["load"], hot_hours=frozenset(range(17, 22)))]
    elif item.case_number == 73:
        rows = [row(index, roles=["load", "solar", "wind"]) for index in range(1, 27)]
    else:
        raise KeyError(f"Unsupported briefing card core case: {item.case_number}")
    card = build_briefing_card(
        rows,
        today.isoformat(),
        generated_at=datetime.combine(
            today,
            datetime.min.time(),
            tzinfo=timezone(timedelta(hours=8), name="Asia/Shanghai"),
        ).replace(hour=9),
    )
    text_fragments: list[str] = []
    header = ((card.get("card") or {}).get("header") or {}).get("title") or {}
    text_fragments.append(str(header.get("content") or ""))
    for element in (card.get("card") or {}).get("elements") or []:
        text = element.get("text") if isinstance(element, dict) else None
        if isinstance(text, dict):
            text_fragments.append(str(text.get("content") or ""))
        if isinstance(element, dict):
            for child in element.get("elements") or []:
                if isinstance(child, dict):
                    text_fragments.append(str(child.get("content") or ""))
    card_text = "\n".join(text_fragments)
    checks = {
        39: "负荷天气压力代理↑" in card_text and "负荷天气压力（同类代表点等权汇总）" in card_text,
        40: "光伏资源代理↓" in card_text and "光资源转弱代理（同类代表点等权汇总）" in card_text,
        41: "地面风资源代理↑" in card_text and "10米风仅作地面风资源代理" in card_text,
        72: "稳定分析区 0 个" not in card_text,
        73: (
            "稳定分析区 26 个，精简卡未逐一列出" in card_text
            and "稳定市场 26 个，已折叠" not in card_text
        ),
    }
    passed = checks[item.case_number]
    return passed, {
        "executor": "briefing_card",
        "case_number": item.case_number,
        "stable_zero_section_omitted": "稳定分析区 0 个" not in card_text,
        "natural_language_remaining_line": "稳定分析区 26 个，精简卡未逐一列出" in card_text,
        "external_calls": 0,
        "feishu_sends": 0,
    }


def _execute_weather_risk_evidence(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.models import (
        AggregatedForecast,
        ForecastPoint,
        ForecastSummary,
        ForecastWindow,
        ProviderForecast,
        TimeInfo,
        WeatherSubmission,
    )
    from services.weather_bot.weather_risk_evidence import (
        analyze_provider_disagreement,
        compare_forecast_versions,
        detect_wind_ramp_windows,
        explain_forecast_confidence,
        rank_renewable_forecast_complexity,
    )

    target = today + timedelta(days=1)
    shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")

    def point(
        hour: int,
        *,
        wind: float = 4.0,
        direction: float = 90.0,
        cloud: float = 30.0,
        rain: float = 10.0,
        temperature: float = 30.0,
    ) -> ForecastPoint:
        return ForecastPoint(
            time=f"{target.isoformat()}T{hour:02d}:00:00+08:00",
            temperature=temperature,
            apparent_temperature=temperature + 1.0,
            precipitation_probability=rain,
            cloud_cover=cloud,
            wind_speed=wind,
            wind_direction=direction,
        )

    def submission(
        region: str,
        run_id: str,
        provider_points: dict[str, list[ForecastPoint]],
        *,
        retrieved_at: str | None = None,
    ) -> WeatherSubmission:
        retrieved = retrieved_at or f"{today.isoformat()}T08:00:00+08:00"
        providers = [
            ProviderForecast(
                provider=provider_name,
                status="ok",
                points=points,
                retrieved_at=retrieved,
                provider_issued_at=retrieved,
                source_url=f"https://official.example.test/{provider_name}/forecast",
                content_sha256=(str(index + 1) * 64)[:64],
            )
            for index, (provider_name, points) in enumerate(provider_points.items())
        ]
        aggregated_points = list(provider_points.values())[0]
        return WeatherSubmission(
            task_id=f"risk-evidence-{run_id}",
            region=region,
            target_date=target.isoformat(),
            data_cutoff_time=f"{today.isoformat()}T16:00:00+08:00",
            time_info=TimeInfo(
                retrieved_at=retrieved,
                provider_issued_at={name: retrieved for name in provider_points},
                aggregation_completed_at=f"{today.isoformat()}T08:00:01+08:00",
                valid_time=ForecastWindow(
                    start=f"{target.isoformat()}T00:00:00+08:00",
                    end=f"{target.isoformat()}T23:00:00+08:00",
                    timezone="Asia/Shanghai",
                ),
                forecast_run_id=run_id,
                business_submission_deadline=f"{today.isoformat()}T16:00:00+08:00",
            ),
            provider_results=providers,
            aggregated_forecast=AggregatedForecast(
                providers_used=list(provider_points),
                points=aggregated_points,
                summary=ForecastSummary(main_weather="离线结构化预报", high_risk_period="无"),
            ),
            confidence={"score": 0.7, "description": "离线规则夹具"},
            key_factors=[],
            risk_notes=[],
        )

    source_run_ids: list[str] = []
    source_urls: list[str] = []
    valid_times: list[str] = []
    if item.case_number == 43:
        structured = submission(
            "内蒙古",
            "run-wind-ramp",
            {
                "source_a": [
                    point(13, wind=4),
                    point(14, wind=4),
                    point(15, wind=8),
                    point(16, wind=12),
                    point(17, wind=12),
                ]
            },
        )
        windows = detect_wind_ramp_windows(structured.provider_results[0].points)
        passed = bool(
            len(windows) == 1
            and windows[0].start == f"{target.isoformat()}T14:00:00+08:00"
            and windows[0].end == f"{target.isoformat()}T16:00:00+08:00"
            and windows[0].metric_label == "10米地面风快速变化代理"
            and "不能解释为实际风电爬坡" in windows[0].boundary
        )
        source_run_ids = [structured.time_info.forecast_run_id]
        source_urls = [str(structured.provider_results[0].source_url)]
        valid_times = [windows[0].start, windows[0].end] if windows else []
        details = {"window_count": len(windows), "metric_label": windows[0].metric_label if windows else None}
    elif item.case_number == 46:
        stable = submission(
            "山东",
            "run-shandong",
            {
                "source_a": [point(14, cloud=30, rain=10, wind=4), point(15, cloud=32, rain=10, wind=4)],
                "source_b": [point(14, cloud=32, rain=15, wind=4), point(15, cloud=34, rain=15, wind=5)],
            },
        )
        complex_weather = submission(
            "广东",
            "run-guangdong",
            {
                "source_a": [point(14, cloud=10, rain=10, wind=3), point(15, cloud=20, rain=10, wind=3)],
                "source_b": [point(14, cloud=95, rain=90, wind=9), point(15, cloud=90, rain=85, wind=12)],
            },
        )
        ranking = rank_renewable_forecast_complexity({"山东": stable, "广东": complex_weather})
        passed = bool(
            ranking.status == "available"
            and [entry.region for entry in ranking.entries] == ["广东", "山东"]
            and ranking.entries[0].score > ranking.entries[1].score
            and ranking.metric_label == "新能源预测复杂度气象代理"
            and "不能视为实际新能源预测偏差" in ranking.boundary
            and all(entry.source_run_id for entry in ranking.entries)
        )
        source_run_ids = [entry.source_run_id for entry in ranking.entries]
        source_urls = [
            str(provider.source_url)
            for structured in (stable, complex_weather)
            for provider in structured.provider_results
        ]
        valid_times = [f"{target.isoformat()}T14:00:00+08:00", f"{target.isoformat()}T15:00:00+08:00"]
        details = {"ranking": [entry.model_dump(mode="json") for entry in ranking.entries]}
    elif item.case_number == 47:
        previous = submission(
            "山东省济南市",
            "run-previous",
            {"source_a": [point(17, temperature=34, wind=4), point(18, temperature=35, wind=5)]},
            retrieved_at=f"{(today - timedelta(days=1)).isoformat()}T08:00:00+08:00",
        )
        current = submission(
            "山东省济南市",
            "run-current",
            {"source_a": [point(17, temperature=37, wind=6), point(18, temperature=36, wind=5)]},
        )
        comparison = compare_forecast_versions(current, previous)
        first_change = comparison.changes[0] if comparison.changes else None
        passed = bool(
            comparison.status == "available"
            and comparison.reason == "same_scope_same_valid_time"
            and comparison.current_run_id == "run-current"
            and comparison.previous_run_id == "run-previous"
            and comparison.comparable_valid_times == 2
            and first_change
            and first_change.variable == "temperature"
            and first_change.valid_time == f"{target.isoformat()}T17:00:00+08:00"
            and first_change.delta == 3.0
            and "同一地点、同一有效时刻" in comparison.boundary
        )
        source_run_ids = [comparison.current_run_id or "", comparison.previous_run_id or ""]
        source_urls = [str(current.provider_results[0].source_url), str(previous.provider_results[0].source_url)]
        valid_times = [change.valid_time for change in comparison.changes]
        details = {"comparable_valid_times": comparison.comparable_valid_times, "change_count": len(comparison.changes)}
    elif item.case_number == 48:
        structured = submission(
            "浙江省杭州市",
            "run-provider-disagreement",
            {
                "source_a": [point(11, cloud=20, rain=10), point(12, cloud=25, rain=10)],
                "source_b": [point(11, cloud=80, rain=70), point(12, cloud=30, rain=15)],
            },
        )
        disagreement = analyze_provider_disagreement(structured)
        first_item = disagreement.items[0] if disagreement.items else None
        passed = bool(
            disagreement.status == "available"
            and disagreement.source_run_id == "run-provider-disagreement"
            and first_item
            and first_item.variable == "cloud_cover"
            and first_item.valid_time == f"{target.isoformat()}T11:00:00+08:00"
            and first_item.spread == 60.0
            and first_item.providers == ("source_a", "source_b")
            and "多数源" in disagreement.boundary
        )
        source_run_ids = [disagreement.source_run_id or ""]
        source_urls = [str(provider.source_url) for provider in structured.provider_results]
        valid_times = [entry.valid_time for entry in disagreement.items]
        details = {"top_variable": first_item.variable if first_item else None, "top_valid_time": first_item.valid_time if first_item else None}
    elif item.case_number == 49:
        structured = submission(
            "山东省济南市",
            "run-confidence",
            {
                "source_a": [point(hour, cloud=20) for hour in range(24)],
                "source_b": [point(hour, cloud=22) for hour in range(24)],
            },
        )
        explanation = explain_forecast_confidence(
            structured,
            now=datetime.combine(today, datetime.min.time(), tzinfo=shanghai).replace(hour=9),
        )
        passed = bool(
            explanation.level in {"较高", "中等", "偏低"}
            and explanation.factors["coverage"].status == "good"
            and explanation.factors["freshness"].status == "good"
            and explanation.factors["source_consistency"].status == "good"
            and explanation.factors["historical_skill"].status == "unavailable"
            and all(fragment in explanation.explanation for fragment in ("覆盖", "时效", "分歧"))
            and "不使用大模型主观补分" in explanation.boundary
        )
        source_run_ids = [structured.time_info.forecast_run_id]
        source_urls = [str(provider.source_url) for provider in structured.provider_results]
        valid_times = [structured.time_info.valid_time.start, structured.time_info.valid_time.end]
        details = {"level": explanation.level, "score": explanation.score, "factor_statuses": {name: factor.status for name, factor in explanation.factors.items()}}
    else:
        raise KeyError(f"Unsupported weather risk evidence core case: {item.case_number}")

    traceable_urls = bool(source_urls) and all(url.startswith("https://") for url in source_urls)
    passed = passed and bool(source_run_ids) and all(source_run_ids) and traceable_urls and bool(valid_times)
    return passed, {
        "executor": "weather_risk_evidence",
        "case_number": item.case_number,
        "source_run_ids": source_run_ids,
        "source_urls": source_urls,
        "valid_times": valid_times,
        "external_calls": 0,
        "feishu_sends": 0,
        **details,
    }


def _execute_subscription_coordinator(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.subscription_runtime import SubscriptionCoordinator
    from services.weather_bot.subscriptions import ConversationScope, SubscriptionStore

    shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.combine(today, datetime.min.time(), tzinfo=shanghai).replace(hour=8)

    def scope(
        *,
        user_id: str = "user-a",
        thread_id: str = "thread-a",
        chat_type: str = "p2p",
    ) -> ConversationScope:
        return ConversationScope(
            bot_role="weather_forecast_bot",
            chat_type=chat_type,
            chat_id="chat-a",
            thread_id=thread_id,
            user_id=user_id,
        )

    with tempfile.TemporaryDirectory(prefix=f"weather-subscription-{item.case_number}-") as temp_dir:
        store = SubscriptionStore(Path(temp_dir) / "subscriptions.db")
        coordinator = SubscriptionCoordinator(store)
        if item.case_number == 74:
            draft = coordinator.handle(
                "每天8:30给我看山东、河南和河北",
                scope(),
                actor_is_admin=False,
                now=now,
            )
            passed = bool(
                draft
                and draft["status"] == "subscription_draft"
                and draft["subscription"]["status"] == "draft"
                and draft["subscription"]["spec"]["regions"] == ("山东", "河南", "河北")
                and draft["subscription"]["spec"]["schedule_time"] == "08:30"
                and draft["send_performed"] is False
            )
            details = {"status": draft.get("status") if draft else None}
        elif item.case_number == 75:
            owner_scope = scope()
            coordinator.handle(
                "广东体感温度超过38℃时提醒我",
                owner_scope,
                actor_is_admin=False,
                now=now,
            )
            result = coordinator.handle(
                "确认订阅",
                scope(thread_id="thread-b"),
                actor_is_admin=False,
                now=now + timedelta(minutes=1),
            )
            current = store.find_latest(owner_scope)
            passed = bool(
                result
                and result["status"] == "subscription_context_missing"
                and current
                and current.status == "draft"
            )
            details = {"status": result.get("status") if result else None, "owner_status": current.status if current else None}
        elif item.case_number in {76, 77}:
            member = scope(chat_type="group")
            admin = scope(user_id="group-admin", chat_type="group")
            coordinator.handle(
                "广东体感温度超过38℃时提醒我",
                member,
                actor_is_admin=False,
                now=now,
            )
            pending = coordinator.handle(
                "确认订阅",
                member,
                actor_is_admin=False,
                now=now + timedelta(minutes=1),
            )
            if item.case_number == 76:
                passed = bool(
                    pending
                    and pending["status"] == "subscription_pending_confirmation"
                    and pending["subscription"]["status"] == "pending_confirmation"
                    and pending["send_performed"] is False
                )
                details = {"status": pending.get("status") if pending else None}
            else:
                active = coordinator.handle(
                    "确认订阅",
                    admin,
                    actor_is_admin=True,
                    now=now + timedelta(minutes=2),
                )
                passed = bool(
                    active
                    and active["status"] == "subscription_active"
                    and active["subscription"]["status"] == "active"
                    and active["subscription"]["backfill_from"] is None
                    and active["subscription"]["confirmed_by_user_id"] == "group-admin"
                    and active["send_performed"] is False
                )
                details = {
                    "status": active.get("status") if active else None,
                    "backfill_from": active["subscription"].get("backfill_from") if active else None,
                }
        elif item.case_number == 78:
            conversation = scope()
            draft = coordinator.handle(
                "广东体感温度超过38℃时提醒我",
                conversation,
                actor_is_admin=False,
                now=now,
            )
            updated = coordinator.handle(
                "把阈值38℃改成39℃",
                conversation,
                actor_is_admin=False,
                now=now + timedelta(minutes=1),
            )
            subscription_id = str((draft or {}).get("subscription", {}).get("subscription_id") or "")
            history = store.history(subscription_id) if subscription_id else []
            passed = bool(
                updated
                and updated["status"] == "subscription_updated"
                and updated["subscription"]["spec"]["trigger_threshold"] == 39.0
                and [record.spec.trigger_threshold for record in history] == [38.0, 39.0]
                and [record.version for record in history] == [1, 2]
            )
            details = {"versions": [record.version for record in history]}
        elif item.case_number == 79:
            conversation = scope()
            draft = coordinator.handle(
                "广东体感温度超过38℃时提醒我",
                conversation,
                actor_is_admin=False,
                now=now,
            )
            first = coordinator.handle(
                "取消订阅",
                conversation,
                actor_is_admin=False,
                now=now + timedelta(minutes=1),
            )
            second = coordinator.handle(
                "取消订阅",
                conversation,
                actor_is_admin=False,
                now=now + timedelta(minutes=2),
            )
            subscription_id = str((draft or {}).get("subscription", {}).get("subscription_id") or "")
            history = store.history(subscription_id) if subscription_id else []
            passed = bool(
                first
                and second
                and first["status"] == second["status"] == "subscription_cancelled"
                and first["subscription"]["version"] == second["subscription"]["version"] == 2
                and len(history) == 2
            )
            details = {"history_length": len(history), "final_version": second["subscription"]["version"] if second else None}
        else:
            raise KeyError(f"Unsupported subscription core case: {item.case_number}")
        del coordinator, store
        gc.collect()
    return passed, {
        "executor": "subscription_coordinator",
        "case_number": item.case_number,
        "send_performed": False,
        **details,
    }


def _execute_alert_engine(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.alerts import AlertEngine, AlertObservation, AlertOutbox
    from services.weather_bot.subscriptions import (
        ConversationScope,
        SubscriptionSpec,
        SubscriptionStore,
    )

    shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.combine(today, datetime.min.time(), tzinfo=shanghai).replace(hour=8)

    def observation(
        run_id: str,
        value: float,
        *,
        severity: str = "high",
        at: datetime | None = None,
        available: bool = True,
    ) -> AlertObservation:
        return AlertObservation(
            source_run_id=run_id,
            provenance_ref=f"forecast-run:{run_id}" if available else None,
            availability_status="allowed_for_calculation" if available else "rejected",
            observed_at=at or now,
            risk_window=f"{today.isoformat()}T14:00:00+08:00/{today.isoformat()}T18:00:00+08:00",
            value=value,
            severity=severity,
            data_available=available,
        )

    with tempfile.TemporaryDirectory(prefix=f"weather-alert-{item.case_number}-") as temp_dir:
        subscriptions = SubscriptionStore(Path(temp_dir) / "subscriptions.db")
        outbox = AlertOutbox(Path(temp_dir) / "alerts.db")
        engine = AlertEngine(outbox)
        scope = ConversationScope(
            bot_role="weather_forecast_bot",
            chat_type="p2p",
            chat_id="chat-a",
            thread_id="thread-a",
            user_id="user-a",
        )
        spec = SubscriptionSpec(
            kind="threshold",
            regions=("广东",),
            metric="apparent_temperature",
            operator=">=",
            trigger_threshold=38.0,
            recovery_threshold=36.0,
            consecutive_hits=2,
            cooldown_seconds=6 * 3600,
        )
        draft = subscriptions.create_draft(scope, spec, now=now)
        rule = subscriptions.confirm(
            draft.subscription_id,
            scope,
            explicit_confirmation=True,
            actor_is_admin=False,
            now=now,
        )
        if item.case_number == 80:
            actions = [
                engine.evaluate(rule, observation("run-1", 39), now=now).action,
                engine.evaluate(rule, observation("run-2", 40), now=now + timedelta(minutes=1)).action,
                engine.evaluate(rule, observation("run-3", 41), now=now + timedelta(minutes=2)).action,
                engine.evaluate(rule, observation("run-3", 41), now=now + timedelta(minutes=2)).action,
            ]
            passed = actions == ["pending_trigger", "triggered", "suppressed_active", "duplicate_observation"] and len(outbox.pending()) == 1
            details = {"actions": actions}
        elif item.case_number == 81:
            engine.evaluate(rule, observation("run-1", 39, severity="medium"), now=now)
            engine.evaluate(rule, observation("run-2", 40, severity="medium"), now=now + timedelta(minutes=1))
            suppressed = engine.evaluate(rule, observation("run-3", 41, severity="medium"), now=now + timedelta(minutes=2))
            passed = suppressed.action == "suppressed_active" and len(outbox.pending()) == 1
            details = {"action": suppressed.action}
        elif item.case_number == 82:
            engine.evaluate(rule, observation("run-1", 39), now=now)
            engine.evaluate(rule, observation("run-2", 40), now=now + timedelta(minutes=1))
            actions = [
                engine.evaluate(rule, observation("run-3", 35), now=now + timedelta(hours=1)).action,
                engine.evaluate(rule, observation("run-4", 35), now=now + timedelta(hours=2)).action,
                engine.evaluate(rule, observation("run-5", 34), now=now + timedelta(hours=3)).action,
            ]
            kinds = [queued.kind for queued in outbox.pending()]
            passed = actions == ["pending_recovery", "recovered", "inactive"] and kinds.count("recovery") == 1
            details = {"actions": actions, "outbox_kinds": kinds}
        elif item.case_number == 83:
            engine.evaluate(rule, observation("run-1", 39), now=now)
            engine.evaluate(rule, observation("run-2", 40), now=now + timedelta(minutes=1))
            sender_calls: list[str] = []

            async def sender(outbox_item: Any) -> None:
                sender_calls.append(outbox_item.outbox_id)

            def drive(coroutine: Any) -> Any:
                try:
                    coroutine.send(None)
                except StopIteration as completed:
                    return completed.value
                finally:
                    coroutine.close()
                raise RuntimeError("alert kill switch unexpectedly awaited sender")

            disabled = drive(outbox.deliver(sender, send_enabled=False, dry_run=False, now=now))
            dry_run = drive(outbox.deliver(sender, send_enabled=True, dry_run=True, now=now))
            passed = bool(
                disabled.sent == dry_run.sent == 0
                and disabled.reason == "send_disabled"
                and dry_run.reason == "dry_run"
                and sender_calls == []
                and len(outbox.pending()) == 1
            )
            details = {"sender_calls": len(sender_calls), "disabled_reason": disabled.reason, "dry_run_reason": dry_run.reason}
        elif item.case_number == 84:
            unavailable = engine.evaluate(rule, observation("failed-run", 999, available=False), now=now)
            next_valid = engine.evaluate(rule, observation("run-1", 39), now=now + timedelta(minutes=1))
            passed = unavailable.action == "data_unavailable" and next_valid.action == "pending_trigger" and outbox.pending() == []
            details = {"unavailable_action": unavailable.action, "next_valid_action": next_valid.action}
        else:
            raise KeyError(f"Unsupported alert core case: {item.case_number}")
        pending_count = len(outbox.pending())
        del engine, outbox, subscriptions
        gc.collect()
    return passed, {
        "executor": "alert_engine",
        "case_number": item.case_number,
        "pending_outbox_items": pending_count,
        "real_sends": 0,
        **details,
    }


def _execute_briefing_risk_order(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.models import (
        AggregatedForecast,
        ForecastPoint,
        ForecastSummary,
        ForecastWindow,
        ProviderForecast,
        TimeInfo,
        WeatherSubmission,
    )
    from services.weather_bot.power_briefing import build_briefing_card

    tomorrow = today + timedelta(days=1)

    def submission(target: date, hot_hours: frozenset[int]) -> WeatherSubmission:
        points = [
            ForecastPoint(
                time=f"{target.isoformat()}T{hour:02d}:00:00+08:00",
                temperature=37.0 if hour in hot_hours else 25.0,
                apparent_temperature=39.0 if hour in hot_hours else 26.0,
                precipitation_probability=20.0,
                cloud_cover=50.0,
                wind_speed=4.0,
            )
            for hour in range(24)
        ]
        retrieved_at = f"{today.isoformat()}T08:00:00+08:00"
        return WeatherSubmission(
            task_id=f"risk-order-{target.isoformat()}-{len(hot_hours)}",
            region="离线代表点",
            target_date=target.isoformat(),
            data_cutoff_time=retrieved_at,
            time_info=TimeInfo(
                retrieved_at=retrieved_at,
                provider_issued_at={"offline_test": f"{today.isoformat()}T07:00:00+08:00"},
                aggregation_completed_at=f"{today.isoformat()}T08:00:01+08:00",
                valid_time=ForecastWindow(
                    start=f"{target.isoformat()}T00:00:00+08:00",
                    end=f"{target.isoformat()}T23:00:00+08:00",
                    timezone="Asia/Shanghai",
                ),
                forecast_run_id=f"risk-order-{target.isoformat()}-{len(hot_hours)}",
            ),
            provider_results=[
                ProviderForecast(
                    provider="offline_test",
                    status="ok",
                    points=points,
                    retrieved_at=retrieved_at,
                    provider_issued_at=f"{today.isoformat()}T07:00:00+08:00",
                    source_url="https://example.test/weather",
                    content_sha256="c" * 64,
                )
            ],
            aggregated_forecast=AggregatedForecast(
                providers_used=["offline_test"],
                points=points,
                summary=ForecastSummary(
                    max_temperature=max(point.temperature or 0 for point in points),
                    min_temperature=25.0,
                    rain_probability=20.0,
                    wind_speed=4.0,
                    cloud_cover=50.0,
                    main_weather="晴到多云",
                    high_risk_period="17:00–23:00" if hot_hours else "无明显风险",
                ),
            ),
            confidence={"score": 0.8, "description": "离线确定性夹具"},
            key_factors=[],
            risk_notes=[],
        )

    baseline = submission(today, frozenset())
    rows = [
        {
            "market_id": "weaker",
            "market": "较弱同级分析区",
            "province": "甲省",
            "point_id": "weak-point",
            "city": "较弱点",
            "roles": ["load"],
            "submissions": {
                today.isoformat(): baseline,
                tomorrow.isoformat(): submission(tomorrow, frozenset({17, 18})),
            },
        },
        {
            "market_id": "stronger",
            "market": "较强同级分析区",
            "province": "乙省",
            "point_id": "strong-point",
            "city": "较强点",
            "roles": ["load"],
            "submissions": {
                today.isoformat(): baseline,
                tomorrow.isoformat(): submission(
                    tomorrow,
                    frozenset({17, 18, 19, 20, 21, 22}),
                ),
            },
        },
    ]
    card = build_briefing_card(rows, today.isoformat())
    top_section = ""
    for element in (card.get("card") or {}).get("elements") or []:
        text = element.get("text") if isinstance(element, dict) else None
        content = str(text.get("content") or "") if isinstance(text, dict) else ""
        if "Top 5 气象侧风险" in content:
            top_section = content
            break
    stronger_index = top_section.find("较强同级分析区")
    weaker_index = top_section.find("较弱同级分析区")
    passed = 0 <= stronger_index < weaker_index
    return passed, {
        "executor": "briefing_risk_order",
        "same_severity": True,
        "stronger_rank": stronger_index,
        "weaker_rank": weaker_index,
        "external_calls": 0,
        "feishu_sends": 0,
    }


def _execute_data_availability_gate(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.data_provenance import (
        DataAvailabilityGate,
        ExternalDataRecord,
    )

    shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.combine(today, datetime.min.time(), tzinfo=shanghai).replace(hour=8)
    base = ExternalDataRecord(
        source_id="offline_structured_source",
        source_kind="structured_api",
        source_url="https://official.example.test/api/weather",
        retrieved_at=now - timedelta(minutes=5),
        provider_issued_at=now - timedelta(hours=1),
        valid_time=f"{today.isoformat()}T00:00:00+08:00/{today.isoformat()}T23:00:00+08:00",
        unit="temperature:C;wind_speed:m/s",
        granularity="hourly",
        coverage="24/24 hours",
        timezone="Asia/Shanghai",
        license_status="verified",
        allowed_uses={"calculation", "text_reference", "derived_storage"},
        completeness=1.0,
        quality_status="good",
        fresh_until=now + timedelta(hours=1),
        content_sha256="a" * 64,
        structured_values=True,
        retention_policy="derived_only",
    )
    if item.case_number == 89:
        record = base.model_copy(update={"completeness": 0.8, "coverage": "19/24 hours"})
        expected_status, expected_reason = "rejected", "insufficient_completeness"
    elif item.case_number == 91:
        record = base.model_copy(
            update={
                "source_kind": "search_discovery",
                "source_url": "https://search.example.test/result",
                "original_source_url": None,
                "allowed_uses": {"text_reference"},
                "structured_values": False,
            }
        )
        expected_status, expected_reason = "rejected", "search_without_original_source"
    elif item.case_number == 92:
        record = base.model_copy(
            update={
                "source_kind": "search_discovery",
                "source_url": "https://search.example.test/result",
                "original_source_url": "https://official.example.test/warning/123",
                "allowed_uses": {"text_reference"},
                "structured_values": False,
            }
        )
        expected_status, expected_reason = "text_only", "search_discovery_only"
    elif item.case_number == 93:
        record = base.model_copy(
            update={
                "source_kind": "cached_snapshot",
                "retrieved_at": now - timedelta(hours=4),
                "fresh_until": now - timedelta(seconds=1),
            }
        )
        expected_status, expected_reason = "rejected", "stale"
    elif item.case_number == 94:
        record = base.model_copy(
            update={
                "source_url": None,
                "valid_time": None,
                "unit": None,
            }
        )
        expected_status, expected_reason = "rejected", "missing_required_metadata"
    else:
        raise KeyError(f"Unsupported data availability core case: {item.case_number}")
    decision = DataAvailabilityGate().evaluate(record, now=now)
    passed = decision.status == expected_status and decision.reason == expected_reason
    if item.case_number == 94:
        passed = passed and {"source_url", "valid_time", "unit"}.issubset(decision.missing_fields)
    return passed, {
        "executor": "data_availability_gate",
        "status": decision.status,
        "reason": decision.reason,
        "missing_fields": list(decision.missing_fields),
        "stale": decision.stale,
        "calculation_admitted": decision.status == "allowed_for_calculation",
    }


def _execute_source_retention_policy(
    item: CoreReplayItem,
    *,
    today: date,
) -> tuple[bool, dict[str, Any]]:
    from services.weather_bot.data_provenance import DataAvailabilityGate, ExternalDataRecord
    from services.weather_bot.source_registry import SourcePolicy

    raw_policy_rejected = False
    try:
        SourcePolicy(
            provider="offline_test",
            environment="test",
            profile="forbidden-raw",
            allowed_uses={"raw_storage"},
        )
    except ValueError:
        raw_policy_rejected = True

    policy = SourcePolicy(
        provider="offline_test",
        environment="test",
        profile="derived-only",
        license_status="verified",
        allowed_uses={"calculation", "derived_storage"},
        terms_version="offline-contract-v1",
        source_url_prefixes=("https://official.example.test/",),
        unit_manifest="temperature:C",
        required_metrics=("temperature",),
        coverage_model="representative_point",
        timezone="Asia/Shanghai",
        max_age_seconds=3600,
        retention_policy="derived_only",
        retention_seconds=86400,
    )
    shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
    now = datetime.combine(today, datetime.min.time(), tzinfo=shanghai).replace(hour=8)
    record = ExternalDataRecord(
        source_id=policy.provider,
        source_kind="structured_api",
        source_url="https://official.example.test/weather",
        retrieved_at=now,
        valid_time=today.isoformat(),
        unit=policy.unit_manifest,
        granularity="daily",
        coverage=policy.coverage_model,
        timezone=policy.timezone,
        license_status=policy.license_status,
        allowed_uses=policy.allowed_uses,
        completeness=1.0,
        quality_status="good",
        fresh_until=now + timedelta(seconds=policy.max_age_seconds or 0),
        content_sha256="b" * 64,
        structured_values=True,
        retention_policy=policy.retention_policy,
    )
    decision = DataAvailabilityGate().evaluate(record, now=now)
    passed = bool(
        raw_policy_rejected
        and decision.status == "allowed_for_calculation"
        and decision.derived_storage_allowed
        and not decision.raw_storage_allowed
        and policy.retention_policy == "derived_only"
    )
    return passed, {
        "executor": "source_retention_policy",
        "raw_policy_rejected": raw_policy_rejected,
        "retention_policy": policy.retention_policy,
        "derived_storage_allowed": decision.derived_storage_allowed,
        "raw_storage_allowed": decision.raw_storage_allowed,
        "raw_payload_persisted": False,
    }


__all__ = [
    "CoreReplayItem",
    "ManifestStatus",
    "OutcomeStatus",
    "core_replay_manifest",
    "run_core_replay_gate",
]
