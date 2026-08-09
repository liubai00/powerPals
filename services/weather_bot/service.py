from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Protocol
from uuid import uuid4

from services.weather_bot.aggregation import aggregate_provider_forecasts
from services.weather_bot.config import Settings
from services.weather_bot.controlled_learning import ControlledLearningStore
from services.weather_bot.data_provenance import DataAvailabilityGate, external_record_from_provider_forecast
from services.weather_bot.location import LocationResolver, apply_location, location_payload, location_slug
from services.weather_bot.llm import LlmClient
from services.weather_bot.models import (
    DataProfile,
    ExplanationProfile,
    ForecastWindow,
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
from services.weather_bot.source_registry import (
    SourcePolicy,
    SourceRegistry,
    same_source_endpoint,
)


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
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        source_registry: SourceRegistry | None = None,
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
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run_id_factory = run_id_factory or (lambda: str(uuid4()))
        if source_registry is not None and source_registry.environment == self.settings.app_env:
            self.source_registry = source_registry
        elif source_registry is not None:
            logger.warning(
                "source_registry_environment_mismatch registry=%s service=%s",
                source_registry.environment,
                self.settings.app_env,
            )
            self.source_registry = SourceRegistry(environment=self.settings.app_env)
        else:
            self.source_registry = SourceRegistry.from_json(
                self.settings.weather_source_policies_json,
                environment=self.settings.app_env,
            )
        self.location_resolver = location_resolver or LocationResolver(
            self.settings,
            source_registry=self.source_registry,
            clock=self.clock,
        )
        self.learning_store: ControlledLearningStore | None = None
        if self.settings.controlled_learning_enabled:
            try:
                self.learning_store = ControlledLearningStore(self.settings.controlled_learning_db)
            except Exception:  # noqa: BLE001 - learning must never break weather service startup
                logger.exception("controlled_learning_store_init_failed")

    async def forecast(self, request: ForecastRequest) -> WeatherSubmission:
        forecast_run_id = self.run_id_factory()
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

            policy = self._preflight_provider_policy(provider_name, provider)
            if policy is None:
                provider_results.append(
                    ProviderForecast(
                        provider=provider_name,
                        status="error",
                        points=[],
                        error_message="Data availability rejected: license_unknown",
                    )
                )
                continue

            try:
                provider_result = await provider.fetch(request)
                if provider_result.status == "ok" and provider_result.retrieved_at is None:
                    provider_result.retrieved_at = _observed_time(self.clock())
                if provider_result.status == "ok":
                    provider_result = self._admit_provider_result(provider_result, request)
                provider_results.append(provider_result)
            except Exception as exc:  # noqa: BLE001 - provider errors should not stop aggregation
                provider_results.append(
                    ProviderForecast(
                        provider=provider_name,
                        status="error",
                        points=[],
                        error_message=f"{type(exc).__name__}: provider request failed",
                    )
                )

        retrieved_at = _observed_time(self.clock())
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
        aggregation_completed_at = _observed_time(self.clock())
        submit_time = _submit_time(request.target_date)
        business_submission_deadline = _business_submission_deadline(request.target_date)
        forecast_start, forecast_end = _forecast_window(request.target_date)
        submission = WeatherSubmission(
            task_id=_task_id(request.target_date, location_slug(location)),
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time=business_submission_deadline,
            scope=ScopeProfile(
                region=request.region,
                target_date=request.target_date,
                time_granularity=request.granularity,
                location=location_payload(location),
            ),
            time_info=TimeInfo(
                submit_time=submit_time,
                data_cutoff_time=business_submission_deadline,
                forecast_start=forecast_start,
                forecast_end=forecast_end,
                retrieved_at=retrieved_at,
                provider_issued_at={result.provider: result.provider_issued_at for result in provider_results},
                aggregation_completed_at=aggregation_completed_at,
                valid_time=ForecastWindow(
                    start=forecast_start,
                    end=forecast_end,
                    timezone="Asia/Shanghai",
                ),
                forecast_run_id=forecast_run_id,
                business_submission_deadline=business_submission_deadline,
            ),
            data_profile=DataProfile(
                data_sources_summary=[
                    self.source_registry.disclosure_label(result.provider, result.source_url)
                    for result in provider_results
                    if result.provider in aggregated.providers_used
                ],
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

    def _preflight_provider_policy(
        self,
        provider_name: str,
        provider: WeatherProvider,
    ) -> SourcePolicy | None:
        policy = self.source_registry.policy(provider_name)
        if (
            policy.provider != provider_name
            or policy.environment != self.source_registry.environment
            or policy.license_status != "verified"
            or "calculation" not in policy.allowed_uses
            or policy.retention_policy not in {"derived_only", "metadata_only"}
        ):
            return None
        declared_endpoints = getattr(provider, "source_endpoints", ())
        if callable(declared_endpoints):
            declared_endpoints = declared_endpoints()
        if isinstance(declared_endpoints, str):
            declared_endpoints = (declared_endpoints,)
        try:
            normalized_endpoints = tuple(
                str(endpoint).strip()
                for endpoint in declared_endpoints
                if str(endpoint).strip()
            )
        except TypeError:
            return None
        if not normalized_endpoints:
            return None
        for endpoint in normalized_endpoints:
            if self.source_registry.resolve(provider_name, endpoint) != policy:
                return None
            if not any(
                same_source_endpoint(prefix, endpoint)
                for prefix in policy.source_url_prefixes
            ):
                return None
        return policy

    def _admit_provider_result(
        self,
        provider_result: ProviderForecast,
        request: ForecastRequest,
    ) -> ProviderForecast:
        if provider_result.raw is not None:
            provider_result = provider_result.model_copy(update={"raw": None})
        policy = self.source_registry.resolve(provider_result.provider, provider_result.source_url)
        completeness = _provider_completeness(provider_result, request, policy)
        record = external_record_from_provider_forecast(
            provider_result,
            valid_time=_provider_valid_time(provider_result),
            unit=policy.unit_manifest,
            granularity=request.granularity,
            coverage=_provider_coverage(request, policy),
            timezone=policy.timezone,
            completeness=completeness,
            quality_status="good" if completeness >= policy.min_completeness else "degraded",
            degradation_reason=(
                None
                if completeness >= policy.min_completeness
                else f"hourly completeness {completeness:.3f} below {policy.min_completeness:.3f}"
            ),
            fresh_until=_fresh_until(provider_result.retrieved_at, policy.max_age_seconds),
            license_status=policy.license_status,
            allowed_uses=policy.allowed_uses,
        )
        decision = DataAvailabilityGate(min_completeness=policy.min_completeness).evaluate(
            record,
            now=self.clock(),
        )
        if decision.status == "allowed_for_calculation":
            effective_retention = (
                "derived_only"
                if (
                    policy.retention_policy == "derived_only"
                    and decision.derived_storage_allowed
                )
                else "metadata_only"
            )
            return provider_result.model_copy(
                update={"retention_policy": effective_retention}
            )
        logger.warning(
            "provider_data_rejected provider=%s reason=%s",
            provider_result.provider,
            decision.reason,
        )
        return provider_result.model_copy(
            update={
                "status": "error",
                "points": [],
                "daily": {},
                "error_message": f"Data availability rejected: {decision.reason}",
            }
        )

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


def _business_submission_deadline(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    cutoff = datetime.combine(target - timedelta(days=1), time(hour=16), tzinfo=SHANGHAI_TZ)
    return cutoff.isoformat()


def _data_cutoff_time(target_date: str) -> str:
    """Compatibility alias for the business submission deadline."""
    return _business_submission_deadline(target_date)


def _submit_time(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    submit = datetime.combine(target - timedelta(days=1), time(hour=16, minute=45), tzinfo=SHANGHAI_TZ)
    return submit.isoformat()


def _forecast_window(target_date: str) -> tuple[str, str]:
    target = date.fromisoformat(target_date)
    start = datetime.combine(target, time(hour=0), tzinfo=SHANGHAI_TZ)
    end = datetime.combine(target, time(hour=23), tzinfo=SHANGHAI_TZ)
    return start.isoformat(), end.isoformat()


def _observed_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ForecastService clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _provider_valid_time(result: ProviderForecast) -> str | None:
    times = sorted(str(point.time).strip() for point in result.points if str(point.time).strip())
    if not times:
        return None
    return f"{times[0]}/{times[-1]}"


def _provider_completeness(
    result: ProviderForecast,
    request: ForecastRequest,
    policy: SourcePolicy,
) -> float:
    expected_points = 24 if request.granularity == "1h" else 1
    expected_values = expected_points * len(policy.required_metrics)
    if expected_values == 0:
        return 0.0
    observed_values = sum(
        1
        for point in result.points
        if str(point.time).strip()
        for metric in policy.required_metrics
        if getattr(point, metric, None) is not None
    )
    return round(min(1.0, observed_values / expected_values), 4)


def _provider_coverage(request: ForecastRequest, policy: SourcePolicy) -> str | None:
    if not policy.coverage_model:
        return None
    location = request.location_code or request.region.strip()
    return f"{policy.coverage_model}:{location}" if location else None


def _fresh_until(retrieved_at: str | None, max_age_seconds: int | None) -> str | None:
    if not retrieved_at or max_age_seconds is None:
        return None
    try:
        observed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None or observed.utcoffset() is None:
        return None
    return (observed + timedelta(seconds=max_age_seconds)).isoformat()


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
