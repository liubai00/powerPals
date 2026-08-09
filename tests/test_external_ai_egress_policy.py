from __future__ import annotations

import pytest

from services.weather_bot.config import Settings
from services.weather_bot.llm import LlmClient
from services.weather_bot.models import AggregatedForecast, ForecastSummary, WeatherSubmission
from services.weather_bot.openclaw import OpenClawExplainer
from services.weather_bot.service import ForecastService


class _SuccessfulChatResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "should-not-be-returned"}}]}


class _CountingAsyncClient:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return _SuccessfulChatResponse()


class _SuccessfulOpenClawResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "key_factors": ["remote interpreter result"],
            "risk_notes": ["remote interpreter risk"],
        }


class _CountingOpenClawAsyncClient(_CountingAsyncClient):
    async def post(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return _SuccessfulOpenClawResponse()


def _submission() -> WeatherSubmission:
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
        key_factors=["local"],
        risk_notes=["local"],
    )


async def test_settings_llm_egress_is_disabled_by_default_even_with_credentials(monkeypatch):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_api_base_url="https://llm.example.test/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_dry_run_vetoes_explicit_llm_egress(monkeypatch):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            dry_run=True,
            llm_egress_enabled=True,
            llm_api_base_url="https://llm.example.test/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_settings_llm_egress_rejects_every_model_except_gpt_5_6_sol(monkeypatch):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_egress_enabled=True,
            llm_api_base_url="https://llm.example.test/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.5-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


@pytest.mark.parametrize(
    "raw_allowlist",
    ["[]", "", "{", '{"prefix":"https://llm.example.test/v1"}', '["https://llm.example.test/v1", 7]'],
)
async def test_settings_llm_egress_rejects_missing_or_invalid_allowlist_json(
    monkeypatch,
    raw_allowlist,
):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_egress_enabled=True,
            llm_allowed_https_prefixes_json=raw_allowlist,
            llm_api_base_url="https://llm.example.test/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_settings_llm_egress_rejects_http_even_when_listed(monkeypatch):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_egress_enabled=True,
            llm_allowed_https_prefixes_json='["http://llm.example.test/v1"]',
            llm_api_base_url="http://llm.example.test/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_settings_llm_egress_rejects_a_similar_malicious_hostname(monkeypatch):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_egress_enabled=True,
            llm_allowed_https_prefixes_json='["https://llm.example.test/v1"]',
            llm_api_base_url="https://llm.example.test.evil/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_settings_llm_egress_rejects_an_unreviewed_port(monkeypatch):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_egress_enabled=True,
            llm_allowed_https_prefixes_json='["https://llm.example.test/v1"]',
            llm_api_base_url="https://llm.example.test:8443/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_settings_llm_egress_enforces_path_boundary_and_allows_exact_https_prefix(
    monkeypatch,
):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    common = {
        "_env_file": None,
        "llm_egress_enabled": True,
        "llm_allowed_https_prefixes_json": '["https://llm.example.test/v1"]',
        "llm_api_key": "configured-secret",
        "llm_model": "gpt-5.6-sol",
    }
    outside_client = LlmClient.from_settings(
        Settings(**common, llm_api_base_url="https://llm.example.test/v11")
    )
    traversal_client = LlmClient.from_settings(
        Settings(
            **common,
            llm_api_base_url="https://llm.example.test/v1/%2e%2e/private",
        )
    )
    approved_client = LlmClient.from_settings(
        Settings(**common, llm_api_base_url="https://llm.example.test/v1")
    )

    outside_result = await outside_client.chat(
        [{"role": "user", "content": "private outside prompt"}]
    )
    traversal_result = await traversal_client.chat(
        [{"role": "user", "content": "private traversal prompt"}]
    )
    approved_result = await approved_client.chat(
        [{"role": "user", "content": "private approved prompt"}]
    )

    assert outside_result is None
    assert traversal_result is None
    assert approved_result == "should-not-be-returned"
    assert len(_CountingAsyncClient.calls) == 1


@pytest.mark.parametrize(
    "invalid_prefix",
    [
        "https://reviewer@llm.example.test/v1",
        "https://llm.example.test:invalid/v1",
        "https://llm.example.test/v1?redirect=elsewhere",
        "https://llm.example.test/v1#fragment",
    ],
)
async def test_one_invalid_entry_rejects_the_entire_llm_allowlist(
    monkeypatch,
    invalid_prefix,
):
    _CountingAsyncClient.calls = []
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _CountingAsyncClient)
    client = LlmClient.from_settings(
        Settings(
            _env_file=None,
            llm_egress_enabled=True,
            llm_allowed_https_prefixes_json=(
                '["https://llm.example.test/v1", '
                f'"{invalid_prefix}"]'
            ),
            llm_api_base_url="https://llm.example.test/v1",
            llm_api_key="configured-secret",
            llm_model="gpt-5.6-sol",
        )
    )

    result = await client.chat([{"role": "user", "content": "private prompt"}])

    assert result is None
    assert _CountingAsyncClient.calls == []


async def test_forecast_service_openclaw_egress_is_disabled_by_default(monkeypatch):
    _CountingOpenClawAsyncClient.calls = []
    monkeypatch.setattr(
        "services.weather_bot.openclaw.httpx.AsyncClient",
        _CountingOpenClawAsyncClient,
    )
    service = ForecastService(
        providers={},
        settings=Settings(
            _env_file=None,
            openclaw_api_url="https://openclaw.example.test/explain",
            openclaw_api_key="configured-secret",
        ),
    )

    explanation = await service.explainer.explain(_submission())

    assert "remote interpreter result" not in explanation["key_factors"]
    assert _CountingOpenClawAsyncClient.calls == []


async def test_forecast_service_dry_run_vetoes_explicit_openclaw_egress(monkeypatch):
    _CountingOpenClawAsyncClient.calls = []
    monkeypatch.setattr(
        "services.weather_bot.openclaw.httpx.AsyncClient",
        _CountingOpenClawAsyncClient,
    )
    service = ForecastService(
        providers={},
        settings=Settings(
            _env_file=None,
            dry_run=True,
            openclaw_egress_enabled=True,
            openclaw_api_url="https://openclaw.example.test/explain",
            openclaw_api_key="configured-secret",
        ),
    )

    explanation = await service.explainer.explain(_submission())

    assert "remote interpreter result" not in explanation["key_factors"]
    assert _CountingOpenClawAsyncClient.calls == []


async def test_forecast_service_openclaw_egress_rejects_an_empty_allowlist(monkeypatch):
    _CountingOpenClawAsyncClient.calls = []
    monkeypatch.setattr(
        "services.weather_bot.openclaw.httpx.AsyncClient",
        _CountingOpenClawAsyncClient,
    )
    service = ForecastService(
        providers={},
        settings=Settings(
            _env_file=None,
            openclaw_egress_enabled=True,
            openclaw_allowed_https_prefixes_json="[]",
            openclaw_api_url="https://openclaw.example.test/explain",
            openclaw_api_key="configured-secret",
        ),
    )

    explanation = await service.explainer.explain(_submission())

    assert "remote interpreter result" not in explanation["key_factors"]
    assert _CountingOpenClawAsyncClient.calls == []


@pytest.mark.parametrize(
    "api_url",
    [
        "http://openclaw.example.test/explain",
        "https://openclaw.example.test.evil/explain",
        "https://openclaw.example.test:8443/explain",
        "https://openclaw.example.test/explanation",
        "https://openclaw.example.test/explain/%2e%2e/private",
    ],
)
async def test_openclaw_settings_reject_unreviewed_scheme_host_port_or_path(
    monkeypatch,
    api_url,
):
    _CountingOpenClawAsyncClient.calls = []
    monkeypatch.setattr(
        "services.weather_bot.openclaw.httpx.AsyncClient",
        _CountingOpenClawAsyncClient,
    )
    explainer = OpenClawExplainer.from_settings(
        Settings(
            _env_file=None,
            openclaw_egress_enabled=True,
            openclaw_allowed_https_prefixes_json=(
                '["https://openclaw.example.test/explain"]'
            ),
            openclaw_api_url=api_url,
            openclaw_api_key="configured-secret",
        )
    )

    explanation = await explainer.explain(_submission())

    assert "remote interpreter result" not in explanation["key_factors"]
    assert _CountingOpenClawAsyncClient.calls == []


async def test_openclaw_settings_allow_an_exact_reviewed_https_prefix(monkeypatch):
    _CountingOpenClawAsyncClient.calls = []
    monkeypatch.setattr(
        "services.weather_bot.openclaw.httpx.AsyncClient",
        _CountingOpenClawAsyncClient,
    )
    explainer = OpenClawExplainer.from_settings(
        Settings(
            _env_file=None,
            openclaw_egress_enabled=True,
            openclaw_allowed_https_prefixes_json=(
                '["https://openclaw.example.test/explain"]'
            ),
            openclaw_api_url="https://openclaw.example.test/explain",
            openclaw_api_key="configured-secret",
        )
    )

    explanation = await explainer.explain(_submission())

    assert explanation["key_factors"] == ["remote interpreter result"]
    assert len(_CountingOpenClawAsyncClient.calls) == 1
