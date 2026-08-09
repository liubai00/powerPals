from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Protocol

from services.weather_bot.aggregation import aggregate_provider_forecasts
from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning import ControlledLearningStore
from services.weather_bot.location import LocationResolver, apply_location, location_payload, location_slug
from services.weather_bot.llm import LlmClient
from services.weather_bot.models import (
    DataProfile,
    ExplanationProfile,
    ForecastRequest,
    ProviderForecast,
    ScopeProfile,
    ScoringProfile,
    TimeInfo,
    WeatherPayload,
    WeatherSubmission,
)
from services.weather_bot.openclaw import OpenClawExplainer
from services.weather_bot.providers import build_default_providers


SHANGHAI_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


class WeatherProvider(Protocol):
    name: str

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        ...


class ForecastService:
    def __init__(
        self,
        providers: dict[str, WeatherProvider] | None = None,
        explainer: OpenClawExplainer | None = None,
        location_resolver: LocationResolver | None = None,
        settings: Settings | None = None,
    ):
        explicit_settings = settings is not None
        self.settings = settings or Settings()
        self.providers = providers or build_default_providers(self.settings)
        llm_client = LlmClient.from_settings(self.settings) if explicit_settings else None
        self.explainer = explainer or OpenClawExplainer(
            self.settings.openclaw_api_url,
            self.settings.openclaw_api_key,
            llm_client=llm_client,
        )
        self.location_resolver = location_resolver or LocationResolver(self.settings)
        self.learning_store: ControlledLearningStore | None = None
        if self.settings.controlled_learning_enabled:
            try:
                self.learning_store = ControlledLearningStore(self.settings.controlled_learning_db)
            except Exception:  # noqa: BLE001 - learning must never break weather service startup
                logger.exception("controlled_learning_store_init_failed")

    async def forecast(self, request: ForecastRequest) -> WeatherSubmission:
        try:
            location = await self.location_resolver.resolve(request)
        except Exception as exc:
            self._record_learning_signal(
                "forecast_request_failed",
                "error",
                {
                    "stage": "location_resolution",
                    "region": request.region,
                    "target_date": request.target_date,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        request = apply_location(request, location)
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

        try:
            aggregated = aggregate_provider_forecasts(provider_results)
        except Exception as exc:
            self._record_learning_signal(
                "forecast_unusable",
                "error",
                {
                    "stage": "provider_aggregation",
                    "region": request.region,
                    "target_date": request.target_date,
                    "provider_status": {
                        result.provider: result.status for result in provider_results
                    },
                    "error_type": type(exc).__name__,
                },
            )
            raise
        submit_time = _submit_time(request.target_date)
        data_cutoff_time = _data_cutoff_time(request.target_date)
        forecast_start, forecast_end = _forecast_window(request.target_date)
        submission = WeatherSubmission(
            task_id=_task_id(request.target_date, location_slug(location)),
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time=data_cutoff_time,
            scope=ScopeProfile(
                region=request.region,
                target_date=request.target_date,
                time_granularity=request.granularity,
                location=location_payload(location),
            ),
            time_info=TimeInfo(
                submit_time=submit_time,
                data_cutoff_time=data_cutoff_time,
                forecast_start=forecast_start,
                forecast_end=forecast_end,
            ),
            data_profile=DataProfile(
                data_sources_summary=aggregated.providers_used,
                provider_status={result.provider: result.status for result in provider_results},
            ),
            payload=WeatherPayload(
                values=aggregated.points,
                summary=aggregated.summary.model_dump(mode="json"),
            ),
            provider_results=provider_results,
            aggregated_forecast=aggregated,
            confidence=_confidence(aggregated.providers_used, request.providers),
            key_factors=["多源气象预报融合"],
            risk_notes=["局地短时天气存在不确定性"],
            scoring_profile=ScoringProfile(),
        )
        explanation = await self.explainer.explain(submission)
        submission.key_factors = explanation["key_factors"]
        submission.risk_notes = explanation["risk_notes"]
        submission.explanation = ExplanationProfile(
            key_factors=submission.key_factors,
            risk_notes=submission.risk_notes,
            business_readable_summary=_business_summary(submission),
        )
        if self.learning_store is not None:
            try:
                self.learning_store.record_forecast_snapshot(submission)
            except Exception:  # noqa: BLE001 - evidence collection is best effort only
                logger.exception("controlled_learning_snapshot_failed")
        return submission

    def _record_learning_signal(
        self,
        signal_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        if self.learning_store is None:
            return
        try:
            self.learning_store.record_signal(
                signal_type,
                "forecast_service",
                severity,
                payload,
            )
        except Exception:  # noqa: BLE001 - evidence collection is best effort only
            logger.exception("controlled_learning_signal_failed signal_type=%s", signal_type)


def _task_id(target_date: str, location_token: str) -> str:
    return f"WEATHER-CN-{location_token}-{target_date.replace('-', '')}-DAYAHEAD-001"


def _data_cutoff_time(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    cutoff = datetime.combine(target - timedelta(days=1), time(hour=16), tzinfo=SHANGHAI_TZ)
    return cutoff.isoformat()


def _submit_time(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    submit = datetime.combine(target - timedelta(days=1), time(hour=16, minute=45), tzinfo=SHANGHAI_TZ)
    return submit.isoformat()


def _forecast_window(target_date: str) -> tuple[str, str]:
    target = date.fromisoformat(target_date)
    start = datetime.combine(target, time(hour=0), tzinfo=SHANGHAI_TZ)
    end = datetime.combine(target, time(hour=23), tzinfo=SHANGHAI_TZ)
    return start.isoformat(), end.isoformat()


def _confidence(providers_used: list[str], requested_providers: list[str]) -> dict[str, float | str]:
    if not requested_providers:
        score = 0.0
    else:
        score = round(min(0.9, 0.45 + 0.15 * len(providers_used)), 2)
    description = "中等" if score >= 0.65 else "偏低"
    if len(providers_used) < len(requested_providers):
        description += "；部分数据源不可用"
    return {"score": score, "description": description}


def _business_summary(submission: WeatherSubmission) -> str:
    summary = submission.aggregated_forecast.summary
    return (
        f"{submission.target_date} {submission.region}气象预测用于社区共测和复盘。"
        f"预计主要天气为{summary.main_weather}，最高温 {summary.max_temperature}℃，"
        f"最低温 {summary.min_temperature}℃，降水概率 {summary.rain_probability}%。"
        "该结果可作为负荷、新能源出力、电价复盘和储能运行观察的参考输入，"
        "不构成交易建议、报价建议、投资建议或收益承诺。"
    )
