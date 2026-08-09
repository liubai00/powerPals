from __future__ import annotations

from dataclasses import dataclass
import json
import secrets
from typing import Iterable

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str
    roles: frozenset[str]


class AdminApiAuthenticator:
    """Fail-closed Bearer authentication for administrative HTTP APIs."""

    def __init__(
        self,
        token: str | None,
        *,
        actor_id: str | None = None,
        roles: Iterable[str] = (),
        environment: str = "local",
    ) -> None:
        self._token = token.strip() if token else None
        normalized_actor = (actor_id or "").strip()
        normalized_roles = frozenset(
            role.strip().casefold()
            for role in roles
            if isinstance(role, str) and role.strip()
        )
        if environment.strip().casefold() in {"local", "test"}:
            normalized_actor = normalized_actor or "local-admin"
            normalized_roles = normalized_roles or frozenset({"administrator"})
        self.principal = (
            AdminPrincipal(normalized_actor, normalized_roles)
            if normalized_actor and "administrator" in normalized_roles
            else None
        )

    async def __call__(self, request: Request) -> AdminPrincipal:
        scheme, _, supplied_token = request.headers.get("authorization", "").partition(" ")
        authorized = (
            bool(self._token)
            and self.principal is not None
            and scheme.lower() == "bearer"
            and bool(supplied_token)
            and secrets.compare_digest(supplied_token, self._token)
        )
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Administrative API authorization required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return self.principal


def parse_admin_roles(raw: str | None) -> tuple[str, ...]:
    """Parse role configuration without accepting malformed partial values."""
    try:
        decoded = json.loads((raw or "").strip() or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    if any(not isinstance(role, str) or not role.strip() for role in decoded):
        return ()
    normalized = tuple(role.strip().casefold() for role in decoded)
    if len(set(normalized)) != len(normalized):
        return ()
    return normalized


__all__ = ["AdminApiAuthenticator", "AdminPrincipal", "parse_admin_roles"]
