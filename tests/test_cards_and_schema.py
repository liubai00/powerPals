import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jsonschema import validate

from services.weather_bot.cards import build_feishu_card, build_text_summary, build_weather_comparison_card
from services.weather_bot.models import (
    AggregatedForecast,
    ForecastPoint,
    ForecastSummary,
    ScopeProfile,
    TimeInfo,
    WeatherSubmission,
)


def make_submission() -> WeatherSubmission:
    return WeatherSubmission(
        task_id="WEATHER-CN-440300-20260610-DAYAHEAD-001",
        region="广东省深圳市",
        target_date="2026-06-10",
        data_cutoff_time="2026-06-09T16:00:00+08:00",
        time_info=TimeInfo(
            retrieved_at="2026-06-09T15:42:00+08:00",
            business_submission_deadline="2026-06-09T16:00:00+08:00",
        ),
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


def card_text(card: dict) -> str:
    texts: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("tag") in {"lark_md", "plain_text"} and isinstance(value.get("content"), str):
                texts.append(value["content"])
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(card.get("card", {}).get("elements", []))
    return "\n".join(texts)


def card_elements_by_tag(card: dict, tag: str) -> list[dict]:
    """Collect nested Feishu elements without depending on the card grid layout."""
    matches: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("tag") == tag:
                matches.append(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(card.get("card", {}).get("elements", []))
    return matches


def test_text_summary_contains_required_fields_and_disclaimer():
    summary = build_text_summary(make_submission())

    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in summary
    assert "广东省深圳市" in summary
    assert "数据抓取时间：2026-06-09T15:42:00+08:00" in summary
    assert "业务提交截止：2026-06-09T16:00:00+08:00" in summary
    assert "数据截止" not in summary
    assert "不构成交易建议" in summary


def test_feishu_card_uses_message_card_shape():
    card = build_feishu_card(make_submission())

    assert card["msg_type"] == "interactive"
    assert "card" in card
    assert card["card"]["header"]["title"]["content"].endswith("广东省深圳市气象预测")


def test_feishu_card_marks_missing_retrieval_time_and_labels_business_deadline():
    submission = make_submission().model_copy(
        update={
            "time_info": TimeInfo(
                business_submission_deadline="2026-06-09T16:00:00+08:00",
            )
        }
    )

    text = card_text(build_feishu_card(submission))

    assert "抓取时间 未记录" in text
    assert "业务提交截止 06-09 16:00" in text
    assert "数据截止" not in text


def test_feishu_card_converts_utc_retrieval_time_to_shanghai_time():
    submission = make_submission().model_copy(
        update={
            "time_info": TimeInfo(
                retrieved_at="2026-06-09T07:42:00+00:00",
                business_submission_deadline="2026-06-09T16:00:00+08:00",
            )
        }
    )

    text = card_text(build_feishu_card(submission))

    assert "抓取时间 06-09 15:42" in text


def test_feishu_card_power_insight_only_describes_weather_proxies_and_verification_boundary():
    submission = make_submission()
    risky_summary = submission.aggregated_forecast.summary.model_copy(
        update={
            "max_temperature": 37.0,
            "wind_speed": 13.0,
            "rain_probability": 75.0,
            "cloud_cover": 85.0,
        }
    )
    submission = submission.model_copy(
        update={
            "aggregated_forecast": submission.aggregated_forecast.model_copy(
                update={"summary": risky_summary}
            )
        }
    )

    text = card_text(build_feishu_card(submission))

    assert "负荷天气压力代理" in text
    assert "10米地面风资源代理" in text
    assert "光资源代理" in text
    assert "实际负荷、风光出力、供需和价格方向待结合电力数据核查" in text
    for forbidden in (
        "供需偏紧",
        "现货承压",
        "现货或承压",
        "电价支撑",
        "电价有支撑",
        "风电出力",
        "光伏出力",
        "光伏大发",
    ):
        assert forbidden not in text


def test_feishu_card_neutral_power_insight_keeps_explicit_proxy_terms():
    submission = make_submission()
    neutral_summary = submission.aggregated_forecast.summary.model_copy(
        update={
            "max_temperature": 28.0,
            "wind_speed": 6.0,
            "rain_probability": 20.0,
            "cloud_cover": 60.0,
        }
    )
    submission = submission.model_copy(
        update={
            "aggregated_forecast": submission.aggregated_forecast.model_copy(
                update={"summary": neutral_summary}
            )
        }
    )

    text = card_text(build_feishu_card(submission))

    assert "负荷天气压力代理、10米地面风资源代理和光资源代理整体平稳" in text
    assert "实际负荷、风光出力、供需和价格方向待结合电力数据核查" in text


def test_feishu_card_labels_province_forecast_as_representative_point():
    submission = make_submission().model_copy(
        update={
            "region": "辽宁省",
            "scope": ScopeProfile(
                region="辽宁省",
                target_date="2026-06-10",
                location={
                    "name": "辽宁省",
                    "representation": "province_representative_point",
                    "representative_city": "沈阳市",
                },
            ),
        }
    )

    card = build_feishu_card(submission)

    assert card["card"]["header"]["title"]["content"].endswith("辽宁省（沈阳代表点）气象预测")


def test_feishu_card_defaults_to_instant_query_display_with_traceable_metadata():
    card = build_feishu_card(make_submission())
    text = card_text(card)

    assert "任务 ID" not in text
    assert "正式提交" not in text
    notes = card_elements_by_tag(card, "note")
    assert len(notes) == 1
    assert "抓取时间 06-09 15:42" in text
    assert "业务提交截止 06-09 16:00" in text
    assert "来源 open_meteo / qweather" in text


def test_feishu_card_uses_compact_forecast_body_and_power_data_boundary():
    card = build_feishu_card(make_submission())
    text = card_text(card)

    assert "32° / 27°" in text
    assert "💧45%" in text
    assert "💨3m/s" in text
    assert "关键指标" in text
    assert "高风险：14:00-18:00 局地降水不确定性较高" in text
    assert "负荷天气压力代理" in text
    assert "实际负荷、风光出力、供需和价格方向待结合电力数据核查" in text


def test_feishu_card_can_focus_on_requested_metric():
    card = build_feishu_card(make_submission(), metrics=["rain"])
    text = card_text(card)
    charts = card_elements_by_tag(card, "chart")

    assert "🌧️ 降水概率 %" in text
    assert "🌡️ 气温" not in text
    assert "💨 风" not in text
    assert len(charts) == 1
    assert charts[0]["chart_spec"]["type"] == "bar"
    assert charts[0]["chart_spec"]["yField"] == "rain_probability"


def test_feishu_card_shows_range_for_multi_day_predictions():
    first = make_submission()
    second = make_submission().model_copy(update={"target_date": "2026-06-11"})
    third = make_submission().model_copy(update={"target_date": "2026-06-12"})

    card = build_feishu_card(first, chart_submissions=[first, second, third])
    text = card_text(card)

    assert "2026-06-10 至 2026-06-12（3天）" in text
    assert "预测日" not in text


def test_weather_comparison_card_uses_native_table_rows():
    first = make_submission()
    second = make_submission().model_copy(update={"region": "广东省", "target_date": "2026-06-11"})

    card = build_weather_comparison_card([first, second])
    elements = card["card"]["elements"]
    rows = [element for element in elements if element.get("tag") == "column_set"]

    assert card["card"]["header"]["title"]["content"] == "多地区气象对比"
    assert rows
    assert rows[0]["background_style"] == "grey"
    assert rows[0]["columns"][0]["elements"][0]["text"]["content"] == "**日期**"
    assert "**广东省深圳市**" in str(card)
    assert "**广东省**" in str(card)
    assert "| 地区 |" not in str(card)


def test_weather_comparison_card_includes_report_and_download_actions():
    card = build_weather_comparison_card(
        [make_submission()],
        report_url=(
            "https://powerpals.example.com/reports/weather/compare?"
            "regions=广州,深圳&target_date=2026-06-10&days=3"
        ),
        download_url=(
            "https://powerpals.example.com/api/weather/compare/export?"
            "regions=广州,深圳&target_date=2026-06-10&days=3"
        ),
        json_url=(
            "https://powerpals.example.com/api/weather/compare/export/json?"
            "regions=广州,深圳&target_date=2026-06-10&days=3"
        ),
    )

    actions = next(element for element in card["card"]["elements"] if element.get("tag") == "action")["actions"]
    button_urls = {action["text"]["content"]: action["url"] for action in actions}
    assert button_urls["打开网页报告"].startswith("https://applink.feishu.cn/client/web_url/open")
    assert button_urls["下载CSV"].startswith("https://powerpals.example.com/api/weather/compare/export?")
    assert button_urls["下载JSON"].startswith("https://powerpals.example.com/api/weather/compare/export/json?")


def test_feishu_task_submission_card_shows_task_id_and_traceable_metadata():
    card = build_feishu_card(make_submission(), show_task_id=True)
    text = card_text(card)

    assert "任务 ID" in text
    assert "WEATHER-CN-440300-20260610-DAYAHEAD-001" in text
    assert "正式提交" not in text
    assert len(card_elements_by_tag(card, "note")) == 1
    assert "抓取时间 06-09 15:42" in text


def test_feishu_card_does_not_repeat_disclaimer_as_standalone_note():
    submission = make_submission()
    card = build_feishu_card(submission)

    note_texts = []
    for element in card["card"]["elements"]:
        if element.get("tag") != "note":
            continue
        note_texts.extend(item.get("content") for item in element.get("elements", []))

    assert submission.disclaimer not in note_texts


def test_feishu_card_embeds_chart_and_download_actions():
    card = build_feishu_card(
        make_submission(),
        report_url="https://powerpals.example.com/reports/weather?region=深圳&target_date=2026-06-10&days=1",
        download_url="https://powerpals.example.com/api/weather/export?region=深圳&target_date=2026-06-10&days=1",
        json_url="https://powerpals.example.com/api/weather/export/json?region=深圳&target_date=2026-06-10&days=1",
        chart_submissions=[make_submission()],
    )

    elements = card["card"]["elements"]
    charts = card_elements_by_tag(card, "chart")
    assert len(charts) >= 4
    charts_by_field = {chart["chart_spec"]["yField"]: chart for chart in charts}
    assert charts_by_field["temperature"]["chart_spec"]["type"] == "line"
    assert charts_by_field["rain_probability"]["chart_spec"]["type"] == "bar"
    assert charts_by_field["wind_speed"]["chart_spec"]["type"] == "line"
    assert charts_by_field["cloud_cover"]["chart_spec"]["type"] == "bar"
    text = card_text(card)
    assert "🌡️ 温度 ℃" in text
    assert "🌧️ 降水概率 %" in text
    assert "💨 风速 m/s" in text
    assert "☁️ 云量 %" in text
    for chart in charts[:4]:
        assert chart["chart_spec"]["tooltip"]["visible"] is True
        assert chart["chart_spec"]["label"]["visible"] is False
        assert chart["chart_spec"]["axes"][0]["label"]["visible"] is False

    actions = next(element for element in elements if element.get("tag") == "action")["actions"]
    button_urls = {action["text"]["content"]: action["url"] for action in actions}
    assert button_urls["打开网页报告"].startswith("https://applink.feishu.cn/client/web_url/open")
    assert parse_qs(urlparse(button_urls["打开网页报告"]).query)["mode"] == ["window"]
    assert button_urls["下载CSV"].startswith("https://powerpals.example.com/api/weather/export?")
    assert button_urls["下载JSON"].startswith("https://powerpals.example.com/api/weather/export/json?")


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
