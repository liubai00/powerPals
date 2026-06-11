from services.weather_bot.llm import LlmClient
from services.weather_bot.models import AggregatedForecast, ForecastSummary, WeatherSubmission
from services.weather_bot.openclaw import OpenClawExplainer


class FakeLlmClient:
    enabled = True

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        return '{"key_factors":["模型解释了多源预测"],"risk_notes":["模型提示了局地不确定性"]}'


def make_submission() -> WeatherSubmission:
    return WeatherSubmission(
        task_id="WEATHER-CN-440100-20260610-DAYAHEAD-001",
        region="广东省广州市",
        target_date="2026-06-10",
        data_cutoff_time="2026-06-09T16:00:00+08:00",
        provider_results=[],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo"],
            points=[],
            summary=ForecastSummary(
                max_temperature=30.0,
                min_temperature=22.0,
                rain_probability=20.0,
                wind_speed=3.0,
                cloud_cover=60.0,
                main_weather="多云",
                high_risk_period="无明显高风险时段",
            ),
        ),
        confidence={"score": 0.7, "description": "中等"},
        key_factors=["多源气象预测融合"],
        risk_notes=["局地短时天气存在不确定性"],
    )


def test_llm_client_builds_openai_compatible_chat_url():
    assert LlmClient("https://kunai.one", "key", "gpt-5.5")._chat_completions_url() == (
        "https://kunai.one/v1/chat/completions"
    )
    assert LlmClient("https://kunai.one/v1", "key", "gpt-5.5")._chat_completions_url() == (
        "https://kunai.one/v1/chat/completions"
    )


async def test_openclaw_explainer_uses_llm_fallback_when_no_openclaw_url():
    explainer = OpenClawExplainer(llm_client=FakeLlmClient())

    explanation = await explainer.explain(make_submission())

    assert explanation["key_factors"] == ["模型解释了多源预测"]
    assert explanation["risk_notes"] == ["模型提示了局地不确定性"]
