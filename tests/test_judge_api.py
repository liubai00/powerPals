from fastapi.testclient import TestClient
from pytest import approx

from services.weather_bot.main import create_app
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


class FakeForecastService:
    pass


def make_submission() -> dict:
    submission = WeatherSubmission(
        task_id="WEATHER-CN-440100-20260610-DAYAHEAD-001",
        region="广东省广州市",
        target_date="2026-06-10",
        data_cutoff_time="2026-06-09T16:00:00+08:00",
        provider_results=[],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo"],
            points=[
                ForecastPoint(
                    time="2026-06-10T00:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=45.0,
                    wind_speed=3.2,
                    cloud_cover=60.0,
                )
            ],
            summary=ForecastSummary(
                max_temperature=32.5,
                min_temperature=27.4,
                rain_probability=45.0,
                wind_speed=3.2,
                cloud_cover=60.0,
                main_weather="多云",
                high_risk_period="无明显高风险时段",
            ),
        ),
        confidence={"score": 0.7, "description": "中等"},
        key_factors=["多源气象预报融合"],
        risk_notes=["局地短时天气存在不确定性"],
    )
    return submission.model_dump(mode="json")


def test_weather_judge_scores_submission_against_truth_summary():
    client = TestClient(create_app(forecast_service=FakeForecastService()))

    response = client.post(
        "/api/judge/weather/score",
        json={
            "submission": make_submission(),
            "truth": {
                "max_temperature": 31.0,
                "min_temperature": 26.0,
                "rain_observed": False,
                "wind_speed": 3.0,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "scored"
    assert body["judge_bot_id"] == "powerpals-weather-judge-bot"
    assert body["task_id"] == "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    assert body["metrics"]["max_temperature_error"] == approx(1.5)
    assert body["metrics"]["min_temperature_error"] == approx(1.4)
    assert body["metrics"]["rain_hit"] is True
    assert body["metrics"]["wind_speed_error"] == approx(0.2)
    assert body["total_score"] == approx(92.88)
    assert "基础评分" in body["summary"]
