from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    admin_api_token: str | None = None
    admin_api_actor_id: str | None = None
    admin_api_roles_json: str = "[]"
    admin_api_send_enabled: bool = False
    admin_api_send_targets_json: str = "[]"
    admin_api_audit_db: str = "data/admin_api_audit.db"
    admin_api_idempotency_required: bool = True
    global_feishu_send_enabled: bool = False
    dry_run: bool = False
    feishu_passive_reply_enabled: bool = True
    electricity_weather_analysis_enabled: bool = False
    manual_power_briefing_enabled: bool = False
    subscriptions_enabled: bool = False
    alert_evaluation_enabled: bool = False
    external_data_workbench_enabled: bool = False
    conversation_history_enabled: bool = False
    conversation_history_ttl_seconds: int = 30 * 60
    conversation_history_max_turns: int = 6
    qweather_api_key: str | None = None
    qweather_api_host: str | None = None
    caiyun_api_key: str | None = None
    openclaw_api_url: str | None = None
    openclaw_api_key: str | None = None
    openclaw_egress_enabled: bool = False
    openclaw_allowed_https_prefixes_json: str = "[]"
    llm_api_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = "gpt-5.6-sol"
    llm_egress_enabled: bool = False
    llm_allowed_https_prefixes_json: str = "[]"
    llm_timeout: float = 60.0
    tavily_api_key: str | None = None
    feishu_progress_message_enabled: bool = False
    feishu_allow_unsigned_events: bool = False
    feishu_internal_timeout_seconds: float = 600.0
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_default_chat_id: str | None = None
    feishu_bot_open_id: str | None = None
    feishu_allow_legacy_name_mentions: bool = False
    feishu_weather_app_id: str | None = None
    feishu_weather_app_secret: str | None = None
    feishu_weather_verification_token: str | None = None
    feishu_weather_encrypt_key: str | None = None
    feishu_weather_default_chat_id: str | None = None
    feishu_weather_bot_open_id: str | None = None
    feishu_task_app_id: str | None = None
    feishu_task_app_secret: str | None = None
    feishu_task_verification_token: str | None = None
    feishu_task_encrypt_key: str | None = None
    feishu_task_default_chat_id: str | None = None
    feishu_task_bot_open_id: str | None = None
    feishu_bitable_app_token: str | None = None
    feishu_bitable_table_id: str | None = None
    feishu_task_bitable_table_id: str | None = None
    local_jsonl_path: str = "data/weather_submissions.jsonl"
    local_task_jsonl_path: str = "data/weather_tasks.jsonl"
    local_locations_path: str = "data/locations.json"
    local_news_jsonl_path: str = "data/news_items.jsonl"
    local_hydrology_jsonl_path: str = "data/hydrology_records.jsonl"
    power_briefing_cache_db: str = "data/power_briefing_cache.db"
    power_briefing_cache_ttl_seconds: int = 86400
    power_briefing_allow_send: bool = False
    power_briefing_targets_json: str = "[]"
    legacy_weather_scheduler_enabled: bool = False
    controlled_learning_enabled: bool = False
    controlled_learning_db: str = "data/controlled_learning.db"
    controlled_learning_report_dir: str = "data/controlled_learning/reports"
    controlled_learning_truth_delay_days: int = 1
    controlled_learning_min_provider_samples: int = 5
    controlled_learning_archive_api_url: str = "https://archive-api.open-meteo.com/v1/archive"
    weather_source_policies_json: str = "[]"
    subscriptions_db: str = "data/subscriptions.db"
    alerts_db: str = "data/alerts.db"
    alert_send_enabled: bool = False
    subscription_admin_open_ids_json: str = "[]"
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
