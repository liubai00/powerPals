from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SendDecision:
    allowed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"sent": self.allowed, "reason": self.reason}


class AdminSendPolicy:
    """Fail-closed policy for external effects initiated by admin HTTP APIs."""

    def __init__(self, *, global_send_enabled: bool, dry_run: bool) -> None:
        self._global_send_enabled = global_send_enabled
        self._dry_run = dry_run

    def external_write_decision(self) -> SendDecision:
        if self._dry_run:
            return SendDecision(False, "dry_run")
        if not self._global_send_enabled:
            return SendDecision(False, "global_send_disabled")
        return SendDecision(True, "allowed")

    def card_send_decision(self, target_chat_id: str | None) -> SendDecision:
        external_decision = self.external_write_decision()
        if not external_decision.allowed:
            return external_decision
        if not target_chat_id:
            return SendDecision(False, "target_not_configured")
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
        return self.card_send_decision(target_chat_id)
