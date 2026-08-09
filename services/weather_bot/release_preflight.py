"""Offline, secret-safe release readiness checks.

This module never calls a provider, model, search service, Feishu, or a server.
It translates the documented production hard gates into a machine-readable
decision.  Human approval references remain external deployment evidence; the
preflight only verifies that current, non-secret references were supplied.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal, Mapping

from services.weather_bot.auth import parse_admin_roles
from services.weather_bot.config import Settings
from services.weather_bot.llm import LlmClient
from services.weather_bot.openclaw import OpenClawExplainer
from services.weather_bot.source_registry import SourcePolicy


ReleasePhase = Literal["shadow", "passive", "scheduled"]
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@dataclass(frozen=True)
class PreflightCheck:
    code: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ReleasePreflightResult:
    phase: ReleasePhase
    checks: tuple[PreflightCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def status(self) -> str:
        return "READY" if self.ready else "BLOCKED"

    @property
    def failed_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ready": self.ready,
            "phase": self.phase,
            "failed_codes": list(self.failed_codes),
            "checks": [check.as_dict() for check in self.checks],
        }


def evaluate_release_preflight(
    settings: Settings,
    *,
    phase: ReleasePhase,
    evidence: Mapping[str, object] | None,
    now: datetime | None = None,
) -> ReleasePreflightResult:
    """Evaluate release gates without exposing configured values in the result."""

    if phase not in {"shadow", "passive", "scheduled"}:
        raise ValueError(f"unsupported release phase: {phase}")
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("preflight now must be timezone-aware")

    policies = _reviewed_source_policies(settings)
    target_count = _reviewed_scheduled_target_count(settings.power_briefing_targets_json)
    checks = [
        PreflightCheck(
            "production_environment",
            settings.app_env.strip().casefold() in {"production", "staging"},
            "runtime environment is production-scoped"
            if settings.app_env.strip().casefold() in {"production", "staging"}
            else "runtime environment is not production-scoped",
        ),
        PreflightCheck(
            "source_policies_reviewed",
            bool(policies),
            "all configured sources are explicit, verified, finite-retention policies"
            if policies
            else "no complete reviewed calculation source policy is configured",
        ),
        PreflightCheck(
            "admin_identity_bound",
            bool(
                (settings.admin_api_token or "").strip()
                and (settings.admin_api_actor_id or "").strip()
                and "administrator" in parse_admin_roles(settings.admin_api_roles_json)
                and settings.admin_api_idempotency_required
            ),
            "management credential is actor/role bound and idempotency is mandatory"
            if (
                (settings.admin_api_token or "").strip()
                and (settings.admin_api_actor_id or "").strip()
                and "administrator" in parse_admin_roles(settings.admin_api_roles_json)
                and settings.admin_api_idempotency_required
            )
            else "management actor, administrator role, token, or mandatory idempotency is missing",
        ),
        PreflightCheck(
            "feishu_bot_identities_bound",
            _role_bot_identity_complete(settings, "weather")
            and _role_bot_identity_complete(settings, "task"),
            "both bot roles have callback credentials and exact open_id bindings"
            if (
                _role_bot_identity_complete(settings, "weather")
                and _role_bot_identity_complete(settings, "task")
            )
            else "one or more bot roles lack callback credentials or exact open_id binding",
        ),
        PreflightCheck(
            "model_selection_reviewed",
            (settings.llm_model or "").strip() == "gpt-5.6-sol",
            "configured model is gpt-5.6-sol"
            if (settings.llm_model or "").strip() == "gpt-5.6-sol"
            else "configured model is not the reviewed gpt-5.6-sol model",
        ),
        PreflightCheck(
            "external_ai_egress_reviewed",
            _external_ai_egress_reviewed(settings),
            "every enabled external AI endpoint has an exact reviewed HTTPS prefix"
            if _external_ai_egress_reviewed(settings)
            else "an enabled external AI endpoint, credential, model, or HTTPS prefix is unreviewed",
        ),
        _runtime_capability_check(settings, phase),
        _phase_effect_check(settings, phase),
        _target_check(phase, target_count, evidence),
        _evidence_check(evidence, policies, observed_at),
    ]
    return ReleasePreflightResult(phase=phase, checks=tuple(checks))


def _reviewed_source_policies(settings: Settings) -> tuple[SourcePolicy, ...]:
    try:
        decoded = json.loads(settings.weather_source_policies_json or "[]")
        if not isinstance(decoded, list) or not decoded:
            return ()
        policies = tuple(SourcePolicy.model_validate(item) for item in decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if any(
        policy.environment != settings.app_env
        or policy.license_status != "verified"
        or policy.retention_seconds is None
        or policy.retention_policy not in {"derived_only", "metadata_only"}
        for policy in policies
    ):
        return ()
    if not any("calculation" in policy.allowed_uses for policy in policies):
        return ()
    return policies


def _role_bot_identity_complete(settings: Settings, role: Literal["weather", "task"]) -> bool:
    app_id = getattr(settings, f"feishu_{role}_app_id") or settings.feishu_app_id
    app_secret = getattr(settings, f"feishu_{role}_app_secret") or settings.feishu_app_secret
    verification_token = (
        getattr(settings, f"feishu_{role}_verification_token")
        or settings.feishu_verification_token
    )
    open_id = getattr(settings, f"feishu_{role}_bot_open_id") or settings.feishu_bot_open_id
    return all(str(value or "").strip() for value in (app_id, app_secret, verification_token, open_id))


def _external_ai_egress_reviewed(settings: Settings) -> bool:
    review_settings = settings.model_copy(update={"dry_run": False})
    if settings.llm_egress_enabled:
        llm_client = LlmClient.from_settings(review_settings)
        if not llm_client.enabled:
            return False
    if settings.openclaw_egress_enabled:
        explainer = OpenClawExplainer.from_settings(review_settings)
        if not (
            explainer.egress_allowed
            and (settings.openclaw_api_url or "").strip()
            and (settings.openclaw_api_key or "").strip()
        ):
            return False
    return True


def _phase_effect_check(settings: Settings, phase: ReleasePhase) -> PreflightCheck:
    proactive_disabled = (
        not settings.admin_api_send_enabled
        and not settings.power_briefing_allow_send
        and not settings.alert_send_enabled
        and not settings.legacy_weather_scheduler_enabled
    )
    if phase == "shadow":
        passed = bool(
            settings.dry_run
            and not settings.global_feishu_send_enabled
            and not settings.feishu_passive_reply_enabled
            and proactive_disabled
        )
        return PreflightCheck(
            "shadow_external_effects_disabled",
            passed,
            "shadow phase has zero reply, send, scheduler, and admin effects"
            if passed
            else "shadow phase does not have every external-effect veto enabled",
        )
    if phase == "passive":
        passed = bool(
            not settings.dry_run
            and not settings.global_feishu_send_enabled
            and settings.feishu_passive_reply_enabled
            and proactive_disabled
        )
        return PreflightCheck(
            "passive_effects_scoped",
            passed,
            "only authenticated passive replies are enabled"
            if passed
            else "passive phase enables a proactive path or does not enable replies",
        )
    passed = bool(
        not settings.dry_run
        and settings.global_feishu_send_enabled
        and settings.power_briefing_allow_send
        and not settings.admin_api_send_enabled
        and not settings.alert_send_enabled
        and not settings.legacy_weather_scheduler_enabled
    )
    return PreflightCheck(
        "scheduled_effects_scoped",
        passed,
        "only the reviewed scheduled briefing path is proactively enabled"
        if passed
        else "scheduled phase switches are incomplete or another proactive path is enabled",
    )


def _runtime_capability_check(
    settings: Settings,
    phase: ReleasePhase,
) -> PreflightCheck:
    actual = (
        bool(settings.electricity_weather_analysis_enabled),
        bool(settings.manual_power_briefing_enabled),
        bool(settings.subscriptions_enabled),
        bool(settings.alert_evaluation_enabled),
        bool(settings.external_data_workbench_enabled),
    )
    expected = (
        (True, False, False, False, False)
        if phase == "passive"
        else (False, False, False, False, False)
    )
    passed = actual == expected
    return PreflightCheck(
        "runtime_capabilities_scoped",
        passed,
        "runtime capabilities match the reviewed phase"
        if passed
        else "one or more runtime capabilities exceed the reviewed phase",
    )


def _reviewed_scheduled_target_count(raw: str | None) -> int | None:
    try:
        decoded = json.loads((raw or "").strip() or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list):
        return None
    seen: set[str] = set()
    for item in decoded:
        if not isinstance(item, dict):
            return None
        chat_id = item.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.strip() or chat_id.strip() in seen:
            return None
        seen.add(chat_id.strip())
    return len(seen)


def _target_check(
    phase: ReleasePhase,
    target_count: int | None,
    evidence: Mapping[str, object] | None,
) -> PreflightCheck:
    if phase != "scheduled":
        passed = target_count is not None
        return PreflightCheck(
            "target_configuration_valid",
            passed,
            "target configuration is syntactically valid"
            if passed
            else "target configuration is malformed or contains duplicates",
        )
    approvals = evidence.get("target_approval_references") if isinstance(evidence, Mapping) else None
    passed = bool(
        target_count == 1
        and isinstance(approvals, list)
        and len(approvals) == 1
        and isinstance(approvals[0], str)
        and approvals[0].strip()
    )
    return PreflightCheck(
        "scheduled_target_reviewed",
        passed,
        "exactly one initial scheduled target has an external approval reference"
        if passed
        else "scheduled target is invalid, duplicated, not singular, or lacks approval evidence",
    )


def _evidence_check(
    evidence: Mapping[str, object] | None,
    policies: tuple[SourcePolicy, ...],
    now: datetime,
) -> PreflightCheck:
    if not isinstance(evidence, Mapping):
        return PreflightCheck(
            "release_evidence_current",
            False,
            "release manifest with backup, rollback, monitoring, and approval references is missing",
        )
    approvals = evidence.get("source_approval_references")
    reviewed_at = _aware_datetime(evidence.get("reviewed_at"))
    expires_at = _aware_datetime(evidence.get("expires_at"))
    passed = bool(
        _REVISION_RE.fullmatch(str(evidence.get("release_revision") or ""))
        and _REVISION_RE.fullmatch(str(evidence.get("previous_stable_revision") or ""))
        and _SHA256_RE.fullmatch(str(evidence.get("config_sha256") or ""))
        and str(evidence.get("backup_reference") or "").strip()
        and str(evidence.get("monitoring_owner") or "").strip()
        and str(evidence.get("rollback_owner") or "").strip()
        and isinstance(approvals, list)
        and len(approvals) >= len(policies) > 0
        and all(isinstance(item, str) and item.strip() for item in approvals)
        and reviewed_at is not None
        and expires_at is not None
        and reviewed_at <= now < expires_at
    )
    return PreflightCheck(
        "release_evidence_current",
        passed,
        "release, backup, rollback, monitoring, and source approvals are current"
        if passed
        else "release evidence is missing, incomplete, stale, or does not cover configured sources",
    )


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _load_evidence(path: str | None) -> Mapping[str, object] | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            decoded = json.load(handle)
    except (OSError, TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline weather-agent release preflight")
    parser.add_argument("--phase", choices=("shadow", "passive", "scheduled"), required=True)
    parser.add_argument("--evidence", help="Path to an external non-secret release manifest")
    args = parser.parse_args(argv)
    result = evaluate_release_preflight(
        Settings(),
        phase=args.phase,
        evidence=_load_evidence(args.evidence),
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PreflightCheck",
    "ReleasePreflightResult",
    "evaluate_release_preflight",
    "main",
]
