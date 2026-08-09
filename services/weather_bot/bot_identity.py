from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def mentions_expected_bot(
    mentions: object,
    *,
    expected_open_id: str | None,
    aliases: Iterable[str] = (),
    allow_name_fallback: bool = False,
) -> bool:
    """Return whether a Feishu mention explicitly targets the configured bot.

    Feishu display names are not identities. When an open_id is configured, only
    that exact structured identity is accepted. Name matching is available only
    as an explicit compatibility mode for environments that cannot yet provide
    bot identity configuration.
    """

    if not isinstance(mentions, list):
        return False

    configured_open_id = (expected_open_id or "").strip()
    if configured_open_id:
        return any(configured_open_id in _mention_open_ids(item) for item in mentions)

    if not allow_name_fallback:
        return False

    allowed_names = {_normalize_name(alias) for alias in aliases if _normalize_name(alias)}
    return any(_normalize_name(_mention_name(item)) in allowed_names for item in mentions)


def _mention_open_ids(mention: object) -> set[str]:
    if not isinstance(mention, Mapping):
        return set()

    values: set[str] = set()
    direct = mention.get("open_id")
    if isinstance(direct, str) and direct.strip():
        values.add(direct.strip())

    identity = mention.get("id")
    if isinstance(identity, Mapping):
        value = identity.get("open_id")
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    return values


def _mention_name(mention: Any) -> str:
    if not isinstance(mention, Mapping):
        return ""
    value = mention.get("name")
    return value if isinstance(value, str) else ""


def _normalize_name(value: str) -> str:
    return value.strip().lstrip("@").strip()
