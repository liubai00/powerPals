from __future__ import annotations

from urllib.parse import urlencode

from services.weather_bot.models import WeatherSubmission


def build_text_summary(submission: WeatherSubmission) -> str:
    summary = submission.aggregated_forecast.summary
    return "\n".join(
        [
            f"【正式提交｜{submission.region}气象预测】",
            "",
            f"任务 ID：{submission.task_id}",
            f"区域：{submission.region}",
            f"预测日：{submission.target_date}",
            f"数据截止：{submission.data_cutoff_time}",
            f"数据来源：{' / '.join(submission.aggregated_forecast.providers_used)}",
            "",
            "核心结果：",
            f"- 最高温：{summary.max_temperature}℃",
            f"- 最低温：{summary.min_temperature}℃",
            f"- 降水概率：{summary.rain_probability}%",
            f"- 风速：{summary.wind_speed} m/s",
            f"- 云量：{summary.cloud_cover}%",
            f"- 主要天气：{summary.main_weather}",
            f"- 高风险时段：{summary.high_risk_period}",
            "",
            "主要影响因素：",
            *[f"{index}. {factor}" for index, factor in enumerate(submission.key_factors, start=1)],
            "",
            "风险提示：",
            *[f"- {note}" for note in submission.risk_notes],
            "",
            f"免责声明：{submission.disclaimer}",
        ]
    )


def build_feishu_card(
    submission: WeatherSubmission,
    report_url: str | None = None,
    download_url: str | None = None,
    json_url: str | None = None,
    chart_submissions: list[WeatherSubmission] | None = None,
) -> dict:
    summary = submission.aggregated_forecast.summary
    content = build_text_summary(submission)
    chart_items = chart_submissions or [submission]
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务 ID**：{submission.task_id}\n"
                    f"**区域**：{submission.region}\n"
                    f"**预测日**：{submission.target_date}\n"
                    f"**数据截止**：{submission.data_cutoff_time}\n"
                    f"**数据来源**：{' / '.join(submission.aggregated_forecast.providers_used)}"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**最高/最低温**：{summary.max_temperature}℃ / {summary.min_temperature}℃\n"
                    f"**降水概率**：{summary.rain_probability}%\n"
                    f"**风速**：{summary.wind_speed} m/s\n"
                    f"**云量**：{summary.cloud_cover}%\n"
                    f"**主要天气**：{summary.main_weather}\n"
                    f"**高风险时段**：{summary.high_risk_period}"
                ),
            },
        },
    ]
    elements.extend(_weather_chart_elements(chart_items))
    actions = []
    if report_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开网页报告"},
                "url": feishu_webview_url(report_url),
                "type": "primary",
            }
        )
    if download_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "下载CSV"},
                "url": download_url,
                "type": "default",
            }
        )
    if json_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "下载JSON"},
                "url": json_url,
                "type": "default",
            }
        )
    if actions:
        elements.extend([{"tag": "hr"}, {"tag": "action", "actions": actions}])
    elements.extend(
        [
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": submission.disclaimer}]},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": content[:900]}],
            },
        ]
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"{submission.region}气象预测"},
            },
            "elements": elements,
        },
    }


def feishu_webview_url(url: str) -> str:
    query = urlencode({"url": url, "mode": "sidebar-semi"})
    return f"https://applink.feishu.cn/client/web_url/open?{query}"


def _weather_chart_elements(submissions: list[WeatherSubmission]) -> list[dict]:
    temperature_values = []
    rain_values = []
    for submission in submissions:
        summary = submission.aggregated_forecast.summary
        label = submission.target_date[5:] if len(submission.target_date) >= 10 else submission.target_date
        if summary.max_temperature is not None:
            temperature_values.append({"date": label, "type": "最高温", "value": summary.max_temperature})
        if summary.min_temperature is not None:
            temperature_values.append({"date": label, "type": "最低温", "value": summary.min_temperature})
        if summary.rain_probability is not None:
            rain_values.append({"date": label, "rain_probability": summary.rain_probability})

    elements = []
    if temperature_values:
        elements.append(
            {
                "tag": "chart",
                "aspect_ratio": "16:9",
                "chart_spec": {
                    "type": "line",
                    "title": {"text": "温度趋势（℃）"},
                    "data": {"values": temperature_values},
                    "xField": "date",
                    "yField": "value",
                    "seriesField": "type",
                    "legends": {"visible": True},
                },
            }
        )
    if rain_values:
        elements.append(
            {
                "tag": "chart",
                "aspect_ratio": "16:9",
                "chart_spec": {
                    "type": "bar",
                    "title": {"text": "降水概率（%）"},
                    "data": {"values": rain_values},
                    "xField": "date",
                    "yField": "rain_probability",
                    "axes": [{"orient": "left", "min": 0, "max": 100}],
                },
            }
        )
    return elements
