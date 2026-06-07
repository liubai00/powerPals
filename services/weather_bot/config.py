from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    qweather_api_key: str | None = None
    caiyun_api_key: str | None = None
    openclaw_api_url: str | None = None
    openclaw_api_key: str | None = None
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_default_chat_id: str | None = None
    feishu_bitable_app_token: str | None = None
    feishu_bitable_table_id: str | None = None
    local_jsonl_path: str = "data/weather_submissions.jsonl"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
