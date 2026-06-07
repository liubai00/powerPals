from __future__ import annotations

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


def build_feishu_card(submission: WeatherSubmission) -> dict:
    summary = submission.aggregated_forecast.summary
    content = build_text_summary(submission)
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"{submission.region}气象预测"},
            },
            "elements": [
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
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": submission.disclaimer}]},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": content[:900]}],
                },
            ],
        },
    }
