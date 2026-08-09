from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import httpx
import lark_oapi as lark
from lark_oapi import ws

from services.weather_bot.config import Settings
from services.weather_bot.logging_safety import text_log_metadata


logger = logging.getLogger(__name__)

FEISHU_WEATHER_BOT = "weather"
FEISHU_TASK_BOT = "task"


@dataclass(frozen=True)
class WsBotConfig:
    role: str
    endpoint_path: str
    app_id: str
    app_secret: str
    verification_token: str | None = None
    encrypt_key: str | None = None


def run_feishu_ws_bot(role: str | None = None, settings: Settings | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or Settings()
    role = role or os.getenv("FEISHU_WS_BOT_ROLE", FEISHU_WEATHER_BOT)
    bot = _ws_bot_config(settings, role)
    internal_base_url = os.getenv("FEISHU_INTERNAL_BASE_URL", "http://weather-bot:8000").rstrip("/")

    def handle_message(data) -> None:
        payload = _sdk_event_payload(data, bot)
        event = payload.get("event", {})
        message = event.get("message", {}) if isinstance(event, dict) else {}
        message_id = str(message.get("message_id") or "") if isinstance(message, dict) else ""
        logger.info(
            "Feishu WS event received role=%s event_type=%s message_sha256=%s chat_type=%s",
            bot.role,
            payload.get("header", {}).get("event_type") if isinstance(payload.get("header"), dict) else "",
            text_log_metadata(message_id)["text_sha256"] if message_id else "",
            message.get("chat_type") if isinstance(message, dict) else "",
        )
        response = httpx.post(
            f"{internal_base_url}{bot.endpoint_path}",
            json=payload,
            timeout=max(30.0, settings.feishu_internal_timeout_seconds),
        )
        if response.status_code >= 400:
            logger.warning(
                "Feishu WS event dispatch failed role=%s status=%s",
                bot.role,
                response.status_code,
            )

    def ignore_event(data) -> None:
        payload = _sdk_event_payload(data, bot)
        header = payload.get("header", {})
        logger.debug(
            "Ignoring Feishu WS event role=%s event_type=%s",
            bot.role,
            header.get("event_type") if isinstance(header, dict) else "",
        )

    event_handler = (
        lark.EventDispatcherHandler.builder(bot.encrypt_key or "", bot.verification_token or "")
        .register_p2_im_message_receive_v1(handle_message)
        .register_p2_im_message_message_read_v1(ignore_event)
        .register_p2_im_message_reaction_created_v1(ignore_event)
        .register_p2_im_message_reaction_deleted_v1(ignore_event)
        .register_p2_im_message_recalled_v1(ignore_event)
        .build()
    )
    logger.info("Starting Feishu WebSocket bot role=%s endpoint=%s", bot.role, bot.endpoint_path)
    ws.Client(
        bot.app_id,
        bot.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.WARNING,
    ).start()


def _ws_bot_config(settings: Settings, role: str) -> WsBotConfig:
    if role == FEISHU_WEATHER_BOT:
        app_id = settings.feishu_weather_app_id or settings.feishu_app_id
        app_secret = settings.feishu_weather_app_secret or settings.feishu_app_secret
        if not app_id or not app_secret:
            raise RuntimeError("Weather Feishu app credentials are not configured")
        return WsBotConfig(
            role=role,
            endpoint_path="/feishu/events/weather",
            app_id=app_id,
            app_secret=app_secret,
            verification_token=settings.feishu_weather_verification_token or settings.feishu_verification_token,
            encrypt_key=settings.feishu_weather_encrypt_key or settings.feishu_encrypt_key,
        )
    if role == FEISHU_TASK_BOT:
        app_id = settings.feishu_task_app_id or settings.feishu_app_id
        app_secret = settings.feishu_task_app_secret or settings.feishu_app_secret
        if not app_id or not app_secret:
            raise RuntimeError("Task Feishu app credentials are not configured")
        return WsBotConfig(
            role=role,
            endpoint_path="/feishu/events/task",
            app_id=app_id,
            app_secret=app_secret,
            verification_token=settings.feishu_task_verification_token or settings.feishu_verification_token,
            encrypt_key=settings.feishu_task_encrypt_key or settings.feishu_encrypt_key,
        )
    raise RuntimeError(f"Unknown Feishu WebSocket bot role: {role}")


def _sdk_event_payload(data, bot: WsBotConfig) -> dict:
    payload = json.loads(lark.JSON.marshal(data) or "{}")
    payload.setdefault("schema", "2.0")
    header = payload.setdefault("header", {})
    if isinstance(header, dict):
        header.setdefault("event_type", "im.message.receive_v1")
        if bot.verification_token:
            header["token"] = bot.verification_token
    return payload


if __name__ == "__main__":
    run_feishu_ws_bot()
