from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    qweather_api_key: str | None = None
    qweather_api_host: str | None = None
    caiyun_api_key: str | None = None
    openclaw_api_url: str | None = None
    openclaw_api_key: str | None = None
    llm_api_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout: float = 60.0
    tavily_api_key: str | None = None
    feishu_progress_message_enabled: bool = False
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_default_chat_id: str | None = None
    feishu_weather_app_id: str | None = None
    feishu_weather_app_secret: str | None = None
    feishu_weather_verification_token: str | None = None
    feishu_weather_encrypt_key: str | None = None
    feishu_weather_default_chat_id: str | None = None
    feishu_task_app_id: str | None = None
    feishu_task_app_secret: str | None = None
    feishu_task_verification_token: str | None = None
    feishu_task_encrypt_key: str | None = None
    feishu_task_default_chat_id: str | None = None
    feishu_bitable_app_token: str | None = None
    feishu_bitable_table_id: str | None = None
    feishu_task_bitable_table_id: str | None = None
    local_jsonl_path: str = "data/weather_submissions.jsonl"
    local_task_jsonl_path: str = "data/weather_tasks.jsonl"
    local_locations_path: str = "data/locations.json"
    local_news_jsonl_path: str = "data/news_items.jsonl"
    local_hydrology_jsonl_path: str = "data/hydrology_records.jsonl"
    public_base_url: str | None = None
    default_weather_region: str = "广东省深圳市"
    default_weather_latitude: float | None = None
    default_weather_longitude: float | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("default_weather_latitude", "default_weather_longitude", mode="before")
    @classmethod
    def _empty_coordinate_as_none(cls, value: object) -> object:
        if value == "":
            return None
        return value
