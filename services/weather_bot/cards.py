from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlencode

from services.weather_bot.models import WeatherSubmission
from services.weather_bot.weather_metrics import SUPPORTED_WEATHER_METRIC_ORDER, normalize_weather_metrics


SHANGHAI_TZ = timezone(timedelta(hours=8))


def to_feishu_lark_md(text: str) -> str:
    """把 LLM 输出规整成飞书 lark_md 友好格式: Markdown 标题(#/##)转成加粗、收敛多余空行。
    lark_md 能渲染 **加粗**/换行/`-` 列表, 但不认 # 标题(会原样露出), 故此处兜底转换。"""
    out_lines = []
    for line in (text or "").split("\n"):
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*#*\s*$", line)
        out_lines.append(f"**{heading.group(2).strip()}**" if heading else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()


def is_rich_reply_text(text: str) -> bool:
    """含 Markdown 加粗/标题的 LLM 回答需用卡片渲染(纯文本会把 ** 和 ## 符号原样露出)。"""
    if not text:
        return False
    return "**" in text or bool(re.search(r"(?:^|\n)\s{0,3}#{1,6}\s", text))


def build_text_reply_card(text: str) -> dict:
    """把富文本回答包成极简 lark_md 卡片, 让加粗/小标题/列表在飞书里正常渲染。"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": to_feishu_lark_md(text)}},
            ],
        },
    }


def build_text_summary(submission: WeatherSubmission) -> str:
    summary = submission.aggregated_forecast.summary
    retrieved_at = submission.time_info.retrieved_at or "未记录"
    business_submission_deadline = (
        submission.time_info.business_submission_deadline or submission.data_cutoff_time
    )
    return "\n".join(
        [
            f"【正式提交｜{submission.region}气象预测】",
            "",
            f"任务 ID：{submission.task_id}",
            f"区域：{submission.region}",
            f"预测日：{submission.target_date}",
            f"数据抓取时间：{retrieved_at}",
            f"业务提交截止：{business_submission_deadline}",
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


def _weather_emoji(text: str | None) -> str:
    t = text or ""
    if "雷" in t:
        return "⛈️"
    if "雪" in t:
        return "❄️"
    if "雨" in t:
        return "🌧️"
    if "雾" in t or "霾" in t:
        return "🌫️"
    if "阴" in t:
        return "☁️"
    if "多云" in t:
        return "⛅"
    if "晴" in t:
        return "☀️"
    return "🌤️"


def _fmt_metric(value: object) -> str:
    if value is None:
        return "—"
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)


def _daily_forecast_columns(submissions: list) -> dict:
    columns = []
    for sub in submissions[:7]:
        s = sub.aggregated_forecast.summary
        emoji = _weather_emoji(getattr(s, "main_weather", None))
        md = (
            f"**{sub.target_date[5:]}**\n"
            f"{emoji} {getattr(s, 'main_weather', None) or '—'}\n"
            f"🌡️ **{_fmt_metric(s.max_temperature)}°** / {_fmt_metric(s.min_temperature)}°\n"
            f"💧 {_fmt_metric(s.rain_probability)}%"
        )
        columns.append({
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "top",
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}],
        })
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "default", "columns": columns}


def _severity_rank(summary) -> int:
    rain = summary.rain_probability or 0
    wind = summary.wind_speed or 0
    temp = summary.max_temperature or 0
    cloud = summary.cloud_cover or 0
    if (rain >= 70 and wind >= 10) or wind >= 15:
        return 4
    if rain >= 50:
        return 3
    if temp >= 35:
        return 2
    if cloud >= 75:
        return 1
    return 0


_HEADER_TEMPLATES = {4: "red", 3: "blue", 2: "orange", 1: "grey", 0: "wathet"}


def _header_template_for(chart_items) -> str:
    rank = max(_severity_rank(item.aggregated_forecast.summary) for item in chart_items)
    return _HEADER_TEMPLATES[rank]


def _card_title(submission, chart_items) -> str:
    emoji = _weather_emoji(getattr(chart_items[0].aggregated_forecast.summary, "main_weather", None))
    region = _submission_region_label(submission)
    if len(chart_items) > 1:
        return f"{emoji} {region} · 未来{len(chart_items)}天气象预测"
    return f"{emoji} {region}气象预测"


def _submission_region_label(submission) -> str:
    location = getattr(getattr(submission, "scope", None), "location", None) or {}
    if location.get("representation") == "province_representative_point":
        representative_city = str(location.get("representative_city") or "").removesuffix("市")
        if representative_city:
            return f"{submission.region}（{representative_city}代表点）"
    return submission.region


def _glance_line(item) -> str:
    from datetime import date as _d

    s = item.aggregated_forecast.summary
    label = "今日" if item.target_date == _d.today().isoformat() else item.target_date[5:]
    weather = getattr(s, "main_weather", None) or "—"
    bits = [f"{_weather_emoji(weather)} {weather}", f"{_fmt_metric(s.max_temperature)}° / {_fmt_metric(s.min_temperature)}°"]
    if s.rain_probability is not None:
        bits.append(f"💧{_fmt_metric(s.rain_probability)}%")
    if s.wind_speed is not None:
        bits.append(f"💨{_fmt_metric(s.wind_speed)}m/s")
    return f"**{label}：{'　'.join(bits)}**"


def _risk_banner(chart_items) -> str | None:
    risky = []
    for item in chart_items:
        period = getattr(item.aggregated_forecast.summary, "high_risk_period", None)
        if period and "无明显" not in period:
            risky.append((item.target_date[5:], period))
    if not risky:
        return None
    day, period = risky[0]
    label = f"{day} {period}" if len(chart_items) > 1 else period
    extra = f"（另有 {len(risky) - 1} 天存在风险时段）" if len(risky) > 1 else ""
    return f"⚠️ **高风险：{label}**{extra}"


def _insight_text(chart_items) -> str:
    """只描述气象侧代理，不将天气直接解释为真实供需、出力或价格。"""
    summaries = [item.aggregated_forecast.summary for item in chart_items]
    rains = [s.rain_probability or 0 for s in summaries]
    temps = [s.max_temperature or 0 for s in summaries]
    winds = [s.wind_speed or 0 for s in summaries]
    clouds = [s.cloud_cover or 0 for s in summaries]
    bits = []
    if max(temps) >= 35:
        bits.append("高温使负荷天气压力代理升高")
    elif max(temps) >= 32:
        bits.append("气温偏高，负荷天气压力代理偏强")
    elif len(temps) >= 2 and temps[0] - temps[-1] >= 5:
        bits.append("显著降温，负荷天气压力代理发生变化")
    peak_wind = max(winds)
    if peak_wind >= 12:
        bits.append("10米地面风资源代理偏强")
    elif peak_wind >= 8:
        bits.append("10米地面风资源代理中等偏强")
    elif peak_wind <= 4:
        bits.append("10米地面风资源代理偏弱")
    if max(rains) >= 60 or min(clouds) >= 80:
        bits.append("云量和降水使光资源代理转弱")
    elif max(clouds) <= 40:
        bits.append("少云条件下光资源代理偏强")
    if not bits:
        bits.append("负荷天气压力代理、10米地面风资源代理和光资源代理整体平稳")
    boundary = "实际负荷、风光出力、供需和价格方向待结合电力数据核查"
    return "💡 " + "；".join([*bits[:3], boundary])


def build_feishu_card(
    submission: WeatherSubmission,
    report_url: str | None = None,
    download_url: str | None = None,
    json_url: str | None = None,
    chart_submissions: list[WeatherSubmission] | None = None,
    show_task_id: bool = False,
    include_submission_note: bool = False,
    metrics: list[str] | None = None,
    notice: str | None = None,
) -> dict:
    chart_items = chart_submissions or [submission]
    selected_metrics = normalize_weather_metrics(metrics)
    date_labels = [item.target_date for item in chart_items]
    date_label = (
        f"{date_labels[0]} 至 {date_labels[-1]}（{len(date_labels)}天）"
        if len(date_labels) > 1
        else submission.target_date
    )
    first_lines = []
    if show_task_id:
        first_lines.append(f"**任务 ID**：{submission.task_id}")
    first_lines.append(_glance_line(chart_items[0]))
    scope_bits = [date_label if len(date_labels) > 1 else f"预测日 {date_label}"]
    retrieved_at = submission.time_info.retrieved_at
    scope_bits.append(f"抓取时间 {_fmt_cutoff(retrieved_at) if retrieved_at else '未记录'}")
    business_submission_deadline = (
        submission.time_info.business_submission_deadline or submission.data_cutoff_time
    )
    scope_bits.append(f"业务提交截止 {_fmt_cutoff(business_submission_deadline)}")
    scope_bits.append("来源 " + " / ".join(submission.aggregated_forecast.providers_used))
    _sm = submission.aggregated_forecast.summary
    if _sm.sunrise and _sm.sunset:
        scope_bits.append(f"日出 {_sm.sunrise} · 日落 {_sm.sunset}")
    meta_note = "　·　".join(scope_bits)
    elements = []
    if notice:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": notice}})
    elements += [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(first_lines)}},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": meta_note}]},
        {"tag": "hr"},
    ]
    if len(chart_items) > 1:
        elements.append(_daily_forecast_columns(chart_items))
        elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**📊 关键指标**"}})
    elements.extend(_summary_metric_rows(submission, selected_metrics))
    risk_line = _risk_banner(chart_items)
    if risk_line:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": risk_line}})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _insight_text(chart_items)}})
    if len(chart_items) > 4:
        # 多日(>4天)用日级趋势图, 逐小时挤不下也看不清
        chart_elements = _weather_chart_elements(chart_items, selected_metrics) or _hourly_chart_elements(chart_items, selected_metrics)
    else:
        chart_elements = _hourly_chart_elements(chart_items, selected_metrics) or _weather_chart_elements(chart_items, selected_metrics)
    elements.extend(_charts_grid(chart_elements))
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
        _btn_layout = "trisection" if len(actions) == 3 else "bisection" if len(actions) == 2 else "flow"
        elements.extend([{"tag": "hr"}, {"tag": "action", "layout": _btn_layout, "actions": actions}])
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
                "template": _header_template_for(chart_items),
                "title": {"tag": "plain_text", "content": _card_title(submission, chart_items)},
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


def _compass(degrees: float | None) -> str:
    if degrees is None:
        return "—"
    names = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return names[int((float(degrees) % 360) / 45 + 0.5) % 8] + "风"


def _uv_level(uv: float | None) -> str:
    if uv is None:
        return "—"
    value = float(uv)
    if value < 3:
        label = "弱"
    elif value < 6:
        label = "中等"
    elif value < 8:
        label = "强"
    elif value < 11:
        label = "很强"
    else:
        label = "极强"
    return "%d（%s）" % (round(value), label)


def _forecast_detail_content(submission: WeatherSubmission, metrics: list[str] | None = None) -> str:
    summary = submission.aggregated_forecast.summary
    selected = set(normalize_weather_metrics(metrics))
    all_metrics = set(SUPPORTED_WEATHER_METRIC_ORDER)
    lines = []
    if "temperature" in selected:
        lines.extend([f"- 最高温：{summary.max_temperature}℃", f"- 最低温：{summary.min_temperature}℃"])
        if summary.feels_like is not None:
            lines.append(f"- 体感最高：{summary.feels_like}℃")
    if "rain" in selected:
        lines.append(f"- 降水概率：{summary.rain_probability}%")
    if "wind" in selected:
        lines.append(f"- 风速：{summary.wind_speed} m/s")
        if summary.wind_direction is not None:
            lines.append(f"- 风向：{_compass(summary.wind_direction)}")
    if "cloud" in selected:
        lines.append(f"- 云量：{summary.cloud_cover}%")
    if selected & {"rain", "cloud"}:
        lines.append(f"- 主要天气：{summary.main_weather}")
    if selected & {"rain", "wind"} or selected == all_metrics:
        lines.append(f"- 高风险时段：{summary.high_risk_period}")
    extras = []
    if summary.uv_index is not None:
        extras.append(f"- 紫外线：{_uv_level(summary.uv_index)}")
    if summary.sunrise and summary.sunset:
        extras.append(f"- 日出 / 日落：{summary.sunrise} / {summary.sunset}")
    lines.extend(extras)
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


def _fmt_cutoff(value: object) -> str:
    v = str(value or "")
    if v:
        try:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(SHANGHAI_TZ).strftime("%m-%d %H:%M")
        except ValueError:
            pass
    if "T" in v and len(v) >= 16:
        return v[5:10] + " " + v[11:16]
    return v


def _summary_metric_rows(submission: WeatherSubmission, metrics: list[str] | None = None) -> list[dict]:
    """关键指标: 与逐日条同构的等宽三列网格(2x3), 比 fields 更整齐。"""
    summary = submission.aggregated_forecast.summary
    selected = set(normalize_weather_metrics(metrics))
    cells: list[tuple[str, str]] = []
    if "temperature" in selected:
        temp_value = f"{_fmt_metric(summary.max_temperature)}° / {_fmt_metric(summary.min_temperature)}°"
        if (summary.max_temperature or 0) >= 35:
            temp_value = f"🔴 {temp_value} ↑"
        cells.append(("🌡️ 气温", temp_value))
        if summary.feels_like is not None:
            feels_value = f"{summary.feels_like}℃"
            if summary.feels_like >= 38:
                feels_value = f"🔴 {feels_value} ↑"
            cells.append(("🤒 体感最高", feels_value))
    if "rain" in selected:
        rain_value = f"{_fmt_metric(summary.rain_probability)}%"
        if (summary.rain_probability or 0) >= 60:
            rain_value = f"🔵 {rain_value} ↑"
        cells.append(("🌧️ 降水概率", rain_value))
    if "wind" in selected:
        wind_value = f"{_fmt_metric(summary.wind_speed)} m/s"
        if summary.wind_direction is not None:
            wind_value += f" · {_compass(summary.wind_direction)}"
        if (summary.wind_speed or 0) >= 10:
            wind_value = f"🟠 {wind_value} ↑"
        cells.append(("💨 风", wind_value))
    if "cloud" in selected:
        cells.append(("☁️ 云量", f"{_fmt_metric(summary.cloud_cover)}%"))
    if summary.uv_index is not None:
        cells.append(("🔆 紫外线", _uv_level(summary.uv_index)))
    if not cells:
        return [{"tag": "div", "text": {"tag": "lark_md", "content": "暂无指标数据"}}]

    rows: list[dict] = []
    per_row = 3
    for start in range(0, len(cells), per_row):
        chunk = cells[start:start + per_row]
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"{label}\n**{value}**"}}],
            }
            for label, value in chunk
        ]
        while len(columns) < per_row and len(cells) > per_row:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "　"}}],
            })
        rows.append({
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "default",
            "columns": columns,
        })
    return rows


def _summary_fields_element(submission: WeatherSubmission, metrics: list[str] | None = None) -> dict:
    summary = submission.aggregated_forecast.summary
    selected = set(normalize_weather_metrics(metrics))
    all_metrics = set(SUPPORTED_WEATHER_METRIC_ORDER)
    fields: list[dict] = []

    def add(label: str, value: str, short: bool = True) -> None:
        fields.append({"is_short": short, "text": {"tag": "lark_md", "content": f"{label}\n**{value}**"}})

    if "temperature" in selected:
        temp_value = f"{_fmt_metric(summary.max_temperature)}° / {_fmt_metric(summary.min_temperature)}°"
        if (summary.max_temperature or 0) >= 35:
            temp_value = f"🔴 {temp_value} ↑"
        add("🌡️ 气温", temp_value)
        if summary.feels_like is not None:
            feels_value = f"{summary.feels_like}℃"
            if summary.feels_like >= 38:
                feels_value = f"🔴 {feels_value} ↑"
            add("🤒 体感最高", feels_value)
    if "rain" in selected:
        rain_value = f"{_fmt_metric(summary.rain_probability)}%"
        if (summary.rain_probability or 0) >= 60:
            rain_value = f"🔵 {rain_value} ↑"
        add("🌧️ 降水概率", rain_value)
    if "wind" in selected:
        wind = f"{_fmt_metric(summary.wind_speed)} m/s"
        if summary.wind_direction is not None:
            wind += f" · {_compass(summary.wind_direction)}"
        if (summary.wind_speed or 0) >= 10:
            wind = f"🟠 {wind} ↑"
        add("💨 风", wind)
    if "cloud" in selected:
        add("☁️ 云量", f"{_fmt_metric(summary.cloud_cover)}%")
    if summary.uv_index is not None:
        add("🔆 紫外线", _uv_level(summary.uv_index))
    if summary.sunrise and summary.sunset:
        add("🌅 日出 / 日落", f"{summary.sunrise} / {summary.sunset}")
    if selected & {"rain", "cloud"}:
        add("🌥️ 主要天气", summary.main_weather)

    if not fields:
        return {"tag": "div", "text": {"tag": "lark_md", "content": "暂无指标数据"}}
    return {"tag": "div", "fields": fields}


def _explanation_block(submission: WeatherSubmission) -> str:
    return "\n".join(
        [
            "**主要影响因素：**",
            *_numbered_lines(submission.key_factors),
            "",
            "**风险提示：**",
            *_bullet_lines(submission.risk_notes),
        ]
    )


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


_CHART_LABELS = {
    "temperature": "🌡️ 温度 ℃",
    "rain_probability": "🌧️ 降水概率 %",
    "wind_speed": "💨 风速 m/s",
    "cloud_cover": "☁️ 云量 %",
}


def _chart_label_text(element: dict) -> str:
    spec = element.get("chart_spec", {}) if isinstance(element, dict) else {}
    yfield = spec.get("yField")
    if yfield in _CHART_LABELS:
        return _CHART_LABELS[yfield]
    title = ""
    raw = spec.get("title")
    if isinstance(raw, dict):
        title = str(raw.get("text") or "")
    if "温" in title:
        return "🌡️ 温度 ℃"
    if "降水" in title or "雨" in title:
        return "🌧️ 降水概率 %"
    if "风" in title:
        return "💨 风速 m/s"
    if "云" in title:
        return "☁️ 云量 %"
    return title or "趋势"


def _labeled_chart(element: dict) -> list[dict]:
    label = _chart_label_text(element)
    spec = element.get("chart_spec") if isinstance(element, dict) else None
    if isinstance(spec, dict):
        spec.pop("title", None)  # 图内标题在窄列里不渲染, 用上方独立标题替代
    return [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{label}**"}},
        element,
    ]


def _charts_grid(chart_elements: list[dict]) -> list[dict]:
    """Arrange trend charts into a 2-column grid (2x2); each chart gets a label header."""
    if not chart_elements:
        return []
    labeled = [_labeled_chart(element) for element in chart_elements]
    if len(chart_elements) == 1:
        return labeled[0]
    col1 = [item for pair in labeled[0::2] for item in pair]
    col2 = [item for pair in labeled[1::2] for item in pair]
    return [{
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top", "elements": col1},
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top", "elements": col2},
        ],
    }]


def _hourly_chart_elements(submissions: list[WeatherSubmission], metrics: list[str] | None = None) -> list[dict]:
    selected = set(normalize_weather_metrics(metrics))
    temperature_values = []
    rain_values = []
    wind_values = []
    cloud_values = []
    seen_hours: set[str] = set()
    for submission in submissions:
        for point in submission.aggregated_forecast.points:
            if _hourly_metric_limit_reached(selected, temperature_values, rain_values, wind_values, cloud_values):
                break
            hour_key = "%s|%s" % (submission.region, str(point.time)[:13])  # 归一化到小时去重(合并 provider 时间格式差异 + 跨日重叠)
            if hour_key in seen_hours:
                continue
            seen_hours.add(hour_key)
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
