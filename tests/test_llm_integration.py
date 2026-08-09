from services.weather_bot.config import Settings
from services.weather_bot.llm import (
    LlmClient,
    answer_role_question,
    answer_weather_knowledge_question,
)
from services.weather_bot.models import AggregatedForecast, ForecastSummary, WeatherSubmission
from services.weather_bot.openclaw import OpenClawExplainer


class FakeLlmClient:
    enabled = True

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        return '{"key_factors":["模型解释了多源预测"],"risk_notes":["模型提示了局地不确定性"]}'


class UnsafeExplanationLlmClient:
    enabled = True

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        return '{"key_factors":["明天电价存在上行空间"],"risk_notes":["建议提高申报价"]}'


class CapturingAnswerLlmClient:
    enabled = True

    def __init__(self):
        self.calls = []

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        self.calls.append(messages)
        return "只能说明当前响应中实际通过来源门禁的数据。"


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
    assert LlmClient("https://kunai.one", "key", "gpt-5.6-sol")._chat_completions_url() == (
        "https://kunai.one/v1/chat/completions"
    )
    assert LlmClient("https://kunai.one/v1", "key", "gpt-5.6-sol")._chat_completions_url() == (
        "https://kunai.one/v1/chat/completions"
    )


def test_default_llm_model_is_gpt_5_6_sol(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)

    assert Settings(_env_file=None).llm_model == "gpt-5.6-sol"


async def test_openclaw_explainer_uses_llm_fallback_when_no_openclaw_url():
    explainer = OpenClawExplainer(llm_client=FakeLlmClient())

    explanation = await explainer.explain(make_submission())

    assert explanation["key_factors"] == ["模型解释了多源预测"]
    assert explanation["risk_notes"] == ["模型提示了局地不确定性"]


async def test_llm_forecast_explanation_fails_closed_on_market_claims():
    explanation = await OpenClawExplainer(llm_client=UnsafeExplanationLlmClient()).explain(
        make_submission()
    )

    serialized = str(explanation)
    assert "电价存在上行空间" not in serialized
    assert "提高申报价" not in serialized
    assert "逐小时温度、降水、风速和云量综合判断" in serialized


async def test_remote_openclaw_explanation_fails_closed_on_market_claims(monkeypatch):
    sent_payloads = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "key_factors": ["天气对多头更有利"],
                "risk_notes": ["山东现货大概率偏强，建议提高申报价"],
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            sent_payloads.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr("services.weather_bot.openclaw.httpx.AsyncClient", FakeAsyncClient)

    explanation = await OpenClawExplainer(api_url="https://example.invalid/explain").explain(
        make_submission()
    )

    serialized = str(explanation)
    assert "多头更有利" not in serialized
    assert "提高申报价" not in serialized
    assert "逐小时温度、降水、风速和云量综合判断" in serialized
    sent = sent_payloads[0]["submission"]
    assert "provider_results" not in sent
    assert "payload" not in sent
    assert "aggregated_forecast" not in sent
    assert sent["summary"]["max_temperature"] == 30.0
    assert sent["forecast_run_id"] == ""


async def test_role_and_knowledge_prompts_do_not_claim_fixed_sources_without_request_provenance():
    client = CapturingAnswerLlmClient()

    await answer_role_question(
        client,
        bot_role="weather_forecast_bot",
        user_text="你使用哪些数据源？",
        fallback="来源不可用",
    )
    await answer_weather_knowledge_question(
        client,
        user_text="你的数据是从哪里来的？",
        fallback="来源不可用",
    )

    prompts = "\n".join(
        str(message.get("content") or "")
        for call in client.calls
        for message in call
        if message.get("role") == "system"
    )
    assert "预测数据来自 Open-Meteo 与 和风天气" not in prompts
    assert "城市/区县中文名通过和风 Geocoding 解析" not in prompts
    assert "每次查询实时拉取" not in prompts
    assert prompts.count("当前响应中实际通过") == 2
    assert "来源元数据门禁" in prompts
