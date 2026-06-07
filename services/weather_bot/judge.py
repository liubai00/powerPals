from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from services.weather_bot.models import WeatherSubmission


class WeatherTruthSummary(BaseModel):
    max_temperature: float
    min_temperature: float
    rain_observed: bool
    wind_speed: float


class WeatherJudgeRequest(BaseModel):
    submission: WeatherSubmission
    truth: WeatherTruthSummary


class WeatherJudgeResult(BaseModel):
    status: str = "scored"
    judge_bot_id: str = "powerpals-weather-judge-bot"
    scoring_version: str = "weather_judge_v0.1"
    task_id: str
    region: str
    target_date: str
    metrics: dict[str, Any]
    component_scores: dict[str, float]
    total_score: float
    summary: str


def score_weather_submission(request: WeatherJudgeRequest) -> WeatherJudgeResult:
    submission = request.submission
    truth = request.truth
    forecast = submission.aggregated_forecast.summary

    max_temperature_error = _absolute_error(forecast.max_temperature, truth.max_temperature)
    min_temperature_error = _absolute_error(forecast.min_temperature, truth.min_temperature)
    wind_speed_error = _absolute_error(forecast.wind_speed, truth.wind_speed)
    predicted_rain = (forecast.rain_probability or 0.0) >= 50.0
    rain_hit = predicted_rain == truth.rain_observed

    temperature_score = _temperature_score(max_temperature_error, min_temperature_error)
    rain_score = 100.0 if rain_hit else 40.0
    wind_score = _bounded_score(wind_speed_error, penalty_per_unit=15.0)
    total_score = round(temperature_score * 0.45 + rain_score * 0.35 + wind_score * 0.2, 2)

    return WeatherJudgeResult(
        task_id=submission.task_id,
        region=submission.region,
        target_date=submission.target_date,
        metrics={
            "max_temperature_error": max_temperature_error,
            "min_temperature_error": min_temperature_error,
            "rain_probability_threshold": 50.0,
            "rain_predicted": predicted_rain,
            "rain_observed": truth.rain_observed,
            "rain_hit": rain_hit,
            "wind_speed_error": wind_speed_error,
        },
        component_scores={
            "temperature": temperature_score,
            "rain": rain_score,
            "wind": wind_score,
        },
        total_score=total_score,
        summary=(
            f"基础评分完成：温度分 {temperature_score:.2f}，"
            f"降水分 {rain_score:.2f}，风速分 {wind_score:.2f}，综合分 {total_score:.2f}。"
        ),
    )


def _absolute_error(forecast_value: float | None, truth_value: float) -> float:
    if forecast_value is None:
        return 99.0
    return round(abs(forecast_value - truth_value), 2)


def _temperature_score(max_temperature_error: float, min_temperature_error: float) -> float:
    average_error = (max_temperature_error + min_temperature_error) / 2
    return _bounded_score(average_error, penalty_per_unit=10.0)


def _bounded_score(error: float, penalty_per_unit: float) -> float:
    return round(max(0.0, 100.0 - error * penalty_per_unit), 2)
