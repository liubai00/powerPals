from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable


@dataclass(frozen=True)
class SendDecision:
    allowed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"sent": self.allowed, "reason": self.reason}


class AdminSendPolicy:
    """Fail-closed policy for external effects initiated by admin HTTP APIs."""

    def __init__(
        self,
        *,
        global_send_enabled: bool,
        dry_run: bool,
        admin_send_enabled: bool = False,
        admin_send_targets: Iterable[str] = (),
    ) -> None:
        self._global_send_enabled = global_send_enabled
        self._dry_run = dry_run
        self._admin_send_enabled = admin_send_enabled
        self._admin_send_targets = frozenset(
            target.strip()
            for target in admin_send_targets
            if isinstance(target, str) and target.strip()
        )

    def _global_send_decision(self) -> SendDecision:
        if self._dry_run:
            return SendDecision(False, "dry_run")
        if not self._global_send_enabled:
            return SendDecision(False, "global_send_disabled")
        return SendDecision(True, "allowed")

    def external_write_decision(self) -> SendDecision:
        global_decision = self._global_send_decision()
        if not global_decision.allowed:
            return global_decision
        if not self._admin_send_enabled:
            return SendDecision(False, "admin_api_send_disabled")
        return SendDecision(True, "allowed")

    def card_send_decision(self, target_chat_id: str | None) -> SendDecision:
        external_decision = self.external_write_decision()
        if not external_decision.allowed:
            return external_decision
        if not target_chat_id:
            return SendDecision(False, "target_not_configured")
        if target_chat_id not in self._admin_send_targets:
            return SendDecision(False, "target_not_allowlisted")
        return SendDecision(True, "allowed")

    def scheduled_card_send_decision(
        self,
        target_chat_id: str | None,
        *,
        schedule_send_enabled: bool,
    ) -> SendDecision:
        """Authorize a reviewed scheduled target through the global kill switch."""
        if not schedule_send_enabled:
            return SendDecision(False, "scheduled_send_disabled")
        global_decision = self._global_send_decision()
        if not global_decision.allowed:
            return global_decision
        if not target_chat_id:
            return SendDecision(False, "target_not_configured")
        return SendDecision(True, "allowed")


def parse_target_allowlist(raw: str | None) -> tuple[str, ...]:
    """Parse a JSON string list and fail closed for malformed configuration."""
    try:
        decoded = json.loads((raw or "").strip() or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    if any(not isinstance(target, str) or not target.strip() for target in decoded):
        return ()
    normalized = tuple(target.strip() for target in decoded)
    if len(set(normalized)) != len(normalized):
        return ()
    return normalized
