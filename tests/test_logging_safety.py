import logging

from fastapi.testclient import TestClient
import pytest

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.llm import LlmClient
from services.weather_bot.main import create_app
from services.weather_bot.logging_safety import (
    redact_sensitive_text,
    safe_error_summary,
    text_log_metadata,
)
from services.weather_bot import memory as weather_memory
from services.weather_bot.search import TavilySearchClient


class _ErrorResponse:
    status_code = 400
    text = '{"error":"secret-response-body","token":"should-not-be-logged"}'

    def json(self):
        return {"code": 99999, "message": "secret-response-body"}


class _ErrorAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, *args, **kwargs):
        return _ErrorResponse()


class _FixedTokenFeishuClient(FeishuClient):
    async def tenant_access_token(self) -> str:
        return "test-token"


def test_text_log_metadata_never_returns_raw_user_text():
    raw_text = "我的手机号是13800138000，查山东明天天气"

    metadata = text_log_metadata(raw_text)

    assert metadata["text_length"] == len(raw_text)
    assert len(metadata["text_sha256"]) == 12
    assert raw_text not in str(metadata)
    assert "13800138000" not in str(metadata)


def test_text_log_metadata_has_stable_but_non_reversible_fingerprint():
    first = text_log_metadata("山东明天天气")
    second = text_log_metadata("山东明天天气")
    different = text_log_metadata("河南明天天气")

    assert first == second
    assert first["text_sha256"] != different["text_sha256"]


def test_safe_error_summary_does_not_include_exception_message_or_url():
    error = RuntimeError(
        "https://open.feishu.cn/open-apis/bitable/v1/apps/secret-app-token"
    )

    assert safe_error_summary(error) == "RuntimeError"


def test_sensitive_conversation_text_is_redacted_but_keeps_weather_intent():
    raw = (
        "查山东明天天气，密码：TopSecret123，token=abc.def.ghi，"
        "Authorization: Bearer opaque-token-value，手机号13800138000，持仓100MWh"
    )

    redacted = redact_sensitive_text(raw)

    assert "查山东明天天气" in redacted
    for secret in ("TopSecret123", "abc.def.ghi", "opaque-token-value", "13800138000", "100MWh"):
        assert secret not in redacted
    assert "[REDACTED" in redacted


def test_conversation_memory_never_persists_raw_sensitive_text(monkeypatch, tmp_path):
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "memory.db"))
    raw = "查河南天气，API_KEY=private-key-value，密码: private-password"

    weather_memory.record_turn("weather|p2p|chat|root|user", "user", raw)

    turns = weather_memory.recent_turns("weather|p2p|chat|root|user")
    serialized = str(turns)
    assert "查河南天气" in serialized
    assert "private-key-value" not in serialized
    assert "private-password" not in serialized


def test_feishu_event_log_does_not_retain_raw_message(caplog):
    raw_text = "云云能做什么，我的手机号是13800138000"
    client = TestClient(
        create_app(settings=Settings(feishu_weather_verification_token=None))
    )

    with caplog.at_level(logging.WARNING, logger="services.weather_bot.main"):
        response = client.post(
            "/feishu/events/weather",
            json={
                "event": {
                    "message": {
                        "chat_type": "p2p",
                        "message_type": "text",
                        "content": {"text": raw_text},
                    }
                }
            },
        )

    assert response.status_code == 200
    assert raw_text not in caplog.text
    assert "13800138000" not in caplog.text
    assert "text_sha256=" in caplog.text


def test_progress_send_failure_log_never_includes_exception_message(monkeypatch, caplog):
    async def fail_send(self, chat_id, text):
        raise RuntimeError("token=private-progress-token https://secret.example/path")

    monkeypatch.setattr(FeishuClient, "send_text_message", fail_send)
    client = TestClient(
        create_app(
            settings=Settings(
                app_env="test",
                feishu_allow_unsigned_events=True,
                feishu_progress_message_enabled=True,
            )
        )
    )

    with caplog.at_level(logging.WARNING, logger="services.weather_bot.main"):
        response = client.post(
            "/feishu/events/weather",
            json={
                "event": {
                    "message": {
                        "chat_id": "p2p-safe-log",
                        "chat_type": "p2p",
                        "message_type": "text",
                        "content": '{"text":"广州明天天气"}',
                    }
                }
            },
        )

    assert response.status_code == 200
    assert "feishu_progress_message_failed" in caplog.text
    assert "private-progress-token" not in caplog.text
    assert "secret.example" not in caplog.text


async def test_llm_http_error_log_does_not_retain_response_body(monkeypatch, caplog):
    monkeypatch.setattr("services.weather_bot.llm.httpx.AsyncClient", _ErrorAsyncClient)
    client = LlmClient("https://llm.example.test", "secret-key", "test-model")

    with caplog.at_level(logging.WARNING, logger="services.weather_bot.llm"):
        result = await client.chat([{"role": "user", "content": "test"}])

    assert result is None
    assert "LLM chat HTTP 400" in caplog.text
    assert "secret-response-body" not in caplog.text
    assert "should-not-be-logged" not in caplog.text


async def test_search_http_error_log_does_not_retain_response_body(monkeypatch, caplog):
    monkeypatch.setattr("services.weather_bot.search.httpx.AsyncClient", _ErrorAsyncClient)
    client = TavilySearchClient("secret-key")

    with caplog.at_level(logging.WARNING, logger="services.weather_bot.search"):
        result = await client.search("test query")

    assert result == []
    assert "Tavily search HTTP 400" in caplog.text
    assert "secret-response-body" not in caplog.text
    assert "should-not-be-logged" not in caplog.text


async def test_feishu_send_error_does_not_expose_response_body(monkeypatch):
    monkeypatch.setattr("services.weather_bot.feishu.httpx.AsyncClient", _ErrorAsyncClient)
    client = _FixedTokenFeishuClient(Settings())

    with pytest.raises(RuntimeError) as exc_info:
        await client.send_message("oc_private_chat", "text", {"text": "private"})

    assert "HTTP 400" in str(exc_info.value)
    assert "secret-response-body" not in str(exc_info.value)
    assert "should-not-be-logged" not in str(exc_info.value)
