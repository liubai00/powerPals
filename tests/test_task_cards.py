from services.weather_bot.task_cards import build_task_card, build_task_text
from services.weather_bot.tasks import WeatherTaskService


def test_task_text_contains_required_community_rhythm_and_boundaries():
    task = WeatherTaskService().create_dayahead_task("2026-06-10")

    text = build_task_text(task)

    assert "【任务发布｜广东省深圳市气象预测】" in text
    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in text
    assert "D-1 16:00" in text
    assert "D-1 17:00" in text
    assert "weather_submission_v1" in text
    assert "不构成交易建议" in text
    assert "共建是宗旨" in text


def test_task_card_uses_feishu_interactive_shape():
    task = WeatherTaskService().create_dayahead_task("2026-06-10")

    card = build_task_card(task)

    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "广东省深圳市气象预测任务"
    content = card["card"]["elements"][0]["text"]["content"]
    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in content
    assert "提交截止" in content
    assert "22.5431, 114.0579" in content


def test_multi_day_task_card_shows_forecast_days_and_range():
    task = WeatherTaskService().create_dayahead_task("2026-06-10", days=3)

    card = build_task_card(task)

    content = card["card"]["elements"][0]["text"]["content"]
    assert "预测天数" in content
    assert "3 天" in content
    assert "2026-06-10T00:00:00+08:00 至 2026-06-12T23:00:00+08:00" in content
