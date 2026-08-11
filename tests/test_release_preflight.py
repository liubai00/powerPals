from __future__ import annotations

from datetime import datetime, timezone
import json

from services.weather_bot.config import Settings
from services.weather_bot.release_preflight import evaluate_release_preflight


def _verified_open_meteo_policy() -> dict[str, object]:
    return {
        "provider": "open_meteo",
        "environment": "production",
        "profile": "reviewed-commercial-profile",
        "license_status": "verified",
        "allowed_uses": ["calculation", "derived_storage"],
        "terms_version": "approval-ticket-2026-08",
        "source_url_prefixes": ["https://api.open-meteo.com/v1/forecast"],
        "unit_manifest": (
            "temperature:C;precipitation_probability:percent;wind_speed:km/h;"
            "cloud_cover:percent;apparent_temperature:C;wind_direction:degree;"
            "uv_index:index;shortwave_radiation:W/m2"
        ),
        "required_metrics": [
            "temperature",
            "precipitation_probability",
            "wind_speed",
            "cloud_cover",
            "apparent_temperature",
            "wind_direction",
            "uv_index",
            "shortwave_radiation",
        ],
        "coverage_model": "representative_point",
        "timezone": "Asia/Shanghai",
        "max_age_seconds": 3600,
        "min_completeness": 0.95,
        "retention_policy": "derived_only",
        "retention_seconds": 86400,
    }


def _release_evidence() -> dict[str, object]:
    return {
        "release_revision": "a" * 40,
        "previous_stable_revision": "b" * 40,
        "config_sha256": "c" * 64,
        "backup_reference": "backup-ticket-20260809",
        "monitoring_owner": "weather-oncall",
        "rollback_owner": "weather-release-manager",
        "source_approval_references": ["source-approval-20260809"],
        "target_approval_references": [],
        "reviewed_at": "2026-08-09T01:00:00+00:00",
        "expires_at": "2026-08-16T01:00:00+00:00",
    }


def _shadow_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "admin_api_token": "injected-secret",
        "admin_api_actor_id": "weather-ops",
        "admin_api_roles_json": '["administrator"]',
        "admin_api_send_enabled": False,
        "global_feishu_send_enabled": False,
        "dry_run": True,
        "feishu_passive_reply_enabled": False,
        "electricity_weather_analysis_enabled": False,
        "manual_power_briefing_enabled": False,
        "subscriptions_enabled": False,
        "alert_evaluation_enabled": False,
        "feishu_weather_app_id": "weather-app",
        "feishu_weather_app_secret": "injected-weather-secret",
        "feishu_weather_verification_token": "injected-weather-token",
        "feishu_weather_bot_open_id": "ou_weather_bot",
        "feishu_task_app_id": "task-app",
        "feishu_task_app_secret": "injected-task-secret",
        "feishu_task_verification_token": "injected-task-token",
        "feishu_task_bot_open_id": "ou_task_bot",
        "power_briefing_allow_send": False,
        "power_briefing_targets_json": "[]",
        "legacy_weather_scheduler_enabled": False,
        "alert_send_enabled": False,
        "weather_source_policies_json": json.dumps([_verified_open_meteo_policy()]),
        "llm_model": "gpt-5.6-sol",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_default_production_configuration_is_explicitly_blocked() -> None:
    result = evaluate_release_preflight(
        Settings(_env_file=None, app_env="production"),
        phase="shadow",
        evidence=None,
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert result.ready is False
    assert result.status == "BLOCKED"
    assert {
        "source_policies_reviewed",
        "admin_identity_bound",
        "feishu_bot_identities_bound",
        "shadow_external_effects_disabled",
        "release_evidence_current",
    }.issubset(set(result.failed_codes))


def test_fully_evidenced_shadow_configuration_passes_without_exposing_secrets() -> None:
    settings = _shadow_settings()

    result = evaluate_release_preflight(
        settings,
        phase="shadow",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert result.ready is True
    assert result.status == "READY"
    serialized = json.dumps(result.as_dict(), ensure_ascii=False)
    for secret in (
        settings.admin_api_token,
        settings.feishu_weather_app_secret,
        settings.feishu_weather_verification_token,
        settings.feishu_task_app_secret,
        settings.feishu_task_verification_token,
    ):
        assert secret not in serialized


def test_scheduled_phase_fails_closed_for_duplicate_or_unapproved_targets() -> None:
    settings = _shadow_settings(
        dry_run=False,
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json=json.dumps(
            [
                {"name": "group-a", "chat_id": "oc_duplicate"},
                {"name": "group-b", "chat_id": "oc_duplicate"},
            ]
        ),
    )
    evidence = _release_evidence()
    evidence["target_approval_references"] = ["target-approval-1"]

    result = evaluate_release_preflight(
        settings,
        phase="scheduled",
        evidence=evidence,
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert result.ready is False
    assert "scheduled_target_reviewed" in result.failed_codes
    assert "oc_duplicate" not in json.dumps(result.as_dict())


def test_preflight_rejects_enabled_external_ai_with_unreviewed_endpoint() -> None:
    settings = _shadow_settings(
        llm_egress_enabled=True,
        llm_api_base_url="https://llm.reviewed.example.evil/v1",
        llm_api_key="injected-llm-secret",
        llm_allowed_https_prefixes_json='["https://llm.reviewed.example/v1"]',
    )

    result = evaluate_release_preflight(
        settings,
        phase="shadow",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert result.ready is False
    assert "external_ai_egress_reviewed" in result.failed_codes
    assert "llm.reviewed.example" not in json.dumps(result.as_dict())


def test_preflight_requires_phase_scoped_runtime_capabilities() -> None:
    shadow = evaluate_release_preflight(
        _shadow_settings(manual_power_briefing_enabled=True),
        phase="shadow",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )
    passive = evaluate_release_preflight(
        _shadow_settings(
            dry_run=False,
            feishu_passive_reply_enabled=True,
            electricity_weather_analysis_enabled=True,
        ),
        phase="passive",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert "runtime_capabilities_scoped" in shadow.failed_codes
    assert "runtime_capabilities_scoped" not in passive.failed_codes


def test_afternoon_briefing_send_is_blocked_outside_the_scheduled_phase() -> None:
    shadow = evaluate_release_preflight(
        _shadow_settings(power_briefing_afternoon_allow_send=True),
        phase="shadow",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )
    passive = evaluate_release_preflight(
        _shadow_settings(
            dry_run=False,
            feishu_passive_reply_enabled=True,
            electricity_weather_analysis_enabled=True,
            power_briefing_afternoon_allow_send=True,
        ),
        phase="passive",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert "shadow_external_effects_disabled" in shadow.failed_codes
    assert "passive_effects_scoped" in passive.failed_codes


def test_scheduled_phase_can_review_morning_and_afternoon_briefing_paths_together() -> None:
    evidence = _release_evidence()
    evidence["target_approval_references"] = ["target-approval-20260809"]
    result = evaluate_release_preflight(
        _shadow_settings(
            dry_run=False,
            global_feishu_send_enabled=True,
            power_briefing_allow_send=True,
            power_briefing_afternoon_allow_send=True,
            power_briefing_targets_json=(
                '[{"chat_id":"oc_reviewed","approval_reference":'
                '"target-approval-20260809"}]'
            ),
        ),
        phase="scheduled",
        evidence=evidence,
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert "scheduled_effects_scoped" not in result.failed_codes


def test_preflight_rejects_legacy_external_data_workbench_in_every_phase() -> None:
    for phase in ("shadow", "passive", "scheduled"):
        overrides: dict[str, object] = {
            "external_data_workbench_enabled": True,
        }
        if phase == "passive":
            overrides.update(
                dry_run=False,
                feishu_passive_reply_enabled=True,
                electricity_weather_analysis_enabled=True,
            )
        elif phase == "scheduled":
            overrides.update(
                dry_run=False,
                global_feishu_send_enabled=True,
                power_briefing_allow_send=True,
                power_briefing_targets_json=(
                    '[{"chat_id":"oc_reviewed","approval_reference":'
                    '"target-approval-20260809"}]'
                ),
            )

        result = evaluate_release_preflight(
            _shadow_settings(**overrides),
            phase=phase,
            evidence=_release_evidence(),
            now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
        )

        assert "runtime_capabilities_scoped" in result.failed_codes


def test_learning_report_send_requires_scheduled_phase_and_explicit_test_target_approval() -> None:
    passive = evaluate_release_preflight(
        _shadow_settings(
            dry_run=False,
            feishu_passive_reply_enabled=True,
            electricity_weather_analysis_enabled=True,
            controlled_learning_report_send_enabled=True,
            controlled_learning_report_chat_name="test",
        ),
        phase="passive",
        evidence=_release_evidence(),
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )
    scheduled_settings = _shadow_settings(
        dry_run=False,
        global_feishu_send_enabled=True,
        power_briefing_allow_send=True,
        power_briefing_targets_json='[{"name":"briefing","chat_id":"oc_reviewed"}]',
        controlled_learning_report_send_enabled=True,
        controlled_learning_report_chat_name="test",
    )
    evidence = _release_evidence()
    evidence["target_approval_references"] = ["briefing-target-approval"]
    missing_approval = evaluate_release_preflight(
        scheduled_settings,
        phase="scheduled",
        evidence=evidence,
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )
    evidence["learning_report_target_approval_reference"] = "test-group-approved-by-owner"
    approved = evaluate_release_preflight(
        scheduled_settings,
        phase="scheduled",
        evidence=evidence,
        now=datetime(2026, 8, 9, 2, tzinfo=timezone.utc),
    )

    assert "learning_report_scope_reviewed" in passive.failed_codes
    assert "learning_report_scope_reviewed" in missing_approval.failed_codes
    assert "learning_report_scope_reviewed" not in approved.failed_codes
