from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DEFAULT_DISCLAIMER = (
    "本输出仅用于小可爱电力社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。"
)


class ForecastRequest(BaseModel):
    region: str = "广东省深圳市"
    latitude: float | None = None
    longitude: float | None = None
    location_code: str | None = None
    location_source: str | None = None
    target_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    days: int = Field(default=1, ge=1, le=16)
    granularity: Literal["1h", "day"] = "1h"
    providers: list[str] = Field(default_factory=lambda: ["open_meteo", "qweather", "caiyun"])

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value: str) -> str:
        normalized = value.strip()
        if normalized in {"深圳", "深圳市", "广东深圳", "广东省深圳市"}:
            return "广东省深圳市"
        return normalized

    @model_validator(mode="after")
    def validate_coordinates(self) -> "ForecastRequest":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


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


class BotProfile(BaseModel):
    bot_id: str = "powerpals-weather-bot"
    bot_name: str = "PowerPals Weather Bot"
    bot_version: str = "v0.6.0"
    owner: str = "PowerPals"
    maturity_group: str = "Baseline Bot"


class ScopeProfile(BaseModel):
    region: str = "广东省深圳市"
    target_date: str | None = None
    time_granularity: str = "1h"
    location: dict[str, Any] = Field(default_factory=dict)
    applicable_scenarios: list[str] = Field(
        default_factory=lambda: ["负荷预测参考", "新能源出力观察", "电价复盘辅助", "储能运行观察"]
    )


class TimeInfo(BaseModel):
    submit_time: str = ""
    data_cutoff_time: str = ""
    forecast_start: str = ""
    forecast_end: str = ""


class DataProfile(BaseModel):
    data_source_group: str = "公开数据组"
    data_sources_summary: list[str] = Field(default_factory=list)
    disclosure_level: str = "类型披露"
    known_limitations: list[str] = Field(default_factory=lambda: ["局地短时强降水和云量变化存在不确定性"])
    provider_status: dict[str, str] = Field(default_factory=dict)


class WeatherPayload(BaseModel):
    unit: dict[str, str] = Field(
        default_factory=lambda: {
            "temperature": "degC",
            "precipitation_probability": "%",
            "wind_speed": "m/s",
            "cloud_cover": "%",
        }
    )
    values: list[ForecastPoint] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class ExplanationProfile(BaseModel):
    key_factors: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    business_readable_summary: str = ""


class ScoringProfile(BaseModel):
    participate_in_public_scorecard: bool = True
    preferred_group: str = "公开数据组"
    self_declared_strengths: list[str] = Field(default_factory=lambda: ["逐小时气象预测", "多源聚合", "风险提示"])
    not_suitable_for: list[str] = Field(default_factory=lambda: ["自动交易指令", "报价建议", "收益承诺"])


class WeatherSubmission(BaseModel):
    submission_type: str = "official_submission"
    task_id: str
    track: str = "weather_forecast"
    bot: BotProfile = Field(default_factory=BotProfile)
    scope: ScopeProfile = Field(default_factory=ScopeProfile)
    time_info: TimeInfo = Field(default_factory=TimeInfo)
    data_profile: DataProfile = Field(default_factory=DataProfile)
    payload: WeatherPayload = Field(default_factory=WeatherPayload)
    explanation: ExplanationProfile = Field(default_factory=ExplanationProfile)
    scoring_profile: ScoringProfile = Field(default_factory=ScoringProfile)
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
