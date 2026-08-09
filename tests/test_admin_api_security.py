import pytest
from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.main import create_app
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ProviderForecast,
    WeatherSubmission,
)


class FakeForecastService:
    def __init__(self) -> None:
        self.call_count = 0

    async def forecast(self, request) -> WeatherSubmission:
        self.call_count += 1
        return WeatherSubmission(
            task_id="WEATHER-CN-440300-20260810-DAYAHEAD-001",
            region="Shenzhen",
            target_date="2026-08-10",
            data_cutoff_time="2026-08-09T16:00:00+08:00",
            provider_results=[
                ProviderForecast(
                    provider="open_meteo",
                    retrieved_at="2026-08-09T00:00:00+00:00",
                    source_url="https://weather.example.test/v1/forecast",
                    content_sha256="a" * 64,
                    retention_policy="derived_only",
                    retention_expires_at="2026-08-12T00:00:00+00:00",
                )
            ],
            aggregated_forecast=AggregatedForecast(
                providers_used=["open_meteo"],
                points=[
                    ForecastPoint(
                        time="2026-08-10T00:00:00+08:00",
                        temperature=28.0,
                        precipitation_probability=20.0,
                        wind_speed=2.0,
                        cloud_cover=60.0,
                    )
                ],
                summary=ForecastSummary(
                    max_temperature=28.0,
                    min_temperature=28.0,
                    rain_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=60.0,
                    main_weather="cloudy",
                    high_risk_period="none",
                ),
            ),
            confidence={"score": 0.7, "description": "medium"},
            key_factors=["multi-source forecast"],
            risk_notes=["forecast uncertainty"],
            retention_policy="derived_only",
            retention_expires_at="2026-08-12T00:00:00+00:00",
            disclaimer="weather information only",
        )


def test_unauthenticated_task_publish_is_rejected_without_writing(tmp_path) -> None:
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/tasks/weather/publish",
        json={"target_date": "2026-08-10"},
    )

    assert response.status_code == 401
    assert not task_log.exists()


def test_unauthenticated_weather_publish_is_rejected_before_external_or_local_writes(
    monkeypatch,
    tmp_path,
) -> None:
    external_writes: list[str] = []

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_writes.append(submission.task_id)

    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    service = FakeForecastService()
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/api/weather/publish",
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 401
    assert service.call_count == 0
    assert external_writes == []
    assert not submission_log.exists()


def test_unauthenticated_task_reminder_is_rejected_before_send_or_write(monkeypatch, tmp_path) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, task) -> None:
        external_effects.append(f"bitable:{task.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_task_bitable_record", record_bitable_write)
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        feishu_task_default_chat_id="oc_test",
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/tasks/weather/remind",
        json={"target_date": "2026-08-10"},
    )

    assert response.status_code == 401
    assert external_effects == []
    assert not task_log.exists()


def test_authenticated_weather_publish_only_generates_when_global_send_is_disabled(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{submission.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=False,
        feishu_weather_default_chat_id="oc_test",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {"sent": False, "reason": "global_send_disabled"}
    assert external_effects == []
    assert not submission_log.exists()


def test_global_send_enable_does_not_authorize_admin_publish_without_admin_opt_in(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{submission.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=True,
        feishu_weather_default_chat_id="oc_test",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {
        "sent": False,
        "reason": "admin_api_send_disabled",
    }
    assert external_effects == []
    assert not submission_log.exists()


def test_admin_publish_refuses_a_target_outside_the_reviewed_allowlist(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{submission.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_reviewed"]',
        admin_api_audit_db=str(tmp_path / "admin_actions.db"),
        global_feishu_send_enabled=True,
        feishu_weather_default_chat_id="oc_unreviewed",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={
            "Authorization": "Bearer test-admin-token",
            "Idempotency-Key": "weather-publish-unreviewed-target-001",
        },
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["delivery"] == {
        "sent": False,
        "reason": "target_not_allowlisted",
    }
    assert external_effects == ["bitable:WEATHER-CN-440300-20260810-DAYAHEAD-001"]
    assert submission_log.exists()


def test_authenticated_task_publish_only_generates_when_global_send_is_disabled(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, task) -> None:
        external_effects.append(f"bitable:{task.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_task_bitable_record", record_bitable_write)
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=False,
        feishu_task_default_chat_id="oc_test",
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/tasks/weather/publish",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {"sent": False, "reason": "global_send_disabled"}
    assert external_effects == []
    assert not task_log.exists()


def test_authenticated_task_reminder_only_generates_when_global_send_is_disabled(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, task) -> None:
        external_effects.append(f"bitable:{task.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_task_bitable_record", record_bitable_write)
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=False,
        feishu_task_default_chat_id="oc_test",
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/tasks/weather/remind",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {"sent": False, "reason": "global_send_disabled"}
    assert external_effects == []
    assert not task_log.exists()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/weather/submission", {}),
        ("POST", "/api/locations", {}),
        ("DELETE", "/api/locations/test-alias", None),
        ("POST", "/api/news/items", {}),
        ("POST", "/api/hydrology/records", {}),
        ("POST", "/api/tasks/weather/create", {"target_date": "2026-08-10"}),
        ("POST", "/api/tasks/weather/close", {"target_date": "2026-08-10"}),
    ],
)
def test_other_unauthenticated_admin_mutations_are_rejected_before_validation_or_write(
    method,
    path,
    payload,
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        local_jsonl_path=str(tmp_path / "weather_submissions.jsonl"),
        local_task_jsonl_path=str(tmp_path / "weather_tasks.jsonl"),
        local_locations_path=str(tmp_path / "locations.json"),
        local_news_jsonl_path=str(tmp_path / "news.jsonl"),
        local_hydrology_jsonl_path=str(tmp_path / "hydrology.jsonl"),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.request(method, path, json=payload)

    assert response.status_code == 401
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("configured_token", "authorization"),
    [
        (None, None),
        ("test-admin-token", "Bearer wrong-token"),
        ("test-admin-token", "Basic test-admin-token"),
    ],
)
def test_admin_api_fails_closed_for_missing_configuration_or_invalid_credentials(
    configured_token,
    authorization,
) -> None:
    service = FakeForecastService()
    settings = Settings(_env_file=None, admin_api_token=configured_token)
    client = TestClient(create_app(forecast_service=service, settings=settings))
    headers = {"Authorization": authorization} if authorization else {}

    response = client.post(
        "/api/weather/publish",
        headers=headers,
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert service.call_count == 0


def test_production_admin_token_without_actor_and_role_binding_is_rejected() -> None:
    service = FakeForecastService()
    settings = Settings(
        _env_file=None,
        app_env="production",
        admin_api_token="test-admin-token",
        admin_api_actor_id=None,
        admin_api_roles_json="[]",
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 401
    assert service.call_count == 0


def test_admin_action_audit_records_bound_actor_and_role_without_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    import sqlite3

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        return None

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    audit_db = tmp_path / "admin_actions.db"
    settings = Settings(
        _env_file=None,
        app_env="production",
        admin_api_token="secret-production-token",
        admin_api_actor_id="weather-ops-01",
        admin_api_roles_json='["administrator"]',
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_reviewed"]',
        admin_api_audit_db=str(audit_db),
        global_feishu_send_enabled=True,
        feishu_weather_default_chat_id="oc_reviewed",
        local_jsonl_path=str(tmp_path / "submissions.jsonl"),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))
    idempotency_key = "weather-publish-audit-001"

    response = client.post(
        "/api/weather/publish",
        headers={
            "Authorization": "Bearer secret-production-token",
            "Idempotency-Key": idempotency_key,
        },
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    with sqlite3.connect(audit_db) as connection:
        actor_id, role, outcome = connection.execute(
            """
            SELECT actor_id, role, outcome
            FROM admin_action_audit
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert (actor_id, role, outcome) == ("weather-ops-01", "administrator", "succeeded")
    raw_db = audit_db.read_bytes()
    assert b"secret-production-token" not in raw_db
    assert idempotency_key.encode("utf-8") not in raw_db


def test_authenticated_weather_publish_sends_only_with_explicit_global_enable(monkeypatch, tmp_path) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{card_message_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_test"]',
        admin_api_audit_db=str(tmp_path / "admin_actions.db"),
        global_feishu_send_enabled=True,
        feishu_weather_default_chat_id="oc_test",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={
            "Authorization": "Bearer test-admin-token",
            "Idempotency-Key": "weather-publish-reviewed-target-001",
        },
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["delivery"] == {"sent": True, "reason": "allowed"}
    assert external_effects == ["send:oc_test", "bitable:message-id"]
    assert submission_log.exists()


def test_dry_run_overrides_explicit_global_send_enable(monkeypatch, tmp_path) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card) -> str:
        external_effects.append(f"send:{chat_id}")
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{submission.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=True,
        dry_run=True,
        feishu_weather_default_chat_id="oc_test",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {"sent": False, "reason": "dry_run"}
    assert external_effects == []
    assert not submission_log.exists()


def test_authenticated_direct_submission_respects_global_effects_gate(monkeypatch, tmp_path) -> None:
    external_effects: list[str] = []

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{submission.task_id}")

    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=False,
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))
    forecast_response = client.post(
        "/api/weather/forecast",
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    response = client.post(
        "/api/weather/submission",
        headers={"Authorization": "Bearer test-admin-token"},
        json=forecast_response.json(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {
        "sent": False,
        "reason": "global_send_disabled",
    }
    assert external_effects == []
    assert not submission_log.exists()


def test_authenticated_task_close_respects_global_effects_gate(monkeypatch, tmp_path) -> None:
    external_effects: list[str] = []

    async def record_task_write(self, task) -> None:
        external_effects.append(f"bitable:{task.task_id}")

    monkeypatch.setattr(FeishuClient, "write_task_bitable_record", record_task_write)
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        global_feishu_send_enabled=False,
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/tasks/weather/close",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"region": "深圳", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["delivery"] == {
        "sent": False,
        "reason": "global_send_disabled",
    }
    assert external_effects == []
    assert not task_log.exists()


def test_weather_publish_replays_one_completed_idempotent_admin_action(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        external_effects.append(f"send:{chat_id}:{idempotency_key}")
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        external_effects.append(f"bitable:{card_message_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    submission_log = tmp_path / "weather_submissions.jsonl"
    audit_db = tmp_path / "admin_actions.db"
    service = FakeForecastService()
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_reviewed"]',
        admin_api_audit_db=str(audit_db),
        global_feishu_send_enabled=True,
        feishu_weather_default_chat_id="oc_reviewed",
        local_jsonl_path=str(submission_log),
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))
    headers = {
        "Authorization": "Bearer test-admin-token",
        "Idempotency-Key": "weather-publish-20260810-001",
    }
    payload = {"region": "Shenzhen", "target_date": "2026-08-10"}

    first = client.post("/api/weather/publish", headers=headers, json=payload)
    second = client.post("/api/weather/publish", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {
        "status": "published",
        "task_id": first.json()["submission"]["task_id"],
        "delivery": {"sent": True, "reason": "allowed"},
        "idempotent_replay": True,
    }
    assert service.call_count == 1
    assert len([item for item in external_effects if item.startswith("send:")]) == 1
    assert external_effects.count("bitable:message-id") == 1
    assert len(submission_log.read_text(encoding="utf-8").splitlines()) == 1
    assert audit_db.exists()


def test_weather_publish_honors_explicit_local_idempotency_compatibility_mode(
    monkeypatch,
    tmp_path,
) -> None:
    sends: list[str] = []

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        sends.append(chat_id)
        return "message-id"

    async def record_bitable_write(self, submission, card_message_id=None) -> None:
        return None

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_bitable_record", record_bitable_write)
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_reviewed"]',
        admin_api_idempotency_required=False,
        global_feishu_send_enabled=True,
        feishu_weather_default_chat_id="oc_reviewed",
        local_jsonl_path=str(tmp_path / "submissions.jsonl"),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))

    response = client.post(
        "/api/weather/publish",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"region": "Shenzhen", "target_date": "2026-08-10"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert sends == ["oc_reviewed"]


def test_task_publish_replays_one_completed_idempotent_admin_action(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        external_effects.append(f"send:{chat_id}:{idempotency_key}")
        return "task-message-id"

    async def record_bitable_write(self, task) -> None:
        external_effects.append(f"bitable:{task.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_task_bitable_record", record_bitable_write)
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_reviewed_task"]',
        admin_api_audit_db=str(tmp_path / "admin_actions.db"),
        global_feishu_send_enabled=True,
        feishu_task_default_chat_id="oc_reviewed_task",
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))
    headers = {
        "Authorization": "Bearer test-admin-token",
        "Idempotency-Key": "task-publish-20260810-001",
    }
    payload = {"region": "深圳", "target_date": "2026-08-10"}

    first = client.post("/api/tasks/weather/publish", headers=headers, json=payload)
    second = client.post("/api/tasks/weather/publish", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {
        "status": "published",
        "task_id": first.json()["task"]["task_id"],
        "delivery": {"sent": True, "reason": "allowed"},
        "idempotent_replay": True,
    }
    assert len([item for item in external_effects if item.startswith("send:")]) == 1
    assert len([item for item in external_effects if item.startswith("bitable:")]) == 1
    assert len(task_log.read_text(encoding="utf-8").splitlines()) == 1


def test_task_reminder_replays_one_completed_idempotent_admin_action(
    monkeypatch,
    tmp_path,
) -> None:
    external_effects: list[str] = []

    async def record_send(self, chat_id, card, *, idempotency_key=None) -> str:
        external_effects.append(f"send:{chat_id}:{idempotency_key}")
        return "reminder-message-id"

    async def record_bitable_write(self, task) -> None:
        external_effects.append(f"bitable:{task.task_id}")

    monkeypatch.setattr(FeishuClient, "send_interactive_card", record_send)
    monkeypatch.setattr(FeishuClient, "write_task_bitable_record", record_bitable_write)
    task_log = tmp_path / "weather_tasks.jsonl"
    settings = Settings(
        _env_file=None,
        admin_api_token="test-admin-token",
        admin_api_send_enabled=True,
        admin_api_send_targets_json='["oc_reviewed_task"]',
        admin_api_audit_db=str(tmp_path / "admin_actions.db"),
        global_feishu_send_enabled=True,
        feishu_task_default_chat_id="oc_reviewed_task",
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=FakeForecastService(), settings=settings))
    headers = {
        "Authorization": "Bearer test-admin-token",
        "Idempotency-Key": "task-reminder-20260810-001",
    }
    payload = {"region": "深圳", "target_date": "2026-08-10"}

    first = client.post("/api/tasks/weather/remind", headers=headers, json=payload)
    second = client.post("/api/tasks/weather/remind", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {
        "status": "published",
        "task_id": first.json()["task"]["task_id"],
        "delivery": {"sent": True, "reason": "allowed"},
        "idempotent_replay": True,
    }
    assert len([item for item in external_effects if item.startswith("send:")]) == 1
    assert len([item for item in external_effects if item.startswith("bitable:")]) == 1
    assert len(task_log.read_text(encoding="utf-8").splitlines()) == 1
