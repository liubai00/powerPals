from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.models import WeatherSubmission


logger = logging.getLogger(__name__)


class LlmClient:
    def __init__(
        self,
        api_base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 20.0,
    ):
        self.api_base_url = api_base_url.rstrip("/") if api_base_url else None
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> "LlmClient":
        return cls(
            api_base_url=settings.llm_api_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_base_url and self.api_key and self.model)

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
                logger.warning("LLM chat HTTP %s: %s", response.status_code, response.text[:500])
                return None
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("LLM chat failed: %s", exc)
            return None

    def _chat_completions_url(self) -> str:
        assert self.api_base_url is not None
        if self.api_base_url.endswith("/chat/completions"):
            return self.api_base_url
        if self.api_base_url.endswith("/v1"):
            return f"{self.api_base_url}/chat/completions"
        return f"{self.api_base_url}/v1/chat/completions"


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
    return {"key_factors": key_factors[:5], "risk_notes": risk_notes[:5]}


async def answer_role_question(
    llm_client: LlmClient | None,
    *,
    bot_role: str,
    user_text: str,
    fallback: str,
    history: list[dict] | None = None,
) -> str:
    if llm_client is None or not llm_client.enabled:
        return fallback

    if bot_role == "weather_forecast_bot":
        role_prompt = (
            "你是云云，PowerPals 小可爱电力社区的气象预测小助手，性格友好、专业、热情。"
            "你擅长全国城市/区县/经纬度的天气预测、多日趋势、逐小时变化、数据源说明和飞书卡片解读。"
            "你的天气数据来自 Open-Meteo、和风天气（QWeather）、彩云天气（Caiyun）三个数据源加权融合，"
            "城市/区县中文名通过和风 Geocoding 解析；被问到数据源、数据来源、用的什么数据时，请如实这样说明，不要编造其它来源。"
            "你只有预报数据（今天起未来 1-16 天），没有历史/实况数据；被问过去的天气（上周/上个月/历史）时如实说明查不了，"
            "不要编造历史数值，可引导用户改问未来时段或查气象局官网。"
            "你服务的是电力交易社区，成员多为电力交易/售电/新能源从业者。"
            "当问题涉及电力（负荷、风电/光伏出力、现货、电价、检修、交易）时，主动用『气象→电力』传导逻辑分析："
            "温度/体感→制冷或取暖负荷；风速→风电出力（约3-25m/s为有效发电区间，风越大出力越强，超~25m/s切出）；"
            "云量/降水→光伏出力；强对流/大风/寒潮→电网与机组风险。"
            "可以给方向性的供需与价格影响判断，也可以探讨交易策略思路，但只基于气象数据定性推演，"
            "不编造具体电价或出力数字；涉及交易的回答结尾加一句：以上为气象侧推演，仅供参考，不构成投资建议。"
            "对话历史中以[天气卡片]开头的内容是你刚发给用户的真实预报数据；用户接着问适不适合出行、聚餐、晾晒、"
            "施工、检修等安排时，直接引用这些数据给出具体建议（含温度/降水/风等数字），不要说你查不到。"
            "你不负责发布气象共测任务；遇到任务发布、提醒、关闭、记录，请友好地引导用户去艾特任务小助手「点点」。"
            "回答用简洁的中文，适合飞书群聊，可以适当用天气相关 emoji，但不要啰嗦。"
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
        max_tokens=500,
    )
    return content or fallback


async def answer_weather_knowledge_question(
    llm_client: LlmClient | None,
    *,
    user_text: str,
    fallback: str,
    search_results: list[dict[str, str]] | None = None,
) -> str:
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
                    "你的预测数据来自 Open-Meteo 与 和风天气(QWeather) 两个数据源按指标加权融合（彩云天气接口已预留、暂未启用）；"
                    "城市/区县中文名通过和风 Geocoding 解析；支持全国城市/区县/经纬度查询、1-16 天预测、逐小时粒度，每次查询实时拉取。"
                    "你没有历史/实况数据，被问过去的天气时如实说明查不了、不要编造，可引导改问未来时段。"
                    "你服务的是电力交易社区；当问题涉及电力（负荷、风电/光伏出力、现货、电价、交易），"
                    "用『气象→电力』传导逻辑分析（温度→负荷，风速→风电出力，云量/降水→光伏出力，强对流→电网风险），"
                    "可给方向性判断与策略思路，但不编造具体电价或出力数字，结尾加：以上为气象侧推演，仅供参考，不构成投资建议。"
                    "请用简洁、友好的中文回答，适合飞书群聊；不要编造实时天气数值；如果需要具体城市预测，"
                    "再提示用户提供城市或经纬度。若提供了搜索上下文，可结合上下文回答。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"question": user_text, "search_context": context},
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.3,
        max_tokens=700,
    )
    return content or fallback


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
