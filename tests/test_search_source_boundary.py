from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.main import create_app
from services.weather_bot.llm import LlmClient
from services.weather_bot.search import TavilySearchClient
from services.weather_bot.typhoon import TyphoonClient


class ForecastMustNotRun:
    async def forecast(self, request):
        raise AssertionError("knowledge source boundary must not call forecast providers")


def test_unverified_search_and_legacy_typhoon_sources_make_zero_external_calls(monkeypatch):
    calls = {"search": 0, "typhoon": 0}

    async def fail_search(self, query):
        calls["search"] += 1
        raise AssertionError("search snippets are discovery-only and must not be fetched here")

    async def fail_typhoon(self, text):
        calls["typhoon"] += 1
        raise AssertionError("unverified typhoon source must fail before HTTP")

    async def fail_llm(self, messages, *, temperature=0.2, max_tokens=600):
        raise AssertionError("LLM must not invent missing live typhoon facts")

    monkeypatch.setattr(TavilySearchClient, "search", fail_search)
    monkeypatch.setattr(TyphoonClient, "brief_for_text", fail_typhoon)
    monkeypatch.setattr(LlmClient, "chat", fail_llm)
    client = TestClient(
        create_app(
            forecast_service=ForecastMustNotRun(),
            settings=Settings(
                app_env="test",
                tavily_api_key="configured-but-not-authorized",
                qweather_api_key="configured-but-not-authorized",
                llm_api_base_url="https://llm.example.test/v1",
                llm_api_key="configured-but-not-authorized",
                weather_source_policies_json="[]",
                feishu_allow_unsigned_events=True,
            ),
        )
    )

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_type": "p2p",
                    "content": '{"text":"请解释当前台风实时路径和数据来源"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "knowledge_answer"
    assert body["search_result_count"] == 0
    assert body["typhoon_grounded"] is False
    assert "当前无法取得通过来源核验的实时台风数据" in body["text"]
    assert calls == {"search": 0, "typhoon": 0}
