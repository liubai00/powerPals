from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

from services.weather_bot.aggregation import aggregate_provider_forecasts
from services.weather_bot.models import ForecastRequest, ProviderForecast, WeatherSubmission
from services.weather_bot.openclaw import OpenClawExplainer
from services.weather_bot.providers import build_default_providers


SHANGHAI_TZ = timezone(timedelta(hours=8))


class WeatherProvider(Protocol):
    name: str

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        ...


class ForecastService:
    def __init__(
        self,
        providers: dict[str, WeatherProvider] | None = None,
        explainer: OpenClawExplainer | None = None,
    ):
        self.providers = providers or build_default_providers()
        self.explainer = explainer or OpenClawExplainer()

    async def forecast(self, request: ForecastRequest) -> WeatherSubmission:
        provider_results = []
        for provider_name in request.providers:
            provider = self.providers.get(provider_name)
            if provider is None:
                provider_results.append(
                    ProviderForecast(
                        provider=provider_name,
                        status="disabled",
                        points=[],
                        error_message="Provider is not configured",
                    )
                )
                continue

            try:
                provider_results.append(await provider.fetch(request))
            except Exception as exc:  # noqa: BLE001 - provider errors should not stop aggregation
                provider_results.append(
                    ProviderForecast(provider=provider_name, status="error", points=[], error_message=str(exc))
                )

        aggregated = aggregate_provider_forecasts(provider_results)
        submission = WeatherSubmission(
            task_id=_task_id(request.target_date),
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time=_data_cutoff_time(request.target_date),
            provider_results=provider_results,
            aggregated_forecast=aggregated,
            confidence=_confidence(aggregated.providers_used, request.providers),
            key_factors=["多源气象预报融合"],
            risk_notes=["局地短时天气存在不确定性"],
        )
        explanation = await self.explainer.explain(submission)
        submission.key_factors = explanation["key_factors"]
        submission.risk_notes = explanation["risk_notes"]
        return submission


def _task_id(target_date: str) -> str:
    return f"WEATHER-SZ-{target_date.replace('-', '')}-DAYAHEAD-001"


def _data_cutoff_time(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    cutoff = datetime.combine(target - timedelta(days=1), time(hour=16), tzinfo=SHANGHAI_TZ)
    return cutoff.isoformat()


def _confidence(providers_used: list[str], requested_providers: list[str]) -> dict[str, float | str]:
    if not requested_providers:
        score = 0.0
    else:
        score = round(min(0.9, 0.45 + 0.15 * len(providers_used)), 2)
    description = "中等" if score >= 0.65 else "偏低"
    if len(providers_used) < len(requested_providers):
        description += "；部分数据源不可用"
    return {"score": score, "description": description}
