from __future__ import annotations

import httpx

from services.weather_bot.llm import LlmClient, explain_weather_with_llm
from services.weather_bot.models import WeatherSubmission


class OpenClawExplainer:
    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        llm_client: LlmClient | None = None,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self.llm_client = llm_client

    async def explain(self, submission: WeatherSubmission) -> dict[str, list[str]]:
        if not self.api_url:
            return await self._fallback_explanation(submission)

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "task": "explain_powerpals_weather_forecast",
            "submission": submission.model_dump(mode="json"),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.api_url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError:
            return await self._fallback_explanation(submission)

        fallback = await self._fallback_explanation(submission)
        return {
            "key_factors": body.get("key_factors") or fallback["key_factors"],
            "risk_notes": body.get("risk_notes") or fallback["risk_notes"],
        }

    async def _fallback_explanation(self, submission: WeatherSubmission) -> dict[str, list[str]]:
        llm_explanation = await explain_weather_with_llm(self.llm_client, submission)
        if llm_explanation:
            return llm_explanation
        return _deterministic_explanation(submission)


def _deterministic_explanation(submission: WeatherSubmission) -> dict[str, list[str]]:
    summary = submission.aggregated_forecast.summary
    factors = [
        "多源气象预报融合",
        f"{submission.region}局地天气变化",
        "逐小时温度、降水、风速和云量综合判断",
    ]
    risks = ["局地短时强降水、风速和云量变化可能导致误差放大"]
    if summary.rain_probability is not None and summary.rain_probability >= 50:
        risks.append("降水概率偏高，建议复盘实际降水发生时段")
    if summary.wind_speed is not None and summary.wind_speed >= 8:
        risks.append("风速偏高时需关注新能源出力和用电侧扰动")
    return {"key_factors": factors, "risk_notes": risks}
