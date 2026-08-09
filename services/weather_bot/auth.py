from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status


class AdminApiAuthenticator:
    """Fail-closed Bearer authentication for administrative HTTP APIs."""

    def __init__(self, token: str | None) -> None:
        self._token = token.strip() if token else None

    async def __call__(self, request: Request) -> None:
        scheme, _, supplied_token = request.headers.get("authorization", "").partition(" ")
        authorized = (
            bool(self._token)
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
