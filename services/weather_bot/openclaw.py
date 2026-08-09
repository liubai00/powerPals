from __future__ import annotations

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.decision_boundary import contains_unsafe_weather_only_claim
from services.weather_bot.llm import (
    LlmClient,
    _matches_allowed_https_prefix,
    _parse_prefix_allowlist,
    explain_weather_with_llm,
)
from services.weather_bot.models import WeatherSubmission


class OpenClawExplainer:
    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        llm_client: LlmClient | None = None,
        egress_allowed: bool = True,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self.llm_client = llm_client
        self.egress_allowed = egress_allowed

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        llm_client: LlmClient | None = None,
    ) -> "OpenClawExplainer":
        allowed_prefixes = _parse_prefix_allowlist(
            settings.openclaw_allowed_https_prefixes_json
        )
        return cls(
            settings.openclaw_api_url,
            settings.openclaw_api_key,
            llm_client=llm_client,
            egress_allowed=(
                settings.openclaw_egress_enabled
                and not settings.dry_run
                and bool(allowed_prefixes)
                and _matches_allowed_https_prefix(
                    settings.openclaw_api_url or "",
                    allowed_prefixes,
                )
            ),
        )

    async def explain(self, submission: WeatherSubmission) -> dict[str, list[str]]:
        if not self.api_url or not self.egress_allowed:
            return await self._fallback_explanation(submission)

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "task": "explain_powerpals_weather_forecast",
            "submission": _minimal_explanation_payload(submission),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.api_url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError:
            return await self._fallback_explanation(submission)

        fallback = await self._fallback_explanation(submission)
        key_factors = _safe_string_list(body.get("key_factors"))
        risk_notes = _safe_string_list(body.get("risk_notes"))
        if not key_factors or not risk_notes:
            return fallback
        if contains_unsafe_weather_only_claim("\n".join([*key_factors, *risk_notes])):
            return fallback
        return {"key_factors": key_factors[:5], "risk_notes": risk_notes[:5]}

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
        risks.append("10米地面风偏高时需关注风资源代理变化，并结合实际新能源数据核查")
    return {"key_factors": factors, "risk_notes": risks}


def _safe_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _minimal_explanation_payload(submission: WeatherSubmission) -> dict:
    """Expose only derived summary and provenance, never provider point series or raw payloads."""

    return {
        "task_id": submission.task_id,
        "region": submission.region,
        "target_date": submission.target_date,
        "forecast_run_id": submission.time_info.forecast_run_id,
        "valid_time": submission.time_info.valid_time.model_dump(mode="json"),
        "providers_used": list(submission.aggregated_forecast.providers_used),
        "summary": submission.aggregated_forecast.summary.model_dump(mode="json"),
        "confidence": dict(submission.confidence),
        "provenance": [
            {
                "provider": result.provider,
                "retrieved_at": result.retrieved_at,
                "provider_issued_at": result.provider_issued_at,
                "source_url": result.source_url,
                "content_sha256": result.content_sha256,
                "retention_policy": result.retention_policy,
            }
            for result in submission.provider_results
            if result.provider in submission.aggregated_forecast.providers_used
        ],
    }
