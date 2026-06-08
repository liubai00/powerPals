import json
from pathlib import Path

from jsonschema import validate

from services.weather_bot.cards import build_feishu_card, build_text_summary
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


def make_submission() -> WeatherSubmission:
    return WeatherSubmission(
        task_id="WEATHER-CN-440300-20260610-DAYAHEAD-001",
        region="广东省深圳市",
        target_date="2026-06-10",
        data_cutoff_time="2026-06-09T16:00:00+08:00",
        provider_results=[],
        aggregated_forecast=AggregatedForecast(
            providers_used=["open_meteo", "qweather"],
            points=[
                ForecastPoint(
                    time="2026-06-10T00:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=60.0,
                )
            ],
            summary=ForecastSummary(
                max_temperature=32.5,
                min_temperature=27.4,
                rain_probability=45.0,
                wind_speed=3.2,
                cloud_cover=70.0,
                main_weather="多云，局部有阵雨",
                high_risk_period="14:00-18:00 局地降水不确定性较高",
            ),
        ),
        confidence={"score": 0.7, "description": "中等"},
        key_factors=["副热带高压影响", "午后对流活动"],
        risk_notes=["短时强降水可能导致局地误差放大"],
        disclaimer="本输出仅用于小可爱电力社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。",
    )


def test_text_summary_contains_required_fields_and_disclaimer():
    summary = build_text_summary(make_submission())

    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in summary
    assert "广东省深圳市" in summary
    assert "数据截止" in summary
    assert "不构成交易建议" in summary


def test_feishu_card_uses_message_card_shape():
    card = build_feishu_card(make_submission())

    assert card["msg_type"] == "interactive"
    assert "card" in card
    assert card["card"]["header"]["title"]["content"] == "广东省深圳市气象预测"


def test_feishu_card_embeds_chart_and_download_actions():
    card = build_feishu_card(
        make_submission(),
        report_url="https://powerpals.example.com/reports/weather?region=深圳&target_date=2026-06-10&days=1",
        download_url="https://powerpals.example.com/api/weather/export?region=深圳&target_date=2026-06-10&days=1",
        json_url="https://powerpals.example.com/api/weather/export/json?region=深圳&target_date=2026-06-10&days=1",
        chart_submissions=[make_submission()],
    )

    elements = card["card"]["elements"]
    charts = [element for element in elements if element.get("tag") == "chart"]
    assert len(charts) >= 2
    assert charts[0]["chart_spec"]["type"] == "line"
    assert charts[1]["chart_spec"]["type"] == "bar"
    assert "温度趋势" in charts[0]["chart_spec"]["title"]["text"]
    assert "降水概率" in charts[1]["chart_spec"]["title"]["text"]

    actions = next(element for element in elements if element.get("tag") == "action")["actions"]
    button_urls = {action["text"]["content"]: action["url"] for action in actions}
    assert button_urls["打开网页报告"].startswith("https://applink.feishu.cn/client/web_url/open")
    assert "下载CSV" in button_urls
    assert "下载JSON" in button_urls


def test_example_submission_matches_json_schema():
    schema = json.loads(Path("schemas/weather_submission_v1.schema.json").read_text(encoding="utf-8"))
    example = json.loads(Path("examples/weather_submission_shenzhen.json").read_text(encoding="utf-8"))

    validate(example, schema)


def test_example_submission_uses_document_style_official_fields():
    example = json.loads(Path("examples/weather_submission_shenzhen.json").read_text(encoding="utf-8"))

    assert example["submission_type"] == "official_submission"
    assert example["track"] == "weather_forecast"
    assert example["bot"]["bot_name"] == "PowerPals Weather Bot"
    assert example["scope"]["applicable_scenarios"]
    assert example["scope"]["location"]["code"] == "440300"
    assert example["time_info"]["data_cutoff_time"] == "2026-06-09T16:00:00+08:00"
    assert example["data_profile"]["data_source_group"] == "公开数据组"
    assert example["payload"]["summary"]["main_weather"]
    assert example["explanation"]["business_readable_summary"]
    assert example["scoring_profile"]["participate_in_public_scorecard"] is True
