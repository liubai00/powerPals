from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.models import WeatherSubmission
from services.weather_bot.tasks import WeatherTask


@dataclass(frozen=True)
class FeishuBotAccount:
    app_id: str | None = None
    app_secret: str | None = None
    verification_token: str | None = None
    encrypt_key: str | None = None
    default_chat_id: str | None = None
    name: str = ""


class FeishuClient:
    _TOKEN_REFRESH_SKEW_SECONDS = 120

    def __init__(self, settings: Settings | None = None, account: FeishuBotAccount | None = None):
        self.settings = settings or Settings()
        self.account = account or FeishuBotAccount(
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            verification_token=self.settings.feishu_verification_token,
            encrypt_key=self.settings.feishu_encrypt_key,
            default_chat_id=self.settings.feishu_default_chat_id,
            name="legacy",
        )
        self._tenant_access_token: str | None = None
        self._tenant_access_token_expires_at = 0.0

    async def tenant_access_token(self) -> str:
        if self._tenant_access_token and time.monotonic() < self._tenant_access_token_expires_at:
            return self._tenant_access_token
        if not self.account.app_id or not self.account.app_secret:
            raise RuntimeError("Feishu app credentials are not configured")

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.account.app_id,
                    "app_secret": self.account.app_secret,
                },
            )
            response.raise_for_status()
            body = response.json()
        if body.get("code", 0) != 0:
            raise RuntimeError(f"Feishu tenant access token failed: {body}")
        self._tenant_access_token = body["tenant_access_token"]
        self._tenant_access_token_expires_at = time.monotonic() + self._token_cache_ttl(body.get("expire"))
        return self._tenant_access_token

    def _clear_tenant_access_token(self) -> None:
        self._tenant_access_token = None
        self._tenant_access_token_expires_at = 0.0

    def _token_cache_ttl(self, expire: Any) -> int:
        try:
            expire_seconds = int(expire)
        except (TypeError, ValueError):
            expire_seconds = 7200
        return max(60, expire_seconds - self._TOKEN_REFRESH_SKEW_SECONDS)

    async def send_interactive_card(self, chat_id: str, card: dict[str, Any]) -> str:
        body = await self.send_message(chat_id, "interactive", card["card"])
        return body.get("data", {}).get("message_id", "")

    async def send_text_message(self, chat_id: str, text: str) -> str:
        body = await self.send_message(chat_id, "text", {"text": text})
        return body.get("data", {}).get("message_id", "")

    async def send_message(self, chat_id: str, msg_type: str, content: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.tenant_access_token()
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": chat_id, "msg_type": msg_type, "content": json_dumps(content)},
                )
            if response.status_code >= 400:
                if attempt == 0 and _is_invalid_access_token(response):
                    self._clear_tenant_access_token()
                    continue
                raise RuntimeError(f"Feishu send message HTTP {response.status_code}: {response.text}")

            body = response.json()
            if body.get("code", 0) == 99991663 and attempt == 0:
                self._clear_tenant_access_token()
                continue
            if body.get("code", 0) != 0:
                raise RuntimeError(f"Feishu send message failed: {body}")
            return body

    async def reply_interactive_card(self, message_id: str, card: dict[str, Any], in_thread: bool = False) -> str:
        body = await self.reply_message(message_id, "interactive", card["card"], in_thread)
        return body.get("data", {}).get("message_id", "")

    async def reply_text_message(self, message_id: str, text: str, in_thread: bool = False) -> str:
        body = await self.reply_message(message_id, "text", {"text": text}, in_thread)
        return body.get("data", {}).get("message_id", "")

    async def reply_message(self, message_id: str, msg_type: str, content: dict[str, Any], in_thread: bool = False) -> dict[str, Any]:
        for attempt in range(2):
            token = await self.tenant_access_token()
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"msg_type": msg_type, "content": json_dumps(content), "reply_in_thread": in_thread},
                )
            if response.status_code >= 400:
                if attempt == 0 and _is_invalid_access_token(response):
                    self._clear_tenant_access_token()
                    continue
                raise RuntimeError(f"Feishu reply message HTTP {response.status_code}: {response.text}")
            body = response.json()
            if body.get("code", 0) == 99991663 and attempt == 0:
                self._clear_tenant_access_token()
                continue
            if body.get("code", 0) != 0:
                raise RuntimeError(f"Feishu reply message failed: {body}")
            return body

        raise RuntimeError("Feishu send message failed after token refresh")

    async def write_bitable_record(self, submission: WeatherSubmission, card_message_id: str | None = None) -> None:
        if not self.settings.feishu_bitable_app_token or not self.settings.feishu_bitable_table_id:
            return
        token = await self.tenant_access_token()
        fields = bitable_fields(submission, card_message_id)
        url = (
            "https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.settings.feishu_bitable_app_token}/tables/{self.settings.feishu_bitable_table_id}/records"
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json={"fields": fields})
            response.raise_for_status()

    async def write_task_bitable_record(self, task: WeatherTask) -> None:
        if not self.settings.feishu_bitable_app_token or not self.settings.feishu_task_bitable_table_id:
            return
        token = await self.tenant_access_token()
        url = (
            "https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.settings.feishu_bitable_app_token}/tables/{self.settings.feishu_task_bitable_table_id}/records"
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"fields": task_bitable_fields(task)},
            )
            response.raise_for_status()


def verify_feishu_token(payload: dict[str, Any], expected_token: str | None) -> bool:
    if not expected_token:
        return True
    header = payload.get("header", {})
    token = header.get("token") if isinstance(header, dict) else None
    return payload.get("token") == expected_token or token == expected_token


def _is_invalid_access_token(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return body.get("code") == 99991663


def bitable_fields(submission: WeatherSubmission, card_message_id: str | None = None) -> dict[str, Any]:
    summary = submission.aggregated_forecast.summary
    return {
        "task_id": submission.task_id,
        "target_date": submission.target_date,
        "region": submission.region,
        "data_cutoff_time": submission.data_cutoff_time,
        "providers_used": " / ".join(submission.aggregated_forecast.providers_used),
        "max_temp": summary.max_temperature,
        "min_temp": summary.min_temperature,
        "rain_probability": summary.rain_probability,
        "wind_speed": summary.wind_speed,
        "cloud_cover": summary.cloud_cover,
        "confidence": submission.confidence.get("score"),
        "risk_summary": "；".join(submission.risk_notes),
        "json_payload": json_dumps(submission.model_dump(mode="json")),
        "card_message_id": card_message_id or "",
        "status": "accepted",
        "notes": "",
    }


def task_bitable_fields(task: WeatherTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "track": task.track,
        "region": task.region,
        "location_code": task.location_code or "",
        "latitude": task.latitude,
        "longitude": task.longitude,
        "location_source": task.location_source,
        "target_date": task.target_date,
        "forecast_start": task.forecast_start,
        "forecast_end": task.forecast_end,
        "publish_time": task.publish_time,
        "data_cutoff_time": task.data_cutoff_time,
        "submission_deadline": task.submission_deadline,
        "status": task.status,
        "task_card_message_id": task.task_card_message_id or "",
        "submission_format_version": task.submission_format_version,
        "scoring_status": task.scoring_status,
        "notes": task.notes,
    }


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
