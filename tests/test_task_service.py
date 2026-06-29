from services.weather_bot.tasks import WeatherTaskService


def test_weather_task_service_builds_doc_rhythm_task_window():
    task = WeatherTaskService().create_dayahead_task("2026-06-10")

    assert task.task_id == "WEATHER-CN-440300-20260610-DAYAHEAD-001"
    assert task.track == "weather_forecast"
    assert task.region == "广东省深圳市"
    assert task.location_code == "440300"
    assert task.latitude == 22.5431
    assert task.longitude == 114.0579
    assert task.status == "draft"
    assert task.forecast_start == "2026-06-10T00:00:00+08:00"
    assert task.forecast_end == "2026-06-10T23:00:00+08:00"
    assert task.forecast_days == 1
    assert task.publish_time == "2026-06-09T09:00:00+08:00"
    assert task.data_cutoff_time == "2026-06-09T16:00:00+08:00"
    assert task.reminder_time == "2026-06-09T16:30:00+08:00"
    assert task.submission_deadline == "2026-06-09T17:00:00+08:00"
    assert task.submission_format_version == "weather_submission_v1"
    assert task.scoring_status == "not_started"


def test_weather_task_service_supports_multi_day_window():
    task = WeatherTaskService().create_dayahead_task("2026-06-10", days=3)

    assert task.task_id == "WEATHER-CN-440300-20260610-DAYAHEAD-001"
    assert task.forecast_days == 3
    assert task.forecast_start == "2026-06-10T00:00:00+08:00"
    assert task.forecast_end == "2026-06-12T23:00:00+08:00"


def test_weather_task_state_transitions_keep_review_rhythm():
    service = WeatherTaskService()
    task = service.create_dayahead_task("2026-06-10")

    published = service.publish(task)
    reminded = service.remind(published)
    closed = service.close(reminded)

    assert published.status == "published"
    assert reminded.status == "published"
    assert "D-1 17:00" in reminded.notes
    assert closed.status == "closed"
    assert closed.scoring_status == "waiting_truth"
