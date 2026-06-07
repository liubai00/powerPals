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
    .table-wrap {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    table {{ width: 100%; min-width: 760px; border-collapse: collapse; background: white; border: 1px solid #d8dee9; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e5e9f0; text-align: left; font-size: 14px; }}
    th {{ background: #edf2f7; }}
    .note {{ color: #607080; font-size: 13px; margin-top: 12px; }}
    .warning {{ color: #9a3412; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p>面向飞书群转发的气象预测网页报告，可下载 CSV 后用 Excel 打开。</p>
  </header>
  <main>
    <div class="toolbar"><a class="button" href="{html.escape(download_url)}">下载CSV</a></div>
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
