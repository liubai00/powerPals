from __future__ import annotations

import csv
import html
import io
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from services.weather_bot.models import ForecastRequest, WeatherSubmission


class WeatherBatchRequest(BaseModel):
    requests: list[ForecastRequest] = Field(default_factory=list)


class NewsItem(BaseModel):
    title: str
    source: str
    url: str
    tags: list[str] = Field(default_factory=list)
    published_at: str = ""
    summary: str = ""


class HydrologyRecord(BaseModel):
    station: str
    basin: str
    water_level: float | None = None
    flow: float | None = None
    observed_at: str
    notes: str = ""


def forecast_request_dates(request: ForecastRequest) -> list[str]:
    start = date.fromisoformat(request.target_date)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(request.days)]


async def collect_forecasts(service: Any, request: ForecastRequest) -> list[WeatherSubmission]:
    submissions = []
    for target_date in forecast_request_dates(request):
        current = request.model_copy(update={"target_date": target_date})
        submissions.append(await service.forecast(current))
    return submissions


async def collect_forecasts_with_errors(service: Any, request: ForecastRequest) -> tuple[list[WeatherSubmission], list[dict[str, str]]]:
    submissions = []
    errors = []
    for target_date in forecast_request_dates(request):
        current = request.model_copy(update={"target_date": target_date})
        try:
            submissions.append(await service.forecast(current))
        except Exception as exc:  # noqa: BLE001 - range reports should preserve usable dates
            errors.append({"target_date": target_date, "error": str(exc)})
    return submissions, errors


def weather_csv(submissions: list[WeatherSubmission]) -> str:
    handle = io.StringIO()
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(
        [
            "target_date",
            "region",
            "max_temperature",
            "min_temperature",
            "rain_probability",
            "wind_speed",
            "cloud_cover",
            "main_weather",
            "high_risk_period",
            "task_id",
        ]
    )
    for submission in submissions:
        summary = submission.aggregated_forecast.summary
        writer.writerow(
            [
                submission.target_date,
                submission.region,
                summary.max_temperature,
                summary.min_temperature,
                summary.rain_probability,
                summary.wind_speed,
                summary.cloud_cover,
                summary.main_weather,
                summary.high_risk_period,
                submission.task_id,
            ]
        )
    return handle.getvalue()


def hydrology_csv(records: list[dict[str, Any]]) -> str:
    handle = io.StringIO()
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(["station", "basin", "water_level", "flow", "observed_at", "notes"])
    for record in records:
        writer.writerow(
            [
                record.get("station", ""),
                record.get("basin", ""),
                record.get("water_level", ""),
                record.get("flow", ""),
                record.get("observed_at", ""),
                record.get("notes", ""),
            ]
        )
    return handle.getvalue()


def weather_report_html(
    submissions: list[WeatherSubmission],
    download_query: dict[str, Any],
    errors: list[dict[str, str]] | None = None,
) -> str:
    region = submissions[0].region if submissions else str(download_query.get("region", ""))
    title = f"{region}气象数据工作台"
    rows = []
    for submission in submissions:
        summary = submission.aggregated_forecast.summary
        rows.append(
            "<tr>"
            f"<td>{html.escape(submission.target_date)}</td>"
            f"<td>{html.escape(submission.region)}</td>"
            f"<td>{summary.max_temperature}</td>"
            f"<td>{summary.min_temperature}</td>"
            f"<td>{summary.rain_probability}</td>"
            f"<td>{summary.wind_speed}</td>"
            f"<td>{summary.cloud_cover}</td>"
            f"<td>{html.escape(summary.main_weather)}</td>"
            "</tr>"
        )
    download_url = "/api/weather/export?" + urlencode(download_query)
    error_note = ""
    if errors:
        details = "；".join(f"{item['target_date']}: {item['error']}" for item in errors[:5])
        error_note = f'<p class="note warning">部分日期暂无可用数据：{html.escape(details)}</p>'
    json_url = "/api/weather/export/json?" + urlencode(download_query)
    temperature_chart = _line_chart_svg(
        "温度趋势（℃）",
        _summary_points(submissions, "max_temperature"),
        "#dc2626",
        "℃",
        second_points=_summary_points(submissions, "min_temperature"),
        second_color="#2563eb",
    )
    rain_chart = _bar_chart_svg("降水概率（%）", _summary_points(submissions, "rain_probability"), "#0f766e", "%", max_value=100)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1f2933; background: #f6f8fb; }}
    header {{ background: #0f766e; color: white; padding: 20px 24px; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 20px; }}
    .toolbar {{ display: flex; gap: 12px; align-items: center; margin: 16px 0; }}
    .button {{ display: inline-block; background: #0f766e; color: white; text-decoration: none; padding: 10px 14px; border-radius: 6px; }}
    .button.secondary {{ background: #334155; }}
    .chart-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 18px 0; }}
    .chart-panel {{ background: white; border: 1px solid #d8dee9; padding: 14px; }}
    .chart-title {{ margin: 0 0 10px; font-size: 15px; color: #263445; }}
    .chart {{ width: 100%; height: auto; display: block; }}
    .table-wrap {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    table {{ width: 100%; min-width: 760px; border-collapse: collapse; background: white; border: 1px solid #d8dee9; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e5e9f0; text-align: left; font-size: 14px; }}
    th {{ background: #edf2f7; }}
    .note {{ color: #607080; font-size: 13px; margin-top: 12px; }}
    .warning {{ color: #9a3412; }}
    @media (max-width: 760px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p>面向飞书群转发的气象预测网页报告，可下载 CSV 后用 Excel 打开。</p>
  </header>
  <main>
    <div class="toolbar">
      <a class="button" href="{html.escape(download_url)}">下载CSV</a>
      <a class="button secondary" href="{html.escape(json_url)}">下载JSON</a>
    </div>
    <section class="chart-grid">
      <div class="chart-panel">
        <h2 class="chart-title">温度趋势</h2>
        {temperature_chart}
      </div>
      <div class="chart-panel">
        <h2 class="chart-title">降水概率</h2>
        {rain_chart}
      </div>
    </section>
    <div class="table-wrap">
      <table>
        <thead><tr><th>日期</th><th>区域</th><th>最高温</th><th>最低温</th><th>降水概率</th><th>风速</th><th>云量</th><th>主要天气</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    {error_note}
    <p class="note">本页面仅用于 PowerPals 社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。</p>
  </main>
</body>
</html>"""


def _summary_points(submissions: list[WeatherSubmission], field: str) -> list[tuple[str, float]]:
    points = []
    for submission in submissions:
        value = getattr(submission.aggregated_forecast.summary, field)
        if value is None:
            continue
        label = submission.target_date[5:] if len(submission.target_date) >= 10 else submission.target_date
        points.append((label, float(value)))
    return points


def _line_chart_svg(
    title: str,
    points: list[tuple[str, float]],
    color: str,
    unit: str,
    second_points: list[tuple[str, float]] | None = None,
    second_color: str = "#2563eb",
) -> str:
    all_points = points + (second_points or [])
    if not all_points:
        return '<p class="note">暂无可绘制数据。</p>'
    width, height = 720, 240
    left, right, top, bottom = 52, 18, 24, 42
    values = [value for _, value in all_points]
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        minimum -= 1
        maximum += 1
    labels = [label for label, _ in points or all_points]

    def scale(series: list[tuple[str, float]]) -> str:
        coords = []
        for index, (_label, value) in enumerate(series):
            x = left + (width - left - right) * (index / max(1, len(series) - 1))
            y = top + (height - top - bottom) * (1 - ((value - minimum) / (maximum - minimum)))
            coords.append(f"{x:.1f},{y:.1f}")
        return " ".join(coords)

    label_nodes = "".join(
        f'<text x="{left + (width - left - right) * (index / max(1, len(labels) - 1)):.1f}" y="{height - 16}" text-anchor="middle" font-size="12" fill="#64748b">{html.escape(label)}</text>'
        for index, label in enumerate(labels)
    )
    y_min = f"{minimum:.1f}{unit}"
    y_max = f"{maximum:.1f}{unit}"
    second_line = f'<polyline points="{scale(second_points or [])}" fill="none" stroke="{second_color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />' if second_points else ""
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
  <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#cbd5e1" />
  <line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1" />
  <text x="8" y="{top + 4}" font-size="12" fill="#64748b">{html.escape(y_max)}</text>
  <text x="8" y="{height - bottom}" font-size="12" fill="#64748b">{html.escape(y_min)}</text>
  <polyline points="{scale(points)}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
  {second_line}
  {label_nodes}
</svg>"""


def _bar_chart_svg(
    title: str,
    points: list[tuple[str, float]],
    color: str,
    unit: str,
    max_value: float | None = None,
) -> str:
    if not points:
        return '<p class="note">暂无可绘制数据。</p>'
    width, height = 720, 240
    left, right, top, bottom = 52, 18, 24, 42
    maximum = max(max_value or 0, max(value for _, value in points), 1)
    bar_area = width - left - right
    slot = bar_area / len(points)
    bar_width = min(52, slot * 0.55)
    bars = []
    labels = []
    for index, (label, value) in enumerate(points):
        x = left + slot * index + (slot - bar_width) / 2
        bar_height = (height - top - bottom) * (value / maximum)
        y = height - bottom - bar_height
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}" rx="3" />')
        labels.append(f'<text x="{x + bar_width / 2:.1f}" y="{height - 16}" text-anchor="middle" font-size="12" fill="#64748b">{html.escape(label)}</text>')
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
  <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#cbd5e1" />
  <line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1" />
  <text x="8" y="{top + 4}" font-size="12" fill="#64748b">{maximum:.0f}{html.escape(unit)}</text>
  <text x="20" y="{height - bottom}" font-size="12" fill="#64748b">0{html.escape(unit)}</text>
  {''.join(bars)}
  {''.join(labels)}
</svg>"""
