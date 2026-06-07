from services.weather_bot.task_cards import build_task_card, build_task_text
from services.weather_bot.tasks import WeatherTaskService


def test_task_text_contains_required_community_rhythm_and_boundaries():
    task = WeatherTaskService().create_dayahead_task("2026-06-10")

    text = build_task_text(task)

    assert "【任务发布｜深圳气象预测】" in text
    assert "WEATHER-SZ-20260610-DAYAHEAD-001" in text
    assert "D-1 16:00" in text
    assert "D-1 17:00" in text
    assert "weather_submission_v1" in text
    assert "不构成交易建议" in text
    assert "共建是宗旨" in text


def test_task_card_uses_feishu_interactive_shape():
    task = WeatherTaskService().create_dayahead_task("2026-06-10")

    card = build_task_card(task)

    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "深圳气象预测任务"
    content = card["card"]["elements"][0]["text"]["content"]
    assert "WEATHER-SZ-20260610-DAYAHEAD-001" in content
    assert "提交截止" in content
