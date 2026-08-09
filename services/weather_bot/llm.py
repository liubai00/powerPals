from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.decision_boundary import (
    contains_unsafe_weather_only_claim,
    enforce_weather_only_llm_answer,
    weather_only_boundary_answer,
)
from services.weather_bot.models import WeatherSubmission


logger = logging.getLogger(__name__)


class LlmClient:
    def __init__(
        self,
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 20.0,
        egress_allowed: bool = True,
    ):
        self.api_base_url = api_base_url.rstrip("/") if api_base_url else None
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.egress_allowed = egress_allowed

    @classmethod
    def from_settings(cls, settings: Settings) -> "LlmClient":
        allowed_prefixes = _parse_prefix_allowlist(
            settings.llm_allowed_https_prefixes_json
        )
        chat_url = _chat_completions_url(settings.llm_api_base_url)
        return cls(
            api_base_url=settings.llm_api_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
            egress_allowed=(
                settings.llm_egress_enabled
                and not settings.dry_run
                and settings.llm_model == "gpt-5.6-sol"
                and bool(allowed_prefixes)
                and _matches_allowed_https_prefix(chat_url, allowed_prefixes)
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.egress_allowed and self.api_base_url and self.api_key and self.model)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = 0.2,
        max_tokens: int | None = 600,
    ) -> str | None:
        if not self.enabled:
            return None

        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        body = await self._post_chat_completion(payload)
        if body is None and ("temperature" in payload or "max_tokens" in payload):
            payload = {"model": self.model, "messages": messages}
            body = await self._post_chat_completion(payload)
        if body is None:
            return None

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return content.strip() if isinstance(content, str) and content.strip() else None

    async def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self._chat_completions_url(),
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if response.status_code >= 400:
                logger.warning("LLM chat HTTP %s", response.status_code)
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("LLM chat failed error_type=%s", type(exc).__name__)
            return None

    def _chat_completions_url(self) -> str:
        assert self.api_base_url is not None
        return _chat_completions_url(self.api_base_url)


def _chat_completions_url(api_base_url: str | None) -> str:
    if not api_base_url:
        return ""
    api_base_url = api_base_url.rstrip("/")
    if api_base_url.endswith("/chat/completions"):
        return api_base_url
    if api_base_url.endswith("/v1"):
        return f"{api_base_url}/chat/completions"
    return f"{api_base_url}/v1/chat/completions"


def _matches_allowed_https_prefix(url: str, allowed_prefixes: tuple[str, ...]) -> bool:
    try:
        target = urlsplit(url)
        target_port = target.port
    except ValueError:
        return False
    if not _is_safe_https_url(target):
        return False
    for prefix in allowed_prefixes:
        try:
            allowed = urlsplit(prefix)
            allowed_port = allowed.port
        except ValueError:
            continue
        if not _is_safe_https_url(allowed):
            continue
        if allowed.hostname != target.hostname or allowed_port != target_port:
            continue
        allowed_path = allowed.path.rstrip("/")
        if (
            not allowed_path
            or target.path == allowed_path
            or target.path.startswith(f"{allowed_path}/")
        ):
            return True
    return False


def _is_safe_https_url(parts) -> bool:
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.query or parts.fragment or "\\" in parts.path:
        return False
    decoded_path = unquote(parts.path)
    if decoded_path != parts.path or "\\" in decoded_path:
        return False
    return not any(segment in {".", ".."} for segment in decoded_path.split("/"))


def _parse_prefix_allowlist(raw: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list) or not parsed:
        return ()
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in parsed
    ):
        return ()
    for item in parsed:
        try:
            parts = urlsplit(item)
            parts.port
        except ValueError:
            return ()
        if not _is_safe_https_url(parts):
            return ()
    return tuple(parsed)


async def extract_location_with_llm(
    llm_client: "LlmClient | None",
    user_text: str,
    *,
    timeout: float = 8.0,
) -> str | None:
    """Best-effort: pull a Chinese place name out of free-form text via the LLM.

    Bounded by a short timeout so a slow/unreachable LLM degrades to clarification
    instead of blocking the reply. Returns None when nothing usable is found.
    """
    if llm_client is None or not llm_client.enabled:
        return None
    try:
        content = await asyncio.wait_for(
            llm_client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是地点识别器。从用户消息里找出他想查询天气的中国地点"
                            "（城市/区县/乡镇/地标都可以）。只输出该地点的中文名称，"
                            "尽量补全到区县级，例如「河南省上蔡县」。如果找不到地点，只输出「无」。"
                            "不要解释，不要标点，不要输出其它任何字。"
                        ),
                    },
                    {"role": "user", "content": user_text},
                ],
                temperature=0.0,
                max_tokens=24,
            ),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - location extraction is best-effort
        return None
    if not content:
        return None
    candidate = content.strip().strip("。.，,、；;：: \t\r\n\"'「」『』（）()")
    if not candidate or candidate in {"无", "没有", "未知", "none", "None", "N/A", "NA"}:
        return None
    if len(candidate) > 20 or any(bad in candidate for bad in ("无法", "抱歉", "没有", "不知道")):
        return None
    return candidate


async def _bounded_chat(client, messages, *, temperature=0.2, max_tokens=300, timeout=9.0):
    """Call client.chat with a hard wall-clock bound so the weather card is never
    held hostage by a slow LLM; returns None on timeout/any error (-> rule-based fallback)."""
    try:
        return await asyncio.wait_for(
            client.chat(messages, temperature=temperature, max_tokens=max_tokens),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - explanation is best-effort
        return None


async def explain_weather_with_llm(
    llm_client: LlmClient | None,
    submission: WeatherSubmission,
) -> dict[str, list[str]] | None:
    if llm_client is None or not llm_client.enabled:
        return None

    summary = submission.aggregated_forecast.summary
    compact_payload = {
        "region": submission.region,
        "target_date": submission.target_date,
        "providers_used": submission.aggregated_forecast.providers_used,
        "summary": summary.model_dump(mode="json"),
        "confidence": submission.confidence,
    }
    content = await _bounded_chat(llm_client,
        [
            {
                "role": "system",
                "content": (
                    "你是云云，PowerPals 小可爱电力社区的气象预测小助手。请只依据用户提供的预测数据做解读，"
                    "语言专业、简洁、口语化，绝不编造任何新的天气数值。只返回 JSON，格式为："
                    '{"key_factors":["因素1","因素2"],"risk_notes":["风险1","风险2"]}。'
                ),
            },
            {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=False)},
        ],
        temperature=0.2,
        max_tokens=300,
    )
    body = _parse_json_object(content)
    if not body:
        return None
    key_factors = _string_list(body.get("key_factors"))
    risk_notes = _string_list(body.get("risk_notes"))
    if not key_factors or not risk_notes:
        return None
    if contains_unsafe_weather_only_claim("\n".join([*key_factors, *risk_notes])):
        return None
    return {"key_factors": key_factors[:5], "risk_notes": risk_notes[:5]}


async def answer_role_question(
    llm_client: LlmClient | None,
    *,
    bot_role: str,
    user_text: str,
    fallback: str,
    history: list[dict] | None = None,
) -> str:
    if bot_role == "weather_forecast_bot":
        boundary_answer = weather_only_boundary_answer(user_text)
        if boundary_answer:
            return boundary_answer
    if llm_client is None or not llm_client.enabled:
        return fallback

    if bot_role == "weather_forecast_bot":
        role_prompt = (
            "你是云云，PowerPals 小可爱电力社区的气象预测小助手，性格友好、专业、热情。"
            "你擅长全国城市/区县/经纬度的天气预测、多日趋势、逐小时变化、数据源说明和飞书卡片解读。"
            "本地系统没有自有气象或电力事实库。被问到数据源时，只能说明当前响应中实际通过来源许可、"
            "端点、时效、完整性和来源元数据门禁的来源；本轮没有提供可回溯来源证据时，必须说当前无法确认。"
            "不要固定宣称 Open-Meteo、和风天气、彩云天气或任何地理编码服务已经被调用；配置了适配器或 API Key 不等于本次可用。"
            "你只有预报数据（今天起未来 1-16 天），没有历史/实况数据；被问过去的天气（上周/上个月/历史）时如实说明查不了，"
            "不要编造历史数值，可引导用户改问未来时段或查气象局官网。"
            "本地系统不提供自有气象、负荷、出力、机组、联络线、价格、持仓或用户资产数据；"
            "所有可陈述的业务事实必须来自当前请求中可回溯的第三方接口或官方公开来源，缺失时必须明确说没有可靠数据。"
            "你服务的是电力交易社区，成员多为电力交易/售电/新能源从业者。"
            "当问题涉及电力（负荷、风电/光伏出力、现货、电价、检修、交易）时，主动用『气象→电力』传导逻辑分析："
            "温度/体感→负荷天气压力代理；风速→风资源代理；云量/降水→光资源代理；"
            "强对流/大风/寒潮→气象侧电网风险。必须把这些结果明确称为气象侧代理，不能写成实际负荷、实际出力或真实供需。"
            "仅有天气数据时，不得判断电价方向，不得给出报价、申报、仓位、买卖、做多做空或具体 MW/MWh 数值；"
            "应说明还需核对负荷与新能源预测、机组可用率、联络线、价格、报价、持仓和市场规则。"
            "对话历史中以[天气卡片]开头的内容是你刚发给用户的真实预报数据；用户接着问适不适合出行、聚餐、晾晒、"
            "施工、检修等安排时，直接引用这些数据给出具体建议（含温度/降水/风等数字），不要说你查不到。"
            "对话历史可能来自群里多位成员；用户说『回答下/上面的问题/刚才那个』时，指的是历史里最近尚未回应的问题，"
            "请直接回应那个问题本身，不要自我介绍或岔开话题。"
            "你不负责发布气象共测任务；遇到任务发布、提醒、关闭、记录，请友好地引导用户去艾特任务小助手「点点」。"
            "【回答风格·重要】直接点名回应用户问的具体对象（台风名/城市/情景，如\"巴威\"），不要泛化成\"超强台风\"这类笼统说法；"
            "先给结论、再列 3-5 条要点，全文控制在 300 字内、适合飞书群聊快读，不要写分很多大节的长篇论文；"
            "不能核验的官方历史数据一句话带过即可、别当开头重点。可适当用天气 emoji，但不要啰嗦。"
            "【格式·适配飞书】小标题用 **加粗**，不要用 # / ## 这类 Markdown 标题，也不要用表格；"
            "要点用 1. 或 - 逐条列、每条一行；结论与要点之间空一行，保持清爽。"
        )
    elif bot_role == "weather_task_bot":
        role_prompt = (
            "你是点点，PowerPals 小可爱电力社区的气象任务小助手，性格利落、负责。"
            "你负责发布气象共测任务、统一提交口径、提醒提交、关闭窗口、记录状态。"
            "你不查询或计算天气；遇到天气预测、经纬度天气、多日趋势，请友好地引导用户去艾特气象小助手「云云」。"
            "回答用简洁的中文，适合飞书群聊。"
        )
    else:
        role_prompt = (
            "你是 PowerPals 小可爱电力社区的飞书助手。请帮用户区分气象小助手「云云」和"
            "任务小助手「点点」，用简洁、友好的中文回答。"
        )

    messages: list[dict] = [{"role": "system", "content": role_prompt}]
    for turn in (history or [])[-6:]:
        turn_role = turn.get("role")
        turn_text = turn.get("content")
        if turn_role in ("user", "assistant") and isinstance(turn_text, str) and turn_text.strip():
            messages.append({"role": turn_role, "content": turn_text})
    messages.append({"role": "user", "content": user_text})
    content = await llm_client.chat(
        messages,
        temperature=0.3,
        max_tokens=420,
    )
    if bot_role == "weather_forecast_bot":
        return enforce_weather_only_llm_answer(content, fallback=fallback)
    return content or fallback


async def answer_weather_knowledge_question(
    llm_client: LlmClient | None,
    *,
    user_text: str,
    fallback: str,
    search_results: list[dict[str, str]] | None = None,
    live_context: str | None = None,
) -> str:
    boundary_answer = weather_only_boundary_answer(user_text)
    if boundary_answer:
        return boundary_answer
    if llm_client is None or not llm_client.enabled:
        return fallback

    context = ""
    if search_results:
        context = json.dumps(search_results[:4], ensure_ascii=False)
    content = await llm_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是云云，PowerPals 社区的气象预测小助手。用户现在问的是气象知识、数据来源、"
                    "更新时间、预测不确定性、术语解释或使用方式，而不是要某个城市的预测卡片。"
                    "【你自己的事实——被问到你的来源/数据/能力时，必须按下面事实正面回答，不要泛泛科普官方机构】"
                    "本地系统没有自有气象或电力事实库；系统可以适配城市/区县/经纬度查询、未来预报和逐小时粒度，"
                    "但配置能力不代表本次已经取得数据。被问到来源时，只能引用当前响应中实际通过来源许可、端点、"
                    "时效、完整性和来源元数据门禁的来源；没有随本轮提供可回溯来源证据时，必须说当前无法确认。"
                    "不要固定宣称 Open-Meteo、和风天气、彩云天气或任何地理编码服务已经被调用。"
                    "你没有历史/实况数据，被问过去的天气时如实说明查不了、不要编造，可引导改问未来时段。"
                    "本地系统不提供自有气象、负荷、出力、机组、联络线、价格、持仓或用户资产数据；"
                    "所有业务事实必须来自当前请求中可回溯的第三方接口或官方公开来源，搜索摘要不能替代原始事实来源。"
                    "你服务的是电力交易社区；当问题涉及电力（负荷、风电/光伏出力、现货、电价、交易），"
                    "只能解释气象侧代理（温度→负荷天气压力，风速→风资源，云量/降水→光资源，强对流→气象侧电网风险），"
                    "不能把代理写成实际负荷、实际出力或真实供需；不得判断电价方向，不得给出报价、仓位、交易指令或具体 MW/MWh 数值。"
                    "缺少可核验外部数据时，应明确说没有可靠数据，并列出仍需核对的负荷、新能源、机组、联络线、价格和市场规则。"
                    "【实时数据优先·最重要】如果下方 live_typhoon_data 有内容，它来自权威气象接口、是最新实时事实，"
                    "你必须完全以它为准说明台风的当前位置、强度、移动方向和预报路径；不要使用你训练记忆里的旧台风信息，"
                    "也不要把用户举例中往年历史台风（如某年某台风）的数字当成当前情况；先播报台风最新实况(位置/强度/路径)，再谈电力影响。"
                    "【回答风格·重要】直接点名回应用户提到的具体台风/城市/情景（不要泛化成\"超强台风\"）；"
                    "先给结论再列 3-5 条要点，全文控制在 300 字内、适合飞书群聊快读，不要写分很多大节的长篇论文；"
                    "不能核验的官方历史数据用一句话带过、不要作为开头重点。"
                    "【格式·适配飞书】小标题用 **加粗**，不要用 # / ## 这类 Markdown 标题，也不要用表格；要点用 1. 或 - 逐条列、每条一行；结论与要点间空一行。"
                    "不要编造实时天气数值；如果需要具体城市预测，再提示用户提供城市或经纬度。"
                    "搜索上下文只能用于发现待核验的原始来源入口，不能把摘要或转载当成事实；未取得并核验原始来源时要明确说明。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"question": user_text, "search_context": context, "live_typhoon_data": live_context or ""},
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.3,
        max_tokens=420,
    )
    return enforce_weather_only_llm_answer(content, fallback=fallback)


def _parse_json_object(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
