from __future__ import annotations

from services.weather_bot.tasks import WeatherTask


def build_task_text(task: WeatherTask) -> str:
    return "\n".join(
        [
            f"【任务发布｜{task.region}气象预测】",
            "",
            f"任务 ID：{task.task_id}",
            f"赛道：{task.track}",
            f"区域：{task.region}",
            f"经纬度：{task.latitude}, {task.longitude}",
            f"预测日期：{task.target_date}",
            f"预测天数：{task.forecast_days} 天",
            f"预测范围：{task.forecast_start} 至 {task.forecast_end}",
            f"任务发布时间：{task.publish_time}",
            f"数据截止：{task.data_cutoff_time}（D-1 16:00）",
            f"提交截止：{task.submission_deadline}（D-1 17:00）",
            f"提交格式：{task.submission_format_version}",
            "",
            "提交要求：",
            "1. 使用标准 JSON 结构化提交。",
            "2. 必须包含 Bot 名称、版本、数据来源、数据截止时间和免责声明。",
            "3. 输出应说明温度、降水概率、风速、云量和关键不确定性。",
            "4. 可补充对负荷、新能源、电价和储能观察的参考价值。",
            "",
            "任务机器人作用：发布任务、统一口径、提醒提交、关闭窗口、记录状态，为后续裁判 Bot 评分和复盘留痕。",
            "社区口径：共建是宗旨，共测是机制，评分是工具，复盘是方法，成长是结果。",
            "免责声明：本任务和 Bot 输出仅用于社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。",
        ]
    )


def build_task_card(task: WeatherTask) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "turquoise",
                "title": {"tag": "plain_text", "content": f"{task.region}气象预测任务"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**任务 ID**：{task.task_id}\n"
                            f"**区域**：{task.region}\n"
                            f"**经纬度**：{task.latitude}, {task.longitude}\n"
                            f"**预测日期**：{task.target_date}\n"
                            f"**预测天数**：{task.forecast_days} 天\n"
                            f"**预测范围**：{task.forecast_start} 至 {task.forecast_end}\n"
                            f"**数据截止**：{task.data_cutoff_time}\n"
                            f"**提交截止**：{task.submission_deadline}\n"
                            f"**提交格式**：{task.submission_format_version}"
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**提交要求**：标准 JSON + 飞书摘要；必须声明数据来源、"
                            "数据截止、适用边界、风险提示和免责声明。"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "任务机器人负责发布、提醒、关闭和记录任务，不负责虚构天气数据或替代裁判评分。",
                        }
                    ],
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "共建是宗旨，共测是机制，评分是工具，复盘是方法，成长是结果。",
                        }
                    ],
                },
            ],
        },
    }
