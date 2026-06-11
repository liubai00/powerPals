from __future__ import annotations

from typing import Any

import pytest

from services.weather_bot import feishu as feishu_module
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuBotAccount, FeishuClient


class FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP error in test: {self.status_code}")


class FakeAsyncClient:
    responses: list[FakeResponse] = []
    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, timeout: float):
        self.timeout = timeout

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = []
    FakeAsyncClient.calls = []
    monkeypatch.setattr(feishu_module.httpx, "AsyncClient", FakeAsyncClient)


def _client() -> FeishuClient:
    return FeishuClient(
        Settings(),
        FeishuBotAccount(app_id="app-id", app_secret="app-secret", verification_token="token"),
    )


async def test_tenant_access_token_refreshes_before_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(feishu_module.time, "monotonic", lambda: now[0])
    FakeAsyncClient.responses = [
        FakeResponse({"code": 0, "tenant_access_token": "token-1", "expire": 300}),
        FakeResponse({"code": 0, "tenant_access_token": "token-2", "expire": 300}),
    ]
    client = _client()

    assert await client.tenant_access_token() == "token-1"
    assert await client.tenant_access_token() == "token-1"

    now[0] = 1200.0

    assert await client.tenant_access_token() == "token-2"
    auth_calls = [url for url, _ in FakeAsyncClient.calls if "tenant_access_token/internal" in url]
    assert len(auth_calls) == 2


async def test_send_message_refreshes_token_after_invalid_token() -> None:
    FakeAsyncClient.responses = [
        FakeResponse({"code": 99991663, "msg": "Invalid access token"}, status_code=400),
        FakeResponse({"code": 0, "tenant_access_token": "fresh-token", "expire": 7200}),
        FakeResponse({"code": 0, "data": {"message_id": "om_1"}}),
    ]
    client = _client()
    client._tenant_access_token = "stale-token"
    client._tenant_access_token_expires_at = 999999.0

    body = await client.send_message("oc_chat", "text", {"text": "hi"})

    assert body["data"]["message_id"] == "om_1"
    message_calls = [kwargs for url, kwargs in FakeAsyncClient.calls if "/im/v1/messages" in url]
    assert message_calls[0]["headers"]["Authorization"] == "Bearer stale-token"
    assert message_calls[1]["headers"]["Authorization"] == "Bearer fresh-token"
