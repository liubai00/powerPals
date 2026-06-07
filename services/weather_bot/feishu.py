from __future__ import annotations

from typing import Any

import httpx

from services.weather_bot.config import Settings
from services.weather_bot.models import WeatherSubmission
from services.weather_bot.tasks import WeatherTask


class FeishuClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._tenant_access_token: str | None = None

    async def tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            raise RuntimeError("Feishu app credentials are not configured")

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.settings.feishu_app_id,
                    "app_secret": self.settings.feishu_app_secret,
                },
            )
            response.raise_for_status()
            body = response.json()
        self._tenant_access_token = body["tenant_access_token"]
        return self._tenant_access_token

    async def send_interactive_card(self, chat_id: str, card: dict[str, Any]) -> str:
        token = await self.tenant_access_token()
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "interactive", "content": json_dumps(card["card"])},
            )
            response.raise_for_status()
            body = response.json()
        return body.get("data", {}).get("message_id", "")

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
    return payload.get("token") == expected_token


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
