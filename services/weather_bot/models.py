from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


DEFAULT_DISCLAIMER = (
    "本输出仅用于小可爱电力社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。"
)


class ForecastRequest(BaseModel):
    region: str = "深圳"
    target_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    granularity: Literal["1h", "day"] = "1h"
    providers: list[str] = Field(default_factory=lambda: ["open_meteo", "qweather", "caiyun"])

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value: str) -> str:
        normalized = value.strip()
        if normalized in {"深圳", "深圳市", "广东深圳", "广东省深圳市"}:
            return "广东省深圳市"
        return normalized


class ForecastPoint(BaseModel):
    time: str
    temperature: float | None = None
    precipitation_probability: float | None = None
    wind_speed: float | None = None
    cloud_cover: float | None = None


class ProviderForecast(BaseModel):
    provider: str
    status: Literal["ok", "disabled", "error"] = "ok"
    points: list[ForecastPoint] = Field(default_factory=list)
    error_message: str | None = None
    raw: dict[str, Any] | None = None


class ForecastSummary(BaseModel):
    max_temperature: float | None = None
    min_temperature: float | None = None
    rain_probability: float | None = None
    wind_speed: float | None = None
    cloud_cover: float | None = None
    main_weather: str
    high_risk_period: str


class AggregatedForecast(BaseModel):
    providers_used: list[str]
    points: list[ForecastPoint]
    summary: ForecastSummary


class WeatherSubmission(BaseModel):
    task_id: str
    region: str
    target_date: str
    data_cutoff_time: str
    provider_results: list[ProviderForecast]
    aggregated_forecast: AggregatedForecast
    confidence: dict[str, Any]
    key_factors: list[str]
    risk_notes: list[str]
    disclaimer: str = DEFAULT_DISCLAIMER


class SubmissionRecord(BaseModel):
    submission: WeatherSubmission
    card_message_id: str | None = None
    status: str = "accepted"
    notes: str | None = None
