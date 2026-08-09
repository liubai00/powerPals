from services.weather_bot.auth import parse_admin_roles
from services.weather_bot.send_policy import parse_target_allowlist


def test_admin_roles_fail_closed_on_any_invalid_or_duplicate_member() -> None:
    assert parse_admin_roles('["administrator", 1]') == ()
    assert parse_admin_roles('["administrator", "administrator"]') == ()


def test_admin_target_allowlist_fails_closed_on_any_invalid_or_duplicate_member() -> None:
    assert parse_target_allowlist('["oc_reviewed", null]') == ()
    assert parse_target_allowlist('["oc_reviewed", "oc_reviewed"]') == ()
