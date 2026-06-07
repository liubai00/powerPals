from services.weather_bot.feishu import task_bitable_fields
from services.weather_bot.tasks import WeatherTaskService


def test_task_bitable_fields_match_community_task_table():
    task = WeatherTaskService().publish(WeatherTaskService().create_dayahead_task("2026-06-10"))

    fields = task_bitable_fields(task)

    assert fields["task_id"] == "WEATHER-SZ-20260610-DAYAHEAD-001"
    assert fields["track"] == "weather_forecast"
    assert fields["region"] == "广东省深圳市"
    assert fields["target_date"] == "2026-06-10"
    assert fields["publish_time"] == "2026-06-09T09:00:00+08:00"
    assert fields["data_cutoff_time"] == "2026-06-09T16:00:00+08:00"
    assert fields["submission_deadline"] == "2026-06-09T17:00:00+08:00"
    assert fields["status"] == "published"
    assert fields["submission_format_version"] == "weather_submission_v1"
    assert fields["scoring_status"] == "not_started"
