from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from services.weather_bot import memory as weather_memory
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import create_app
from services.weather_bot.official_warnings import (
    OfficialWarning,
    OfficialWarningFetchResult,
)


class _ForbiddenForecastService:
    def __init__(self) -> None:
        self.calls = 0

    async def forecast(self, _request):
        self.calls += 1
        raise AssertionError("official warning queries must not call forecast providers")


def _event(text: str) -> dict:
    token = uuid4().hex
    return {
        "header": {
            "event_id": f"warning-event-{token}",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "user-a"}},
            "message": {
                "message_id": f"warning-message-{token}",
                "chat_id": "warning-private-a",
                "chat_type": "p2p",
                "message_type": "text",
                "content": {"text": text},
            },
        },
    }


def _settings(tmp_path, *, policies: str = "[]") -> Settings:
    return Settings(
        app_env="test",
        qweather_api_key="not-a-real-key",
        qweather_api_host="warning-api.qweather.test",
        weather_source_policies_json=policies,
        feishu_weather_bot_open_id="ou_weather_bot",
        subscriptions_db=str(tmp_path / "subscriptions.db"),
        power_briefing_cache_db=str(tmp_path / "briefing.db"),
    )


def _disable_real_sends(monkeypatch, tmp_path) -> list[str]:
    monkeypatch.setattr(weather_memory, "DB_PATH", str(tmp_path / "memory.db"))
    sends: list[str] = []

    async def fake_send(*_args, **_kwargs) -> str:
        sends.append("direct_reply")
        return "warning-reply-message-id"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_text_message", fake_send)
    monkeypatch.setattr(FeishuClient, "reply_interactive_card", fake_send)
    return sends


def test_public_official_warning_query_uses_location_and_normalized_official_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    sends = _disable_real_sends(monkeypatch, tmp_path)
    calls: list[tuple[float, float, str]] = []
    issued = datetime(2026, 8, 9, 0, 30, tzinfo=timezone.utc)
    retrieved = datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc)

    async def fake_fetch(latitude, longitude, config, *, source_registry, source_policy, **_kwargs):
        calls.append((float(latitude), float(longitude), source_policy.provider))
        assert config.api_key.get_secret_value() == "not-a-real-key"
        assert source_registry.environment == "test"
        return OfficialWarningFetchResult(
            status="ok",
            reason="active_warnings",
            source_tag="official-run-1",
            zero_result=False,
            attribution="QWeather；国家预警信息发布中心",
            retrieved_at=retrieved,
            source_url="https://warning-api.qweather.test/weatheralert/v1/current/36.6512/117.1201",
            content_sha256="a" * 64,
            warnings=(
                OfficialWarning(
                    warning_id="warning-1",
                    headline="山东省气象台发布高温橙色预警",
                    original_issuer="山东省气象台",
                    published_at=issued,
                    retrieved_at=retrieved,
                    effective_at=issued,
                    expires_at=datetime(2026, 8, 9, 10, 30, tzinfo=timezone.utc),
                    source_url="https://warning-api.qweather.test/weatheralert/v1/current/36.6512/117.1201",
                    content_sha256="a" * 64,
                    source_tag="official-run-1",
                    message_type="Alert",
                    attribution="QWeather；国家预警信息发布中心",
                ),
            ),
        )

    monkeypatch.setattr("services.weather_bot.main.fetch_official_warnings", fake_fetch)
    policies = """[
      {
        "provider":"qweather_official_warning","environment":"test","profile":"warning-test",
        "license_status":"verified","allowed_uses":["text_reference","derived_storage"],
        "terms_version":"test-only","source_url_prefixes":["https://warning-api.qweather.test/weatheralert/v1/current/"],
        "unit_manifest":"warning_id:text;headline:text;original_issuer:text;published_at:iso8601;effective_at:iso8601;expires_at:iso8601;message_type:text;source_tag:text",
        "required_metrics":["warning_id","headline","original_issuer","published_at","effective_at","expires_at","message_type","source_tag"],
        "coverage_model":"latitude-longitude-point","timezone":"Asia/Shanghai","max_age_seconds":600,
        "retention_policy":"metadata_only","attribution_required":true,"attribution_text":"QWeather"
      }
    ]"""
    service = _ForbiddenForecastService()
    app = create_app(settings=_settings(tmp_path, policies=policies), forecast_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("山东有官方高温预警吗"),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "handled"
    assert payload["mode"] == "official_weather_warning"
    assert payload["source_kind"] == "official_structured_api"
    assert "山东省气象台发布高温橙色预警" in payload["text"]
    assert "山东省气象台" in payload["text"]
    assert "发布时间" in payload["text"]
    assert "抓取时间" in payload["text"]
    assert "https://warning-api.qweather.test/weatheralert/v1/current/" in payload["text"]
    assert "搜索摘要" not in payload["text"]
    assert calls == [(36.6512, 117.1201, "qweather_official_warning")]
    assert service.calls == 0
    assert sends == ["direct_reply"]

def test_official_warning_query_fails_closed_before_http_when_policy_is_unreviewed(
    monkeypatch,
    tmp_path,
) -> None:
    sends = _disable_real_sends(monkeypatch, tmp_path)
    calls: list[str] = []

    async def fake_fetch(*_args, **_kwargs):
        calls.append("adapter")
        return OfficialWarningFetchResult(
            status="unavailable",
            reason="source_policy_rejected",
        )

    monkeypatch.setattr("services.weather_bot.main.fetch_official_warnings", fake_fetch)
    service = _ForbiddenForecastService()
    app = create_app(settings=_settings(tmp_path), forecast_service=service)

    with TestClient(app) as client:
        response = client.post(
            "/feishu/events/weather",
            json=_event("山东有官方高温预警吗"),
        )

    payload = response.json()
    assert payload["status"] == "data_unavailable"
    assert payload["mode"] == "official_weather_warning"
    assert "当前无法取得可靠官方预警数据" in payload["text"]
    assert "不会使用搜索摘要或模型补写" in payload["text"]
    assert calls == ["adapter"]
    assert service.calls == 0
    assert sends == ["direct_reply"]
