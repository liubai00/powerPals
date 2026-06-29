from __future__ import annotations

from urllib.parse import urlencode

from services.weather_bot.models import WeatherSubmission
from services.weather_bot.weather_metrics import SUPPORTED_WEATHER_METRIC_ORDER, normalize_weather_metrics


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
    show_task_id: bool = False,
    include_submission_note: bool = False,
    metrics: list[str] | None = None,
) -> dict:
    chart_items = chart_submissions or [submission]
    selected_metrics = normalize_weather_metrics(metrics)
    date_labels = [item.target_date for item in chart_items]
    date_label = (
        f"{date_labels[0]} 至 {date_labels[-1]}（{len(date_labels)}天）"
        if len(date_labels) > 1
        else submission.target_date
    )
    metadata_lines = [
        f"**区域**：{submission.region}",
        f"**预测范围**：{date_label}" if len(date_labels) > 1 else f"**预测日**：{date_label}",
        f"**数据截止**：{submission.data_cutoff_time}",
        f"**数据来源**：{' / '.join(submission.aggregated_forecast.providers_used)}",
    ]
    if show_task_id:
        metadata_lines.insert(0, f"**任务 ID**：{submission.task_id}")
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(metadata_lines),
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": _forecast_detail_content(submission, selected_metrics),
            },
        },
    ]
    chart_elements = _hourly_chart_elements(chart_items, selected_metrics)
    elements.extend(chart_elements or _weather_chart_elements(chart_items, selected_metrics))
    actions = []
    if report_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开网页报告"},
                "url": feishu_webview_url(report_url, mode="window"),
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
    if include_submission_note:
        content = build_text_summary(submission)
        elements.extend(
            [
                {"tag": "hr"},
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


def build_weather_comparison_card(
    submissions: list[WeatherSubmission],
    metrics: list[str] | None = None,
    report_url: str | None = None,
    download_url: str | None = None,
    json_url: str | None = None,
) -> dict:
    selected_metrics = normalize_weather_metrics(metrics)
    regions = _ordered_unique([submission.region for submission in submissions])
    dates = _ordered_unique([submission.target_date for submission in submissions])
    date_label = (
        f"{dates[0]} 至 {dates[-1]}（{len(dates)}天）"
        if len(dates) > 1
        else dates[0]
        if dates
        else "暂无日期"
    )
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(
                    [
                        f"**对比地区**：{' / '.join(regions) if regions else '暂无地区'}",
                        f"**预测范围**：{date_label}",
                    ]
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": _comparison_insight_content(submissions, selected_metrics)},
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**对比明细：**"},
        },
    ]
    elements.extend(_comparison_table_elements(submissions, selected_metrics))
    elements.extend(_comparison_chart_elements(submissions, selected_metrics))
    actions = []
    if report_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开网页报告"},
                "url": feishu_webview_url(report_url, mode="window"),
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
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "多地区气象对比"},
            },
            "elements": elements,
        },
    }


def feishu_webview_url(url: str, mode: str = "window") -> str:
    query = urlencode({"url": url, "mode": mode})
    return f"https://applink.feishu.cn/client/web_url/open?{query}"


def _forecast_detail_content(submission: WeatherSubmission, metrics: list[str] | None = None) -> str:
    summary = submission.aggregated_forecast.summary
    selected = set(normalize_weather_metrics(metrics))
    all_metrics = set(SUPPORTED_WEATHER_METRIC_ORDER)
    lines = []
    if "temperature" in selected:
        lines.extend([f"- 最高温：{summary.max_temperature}℃", f"- 最低温：{summary.min_temperature}℃"])
    if "rain" in selected:
        lines.append(f"- 降水概率：{summary.rain_probability}%")
    if "wind" in selected:
        lines.append(f"- 风速：{summary.wind_speed} m/s")
    if "cloud" in selected:
        lines.append(f"- 云量：{summary.cloud_cover}%")
    if selected & {"rain", "cloud"}:
        lines.append(f"- 主要天气：{summary.main_weather}")
    if selected & {"rain", "wind"} or selected == all_metrics:
        lines.append(f"- 高风险时段：{summary.high_risk_period}")
    lines.extend(
        [
            "",
            "**主要影响因素：**",
            *_numbered_lines(submission.key_factors),
            "",
            "**风险提示：**",
            *_bullet_lines(submission.risk_notes),
        ]
    )
    return "\n".join(lines)


def _numbered_lines(items: list[str]) -> list[str]:
    if not items:
        return ["1. 暂无补充影响因素"]
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def _bullet_lines(items: list[str]) -> list[str]:
    if not items:
        return ["- 暂无补充风险提示"]
    return [f"- {item}" for item in items]


def _weather_chart_elements(submissions: list[WeatherSubmission], metrics: list[str] | None = None) -> list[dict]:
    selected = set(normalize_weather_metrics(metrics))
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
    if "temperature" in selected and temperature_values:
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
    if "rain" in selected and rain_values:
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


def _hourly_chart_elements(submissions: list[WeatherSubmission], metrics: list[str] | None = None) -> list[dict]:
    selected = set(normalize_weather_metrics(metrics))
    temperature_values = []
    rain_values = []
    wind_values = []
    cloud_values = []
    for submission in submissions:
        for point in submission.aggregated_forecast.points:
            if _hourly_metric_limit_reached(selected, temperature_values, rain_values, wind_values, cloud_values):
                break
            label = _hour_label(point.time)
            if "temperature" in selected and point.temperature is not None and len(temperature_values) < 96:
                temperature_values.append({"time": label, "temperature": point.temperature})
            if "rain" in selected and point.precipitation_probability is not None and len(rain_values) < 96:
                rain_values.append({"time": label, "rain_probability": point.precipitation_probability})
            if "wind" in selected and point.wind_speed is not None and len(wind_values) < 96:
                wind_values.append({"time": label, "wind_speed": point.wind_speed})
            if "cloud" in selected and point.cloud_cover is not None and len(cloud_values) < 96:
                cloud_values.append({"time": label, "cloud_cover": point.cloud_cover})
        if _hourly_metric_limit_reached(selected, temperature_values, rain_values, wind_values, cloud_values):
            break

    elements = []
    if "temperature" in selected and temperature_values:
        elements.append(
            {
                "tag": "chart",
                "aspect_ratio": "16:9",
                "chart_spec": {
                    "type": "line",
                    "title": {"text": "小时温度趋势（℃）"},
                    "data": {"values": temperature_values},
                    "xField": "time",
                    "yField": "temperature",
                    "tooltip": {"visible": True},
                    "label": {"visible": False},
                    "axes": [
                        {"orient": "bottom", "label": {"visible": False}, "tick": {"visible": False}},
                        {"orient": "left"},
                    ],
                },
            }
        )
    if "rain" in selected and rain_values:
        elements.append(
            {
                "tag": "chart",
                "aspect_ratio": "16:9",
                "chart_spec": {
                    "type": "bar",
                    "title": {"text": "小时降水概率（%）"},
                    "data": {"values": rain_values},
                    "xField": "time",
                    "yField": "rain_probability",
                    "tooltip": {"visible": True},
                    "label": {"visible": False},
                    "axes": [
                        {"orient": "bottom", "label": {"visible": False}, "tick": {"visible": False}},
                        {"orient": "left", "min": 0, "max": 100},
                    ],
                },
            }
        )
    if "wind" in selected and wind_values:
        elements.append(
            {
                "tag": "chart",
                "aspect_ratio": "16:9",
                "chart_spec": {
                    "type": "line",
                    "title": {"text": "小时风速趋势（m/s）"},
                    "data": {"values": wind_values},
                    "xField": "time",
                    "yField": "wind_speed",
                    "tooltip": {"visible": True},
                    "label": {"visible": False},
                    "axes": [
                        {"orient": "bottom", "label": {"visible": False}, "tick": {"visible": False}},
                        {"orient": "left"},
                    ],
                },
            }
        )
    if "cloud" in selected and cloud_values:
        elements.append(
            {
                "tag": "chart",
                "aspect_ratio": "16:9",
                "chart_spec": {
                    "type": "bar",
                    "title": {"text": "小时云量（%）"},
                    "data": {"values": cloud_values},
                    "xField": "time",
                    "yField": "cloud_cover",
                    "tooltip": {"visible": True},
                    "label": {"visible": False},
                    "axes": [
                        {"orient": "bottom", "label": {"visible": False}, "tick": {"visible": False}},
                        {"orient": "left", "min": 0, "max": 100},
                    ],
                },
            }
        )
    return elements


def _hourly_metric_limit_reached(
    selected: set[str],
    temperature_values: list[dict],
    rain_values: list[dict],
    wind_values: list[dict],
    cloud_values: list[dict],
) -> bool:
    values_by_metric = {
        "temperature": temperature_values,
        "rain": rain_values,
        "wind": wind_values,
        "cloud": cloud_values,
    }
    return all(len(values_by_metric[metric]) >= 96 for metric in selected)


def _hour_label(value: str) -> str:
    if "T" not in value:
        return value
    date_part, time_part = value.split("T", 1)
    return f"{date_part[5:]} {time_part[:5]}" if len(date_part) >= 10 else time_part[:5]


def _comparison_insight_content(submissions: list[WeatherSubmission], metrics: list[str]) -> str:
    insights = []
    if "temperature" in metrics:
        hot = _best_summary_value(submissions, "max_temperature", reverse=True)
        cool = _best_summary_value(submissions, "min_temperature", reverse=False)
        if hot:
            insights.append(f"- 最高温更高：{hot.region} {hot.target_date} 约 {hot.aggregated_forecast.summary.max_temperature}℃")
        if cool:
            insights.append(f"- 最低温更低：{cool.region} {cool.target_date} 约 {cool.aggregated_forecast.summary.min_temperature}℃")
    if "rain" in metrics:
        rainy = _best_summary_value(submissions, "rain_probability", reverse=True)
        if rainy:
            insights.append(f"- 降水风险更高：{rainy.region} {rainy.target_date}，概率约 {rainy.aggregated_forecast.summary.rain_probability}%")
    if "wind" in metrics:
        windy = _best_summary_value(submissions, "wind_speed", reverse=True)
        if windy:
            insights.append(f"- 风速更高：{windy.region} {windy.target_date}，约 {windy.aggregated_forecast.summary.wind_speed} m/s")
    if "cloud" in metrics:
        cloudy = _best_summary_value(submissions, "cloud_cover", reverse=True)
        if cloudy:
            insights.append(f"- 云量更高：{cloudy.region} {cloudy.target_date}，约 {cloudy.aggregated_forecast.summary.cloud_cover}%")
    if not insights:
        insights.append("- 暂无足够数据形成对比结论")
    return "**对比结论：**\n" + "\n".join(insights)


def _comparison_table_elements(submissions: list[WeatherSubmission], metrics: list[str]) -> list[dict]:
    elements: list[dict] = []
    shown_count = 0
    max_rows = 24
    for region, region_submissions in _group_submissions_by_region(submissions):
        if shown_count >= max_rows:
            break
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{region}**"},
            }
        )
        elements.append(_comparison_table_row(_comparison_table_columns(metrics), is_header=True))
        for submission in region_submissions:
            if shown_count >= max_rows:
                break
            elements.append(_comparison_table_row(_comparison_table_cells(submission, metrics), is_header=False))
            shown_count += 1
    if len(submissions) > max_rows:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"已展示前 {max_rows} 条，共 {len(submissions)} 条；完整明细可打开网页报告查看。",
                    }
                ],
            }
        )
    return elements


def _comparison_table_columns(metrics: list[str]) -> list[tuple[str, int]]:
    left_labels = []
    if "temperature" in metrics:
        left_labels.append("温度")
    if "rain" in metrics:
        left_labels.append("降水")
    right_labels = []
    if "wind" in metrics:
        right_labels.append("风速")
    if "cloud" in metrics:
        right_labels.append("云量")
    if any(metric in metrics for metric in ("rain", "cloud")):
        right_labels.append("天气")
    return [
        ("日期", 10),
        (" / ".join(left_labels) or "指标", 24),
        (" / ".join(right_labels) or "说明", 34),
    ]


def _comparison_table_cells(submission: WeatherSubmission, metrics: list[str]) -> list[tuple[str, int]]:
    summary = submission.aggregated_forecast.summary
    primary_lines = []
    if "temperature" in metrics:
        primary_lines.append(
            f"最高/最低：{_format_card_value(summary.max_temperature, '℃')} / {_format_card_value(summary.min_temperature, '℃')}"
        )
    if "rain" in metrics:
        primary_lines.append(f"降水：{_format_card_value(summary.rain_probability, '%')}")
    secondary_lines = []
    if "wind" in metrics:
        secondary_lines.append(f"风速：{_format_card_value(summary.wind_speed, 'm/s')}")
    if "cloud" in metrics:
        secondary_lines.append(f"云量：{_format_card_value(summary.cloud_cover, '%')}")
    if any(metric in metrics for metric in ("rain", "cloud")):
        secondary_lines.append(f"天气：{summary.main_weather}")
    if any(metric in metrics for metric in ("rain", "wind")):
        secondary_lines.append(f"风险：{summary.high_risk_period}")
    return [
        (submission.target_date[5:] if len(submission.target_date) >= 10 else submission.target_date, 10),
        ("\n".join(primary_lines) if primary_lines else "暂无指标数据", 24),
        ("\n".join(secondary_lines) if secondary_lines else "暂无补充说明", 34),
    ]


def _group_submissions_by_region(submissions: list[WeatherSubmission]) -> list[tuple[str, list[WeatherSubmission]]]:
    groups: list[tuple[str, list[WeatherSubmission]]] = []
    index: dict[str, list[WeatherSubmission]] = {}
    for submission in submissions:
        bucket = index.get(submission.region)
        if bucket is None:
            bucket = []
            index[submission.region] = bucket
            groups.append((submission.region, bucket))
        bucket.append(submission)
    for _region, bucket in groups:
        bucket.sort(key=lambda item: item.target_date)
    return groups


def _comparison_table_row(cells: list[tuple[str, int]], is_header: bool) -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey" if is_header else "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": weight,
                "vertical_align": "top",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{value}**" if is_header else str(value),
                        },
                    }
                ],
            }
            for value, weight in cells
        ],
    }


def _comparison_chart_elements(submissions: list[WeatherSubmission], metrics: list[str]) -> list[dict]:
    elements = []
    chart_configs = [
        ("temperature", "最高温对比（℃）", "line", "max_temperature", "value", None),
        ("rain", "降水概率对比（%）", "bar", "rain_probability", "value", 100),
        ("wind", "风速对比（m/s）", "line", "wind_speed", "value", None),
        ("cloud", "云量对比（%）", "bar", "cloud_cover", "value", 100),
    ]
    for metric, title, chart_type, field, value_field, max_value in chart_configs:
        if metric not in metrics:
            continue
        values = []
        for submission in submissions:
            value = getattr(submission.aggregated_forecast.summary, field)
            if value is None:
                continue
            label = submission.target_date[5:] if len(submission.target_date) >= 10 else submission.target_date
            values.append({"date": label, "region": submission.region, value_field: value})
        if not values:
            continue
        spec = {
            "type": chart_type,
            "title": {"text": title},
            "data": {"values": values},
            "xField": "date",
            "yField": value_field,
            "seriesField": "region",
            "legends": {"visible": True},
            "tooltip": {"visible": True},
            "label": {"visible": False},
        }
        if max_value is not None:
            spec["axes"] = [{"orient": "left", "min": 0, "max": max_value}]
        elements.append({"tag": "chart", "aspect_ratio": "16:9", "chart_spec": spec})
    return elements


def _best_summary_value(submissions: list[WeatherSubmission], field: str, reverse: bool) -> WeatherSubmission | None:
    usable = [submission for submission in submissions if getattr(submission.aggregated_forecast.summary, field) is not None]
    if not usable:
        return None
    return sorted(usable, key=lambda item: getattr(item.aggregated_forecast.summary, field), reverse=reverse)[0]


def _format_card_value(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    return f"{value:g}{unit}"


def _ordered_unique(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
