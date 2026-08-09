from services.weather_bot.subscription_commands import parse_subscription_command


def test_parse_daily_multi_province_briefing_draft() -> None:
    command = parse_subscription_command("每天8:30给我看山东、河南和河北")

    assert command is not None
    assert command.action == "create_draft"
    assert command.spec is not None
    assert command.spec.kind == "scheduled_briefing"
    assert command.spec.regions == ("山东", "河南", "河北")
    assert command.spec.schedule_time == "08:30"


def test_parse_threshold_alert_draft_with_hysteresis() -> None:
    command = parse_subscription_command("广东体感温度超过38℃时提醒我")

    assert command is not None
    assert command.action == "create_draft"
    assert command.spec is not None
    assert command.spec.kind == "threshold"
    assert command.spec.regions == ("广东",)
    assert command.spec.metric == "apparent_temperature"
    assert command.spec.operator == ">"
    assert command.spec.trigger_threshold == 38
    assert command.spec.recovery_threshold == 36


def test_only_exact_confirmation_phrase_can_request_activation() -> None:
    assert parse_subscription_command("可以") is None
    assert parse_subscription_command("好的") is None

    command = parse_subscription_command("确认订阅")
    assert command is not None
    assert command.action == "confirm"
    assert command.explicit_confirmation is True


def test_parse_threshold_update_and_idempotent_cancel_intents() -> None:
    update = parse_subscription_command("把阈值38℃改成39℃")
    cancel = parse_subscription_command("取消订阅")

    assert update is not None
    assert update.action == "update_threshold"
    assert update.new_threshold == 39
    assert cancel is not None
    assert cancel.action == "cancel"
