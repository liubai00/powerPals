from __future__ import annotations

import json
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, model_validator

from services.weather_bot.data_provenance import AllowedUse, LicenseStatus


class SourcePolicy(BaseModel):
    """Explicit, environment-scoped permission for one external source profile."""

    provider: str
    environment: str
    profile: str
    license_status: LicenseStatus = "unknown"
    allowed_uses: set[AllowedUse] = Field(default_factory=set)
    terms_version: str | None = None
    source_url_prefixes: tuple[str, ...] = ()
    unit_manifest: str | None = None
    required_metrics: tuple[str, ...] = ()
    coverage_model: str | None = None
    timezone: str | None = None
    max_age_seconds: int | None = Field(default=None, gt=0)
    min_completeness: float = Field(default=0.95, ge=0.0, le=1.0)
    retention_policy: Literal["derived_only", "metadata_only"] = "derived_only"
    retention_seconds: int | None = Field(default=None, gt=0)
    attribution_required: bool = False
    attribution_text: str | None = None

    @model_validator(mode="after")
    def validate_explicit_verified_policy(self) -> "SourcePolicy":
        if self.license_status == "verified":
            required = {
                "terms_version": self.terms_version,
                "source_url_prefixes": self.source_url_prefixes,
                "unit_manifest": self.unit_manifest,
                "required_metrics": self.required_metrics,
                "coverage_model": self.coverage_model,
                "timezone": self.timezone,
                "max_age_seconds": self.max_age_seconds,
                "retention_seconds": self.retention_seconds,
            }
            missing = [name for name, value in required.items() if not value]
            if "retention_policy" not in self.model_fields_set:
                missing.append("retention_policy")
            if missing:
                raise ValueError(f"verified source policy missing: {', '.join(missing)}")
            unit_metrics = {
                metric.strip()
                for item in (self.unit_manifest or "").split(";")
                for metric, separator, _unit in [item.partition(":")]
                if separator and metric.strip()
            }
            metrics_without_units = [
                metric for metric in self.required_metrics if metric not in unit_metrics
            ]
            if metrics_without_units:
                raise ValueError(
                    f"unit_manifest missing metrics: {', '.join(metrics_without_units)}"
                )
        if "raw_storage" in self.allowed_uses:
            raise ValueError("raw_storage is not supported by the weather service")
        if self.attribution_required and not (self.attribution_text or "").strip():
            raise ValueError("attribution_text is required when attribution_required is true")
        return self

    @classmethod
    def unconfigured(cls, provider: str, environment: str) -> "SourcePolicy":
        return cls(
            provider=provider,
            environment=environment,
            profile="unconfigured",
            license_status="unknown",
        )


class SourceRegistry:
    def __init__(self, policies: list[SourcePolicy] | None = None, *, environment: str) -> None:
        self.environment = environment
        self._policies = {
            policy.provider: policy
            for policy in (policies or [])
            if policy.environment == environment
        }

    @classmethod
    def from_json(cls, raw: str, *, environment: str) -> "SourceRegistry":
        try:
            payload = json.loads(raw or "[]")
            policies = [SourcePolicy.model_validate(item) for item in payload] if isinstance(payload, list) else []
        except (TypeError, ValueError):
            policies = []
        return cls(policies, environment=environment)

    def resolve(self, provider: str, source_url: str | None) -> SourcePolicy:
        policy = self._policies.get(provider)
        if policy is None or not source_url:
            return SourcePolicy.unconfigured(provider, self.environment)
        if not any(_same_host_prefix(source_url, prefix) for prefix in policy.source_url_prefixes):
            return SourcePolicy.unconfigured(provider, self.environment)
        return policy

    def policy(self, provider: str) -> SourcePolicy:
        """Return the environment-scoped policy without authorizing an endpoint."""

        return self._policies.get(provider) or SourcePolicy.unconfigured(
            provider,
            self.environment,
        )

    def disclosure_label(self, provider: str, source_url: str | None) -> str:
        policy = self.resolve(provider, source_url)
        if policy.attribution_required and policy.attribution_text:
            return f"{provider} — {policy.attribution_text}"
        return provider


def same_source_endpoint(left: str, right: str) -> bool:
    """Compare query-free web endpoints after authority/path normalization."""

    normalized_left = _normalized_web_url(left, allow_query=False)
    normalized_right = _normalized_web_url(right, allow_query=False)
    return bool(
        normalized_left is not None
        and normalized_right is not None
        and normalized_left == normalized_right
    )


def _same_host_prefix(source_url: str, prefix: str) -> bool:
    source = _normalized_web_url(source_url, allow_query=True)
    approved = _normalized_web_url(prefix, allow_query=False)
    if source is None or approved is None:
        return False
    source_scheme, source_host, source_port, source_path = source
    approved_scheme, approved_host, approved_port, approved_path = approved
    if (
        source_scheme != approved_scheme
        or source_host != approved_host
        or source_port != approved_port
    ):
        return False
    if approved_path == "/":
        return True
    return source_path == approved_path or source_path.startswith(f"{approved_path}/")


def _normalized_web_url(
    value: str,
    *,
    allow_query: bool,
) -> tuple[str, str, int, str] | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    if any(ord(character) < 32 for character in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.fragment or (parsed.query and not allow_query):
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        host = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        path = unquote(parsed.path or "/", errors="strict")
    except (UnicodeError, ValueError):
        return None
    if "%" in path or "\\" in path or any(ord(character) < 32 for character in path):
        return None
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return None
    normalized_path = path.rstrip("/") or "/"
    return scheme, host, port, normalized_path
