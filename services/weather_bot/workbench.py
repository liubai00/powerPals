from __future__ import annotations

import csv
import html
import io
import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from services.weather_bot.models import ForecastRequest, WeatherSubmission
from services.weather_bot.weather_metrics import normalize_weather_metrics


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


def weather_csv(submissions: list[WeatherSubmission], include_task_id: bool = False) -> str:
    handle = io.StringIO()
    writer = csv.writer(handle, lineterminator="\n")
    columns = [
        "target_date",
        "region",
        "max_temperature",
        "min_temperature",
        "rain_probability",
        "wind_speed",
        "cloud_cover",
        "main_weather",
        "high_risk_period",
    ]
    if include_task_id:
        columns.append("task_id")
    writer.writerow(columns)
    for submission in submissions:
        summary = submission.aggregated_forecast.summary
        row = [
            submission.target_date,
            submission.region,
            summary.max_temperature,
            summary.min_temperature,
            summary.rain_probability,
            summary.wind_speed,
            summary.cloud_cover,
            summary.main_weather,
            summary.high_risk_period,
        ]
        if include_task_id:
            row.append(submission.task_id)
        writer.writerow(row)
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
    metrics: list[str] | None = None,
    auto_download: str | None = None,
    title: str | None = None,
    download_path: str = "/api/weather/export",
    json_path: str = "/api/weather/export/json",
) -> str:
    selected_metrics = normalize_weather_metrics(metrics)
    auto_download_kind = auto_download if auto_download in {"csv", "json"} else ""
    region = submissions[0].region if submissions else str(download_query.get("region", ""))
    page_title = title or f"{region}气象预测"
    comparison_report = _is_multi_region_report(submissions, download_query)
    report_scope = _report_scope_html(submissions, download_query, comparison_report)
    rows = _summary_table_rows(submissions, selected_metrics)
    summary_table_header = _summary_table_header(selected_metrics)
    download_url = download_path + "?" + urlencode(download_query)
    error_note = ""
    if errors:
        details = "；".join(f"{item['target_date']}: {item['error']}" for item in errors[:5])
        error_note = f'<p class="note warning">部分日期暂无可用数据：{html.escape(details)}</p>'
    json_url = json_path + "?" + urlencode(download_query)
    csv_filename = f"powerpals-weather-{download_query.get('target_date', 'forecast')}-{download_query.get('days', 1)}d.csv"
    json_filename = f"powerpals-weather-{download_query.get('target_date', 'forecast')}-{download_query.get('days', 1)}d.json"
    download_payload_script = _json_for_script(_download_payload(submissions, download_query, errors))
    auto_download_script = _json_for_script(auto_download_kind)
    if comparison_report:
        temperature_chart = _multi_series_line_chart_svg(
            "最高温趋势（℃）",
            _summary_series(submissions, "max_temperature"),
            "℃",
        )
        rain_chart = _multi_series_line_chart_svg(
            "降水概率（%）",
            _summary_series(submissions, "rain_probability"),
            "%",
            min_value=0,
            max_value=100,
        )
        hourly_temperature_chart = _multi_series_line_chart_svg(
            "小时温度（℃）",
            _hourly_series(submissions, "temperature"),
            "℃",
        )
        hourly_rain_chart = _multi_series_line_chart_svg(
            "小时降水概率（%）",
            _hourly_series(submissions, "precipitation_probability"),
            "%",
            min_value=0,
            max_value=100,
        )
        hourly_wind_chart = _multi_series_line_chart_svg(
            "小时风速（m/s）",
            _hourly_series(submissions, "wind_speed"),
            "m/s",
            min_value=0,
        )
        hourly_cloud_chart = _multi_series_line_chart_svg(
            "小时云量（%）",
            _hourly_series(submissions, "cloud_cover"),
            "%",
            min_value=0,
            max_value=100,
        )
    else:
        temperature_chart = _line_chart_svg(
            "温度趋势（℃）",
            _summary_points(submissions, "max_temperature"),
            "#dc2626",
            "℃",
            second_points=_summary_points(submissions, "min_temperature"),
            second_color="#2563eb",
        )
        rain_chart = _bar_chart_svg("降水概率（%）", _summary_points(submissions, "rain_probability"), "#0f766e", "%", max_value=100)
        hourly_temperature_chart = _line_chart_svg("小时温度（℃）", _hourly_points(submissions, "temperature"), "#dc2626", "℃")
        hourly_rain_chart = _bar_chart_svg(
            "小时降水概率（%）",
            _hourly_points(submissions, "precipitation_probability"),
            "#0284c7",
            "%",
            max_value=100,
        )
        hourly_wind_chart = _line_chart_svg("小时风速（m/s）", _hourly_points(submissions, "wind_speed"), "#7c3aed", "m/s")
        hourly_cloud_chart = _bar_chart_svg("小时云量（%）", _hourly_points(submissions, "cloud_cover"), "#64748b", "%", max_value=100)
    hourly_tabs = _hourly_metric_tabs(selected_metrics)
    hourly_panels = _hourly_metric_panels(
        selected_metrics,
        {
            "temperature": ("小时温度", hourly_temperature_chart),
            "rain": ("小时降水概率", hourly_rain_chart),
            "wind": ("小时风速", hourly_wind_chart),
            "cloud": ("小时云量", hourly_cloud_chart),
        },
    )
    daily_panels = _daily_metric_panels(
        selected_metrics,
        {
            "temperature": ("温度趋势", temperature_chart),
            "rain": ("降水概率", rain_chart),
            "wind": (
                "风速趋势",
                _multi_series_line_chart_svg("风速趋势（m/s）", _summary_series(submissions, "wind_speed"), "m/s", min_value=0)
                if comparison_report
                else _bar_chart_svg("风速趋势（m/s）", _summary_points(submissions, "wind_speed"), "#7c3aed", "m/s"),
            ),
            "cloud": (
                "云量趋势",
                _multi_series_line_chart_svg("云量趋势（%）", _summary_series(submissions, "cloud_cover"), "%", min_value=0, max_value=100)
                if comparison_report
                else _bar_chart_svg("云量趋势（%）", _summary_points(submissions, "cloud_cover"), "#64748b", "%", max_value=100),
            ),
        },
    )
    hourly_header = _hourly_table_header(selected_metrics, include_region=comparison_report)
    hourly_rows = _hourly_table_rows(submissions, selected_metrics, include_region=comparison_report)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <style>
    :root {{
      --brand-1: #38bdf8; --brand-2: #0ea5e9; --brand-3: #0f766e;
      --ink: #0f1f33; --muted: #5b7088; --line: #e7eef6;
      --card: #ffffff; --bg: #eef3f9;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: var(--ink); background: linear-gradient(180deg, #e8f1fc 0%, var(--bg) 240px, var(--bg) 100%); -webkit-font-smoothing: antialiased; }}
    .page-shell {{ width: min(1140px, calc(100% - 40px)); margin: 0 auto; }}
    header {{ background: linear-gradient(135deg, #38bdf8 0%, #0ea5e9 45%, #0f766e 100%); color: #fff; box-shadow: 0 12px 32px rgba(14,116,144,.28); position: relative; overflow: hidden; }}
    header::after {{ content: ""; position: absolute; right: -70px; top: -70px; width: 260px; height: 260px; background: radial-gradient(circle, rgba(255,255,255,.20), rgba(255,255,255,0) 70%); }}
    header .page-shell {{ padding: 26px 0 30px; position: relative; z-index: 1; }}
    .brand {{ display: inline-flex; align-items: center; gap: 8px; background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.30); padding: 6px 13px; border-radius: 999px; font-size: 13px; font-weight: 600; letter-spacing: .2px; }}
    .brand-logo {{ font-size: 16px; line-height: 1; }}
    h1 {{ margin: 15px 0 6px; font-size: 27px; font-weight: 750; letter-spacing: .3px; }}
    .subtitle {{ margin: 0; color: rgba(255,255,255,.92); font-size: 14px; }}
    .report-scope {{ margin-top: 13px; color: rgba(255,255,255,.94); font-size: 14px; line-height: 1.7; }}
    .report-scope strong {{ color: #fff; }}
    main.page-shell {{ padding: 22px 0 40px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 18px 0 6px; }}
    .button {{ display: inline-flex; align-items: center; gap: 6px; border: 0; background: linear-gradient(135deg, #0ea5e9, #0f766e); color: #fff; text-decoration: none; padding: 10px 18px; border-radius: 999px; cursor: pointer; font: inherit; font-weight: 600; box-shadow: 0 6px 16px rgba(14,116,144,.26); transition: transform .15s ease, box-shadow .15s ease, opacity .15s ease; }}
    .button:hover {{ transform: translateY(-1px); box-shadow: 0 10px 22px rgba(14,116,144,.32); }}
    .button:active {{ transform: translateY(0); opacity: .92; }}
    .button.secondary {{ background: #fff; color: #0f766e; border: 1px solid #cfe6e3; box-shadow: 0 4px 12px rgba(2,32,71,.06); }}
    .section-title {{ display: flex; align-items: center; gap: 10px; margin: 30px 0 12px; font-size: 18px; font-weight: 700; color: var(--ink); }}
    .section-title::before {{ content: ""; width: 6px; height: 18px; border-radius: 999px; background: linear-gradient(180deg, #38bdf8, #0f766e); }}
    .chart-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin: 14px 0; }}
    .chart-panel {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 18px; box-shadow: 0 8px 24px rgba(2,32,71,.06); }}
    .chart-panel.wide {{ grid-column: 1 / -1; }}
    .chart-title {{ margin: 0 0 10px; font-size: 15px; font-weight: 600; color: #26405a; }}
    .chart {{ width: 100%; height: auto; display: block; }}
    .chart [data-tooltip] {{ cursor: crosshair; outline: none; }}
    .chart-tooltip {{ position: fixed; z-index: 9999; display: none; max-width: 260px; pointer-events: none; padding: 8px 11px; border-radius: 8px; background: rgba(15,23,42,.92); color: #fff; font-size: 13px; line-height: 1.45; box-shadow: 0 8px 24px rgba(15,23,42,.25); }}
    .metric-tabs {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }}
    .metric-tab {{ border: 1px solid var(--line); background: #f7fafc; color: #3a526b; border-radius: 999px; padding: 7px 16px; cursor: pointer; font: inherit; font-weight: 500; transition: all .15s ease; }}
    .metric-tab:hover {{ border-color: #9fd2cd; color: #0f766e; }}
    .metric-tab.is-active {{ border-color: transparent; background: linear-gradient(135deg, #0ea5e9, #0f766e); color: #fff; font-weight: 600; box-shadow: 0 4px 12px rgba(14,116,144,.28); }}
    .metric-chart {{ display: none; }}
    .metric-chart.is-active {{ display: block; }}
    .table-wrap {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 14px; border: 1px solid var(--line); box-shadow: 0 8px 24px rgba(2,32,71,.05); background: #fff; }}
    .table-wrap.compact {{ max-height: 460px; overflow: auto; }}
    table {{ width: 100%; min-width: 760px; border-collapse: collapse; background: #fff; }}
    th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; font-size: 14px; }}
    thead th {{ position: sticky; top: 0; background: linear-gradient(180deg, #f2f7fc, #e9f1f8); color: #2a3f57; font-weight: 600; z-index: 1; }}
    tbody tr:nth-child(even) {{ background: #fafcfe; }}
    tbody tr:hover {{ background: #eef7f6; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    .note {{ color: var(--muted); font-size: 13px; margin-top: 12px; }}
    .warning {{ color: #b4530f; }}
    .download-status {{ color: #466172; font-size: 13px; min-height: 18px; }}
    footer {{ text-align: center; color: var(--muted); font-size: 12px; padding: 26px 0 10px; }}
    @media (max-width: 760px) {{
      .page-shell {{ width: calc(100% - 24px); }}
      .chart-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="page-shell">
      <div class="brand"><span class="brand-logo">⛅</span><span>云云 · 气象预测小助手</span></div>
      <h1>{html.escape(page_title)}</h1>
      <p class="subtitle">逐小时 / 多日气象预测·一键导出 CSV、JSON，转发飞书群</p>
      {report_scope}
    </div>
  </header>
  <main class="page-shell">
    <div class="toolbar">
      <button class="button download-button" type="button" data-download-kind="csv" data-download-url="{html.escape(download_url)}" data-filename="{html.escape(csv_filename)}" data-mime="text/csv;charset=utf-8">下载CSV</button>
      <button class="button secondary download-button" type="button" data-download-kind="json" data-download-url="{html.escape(json_url)}" data-filename="{html.escape(json_filename)}" data-mime="application/json;charset=utf-8">下载JSON</button>
      <span id="download-status" class="download-status" role="status"></span>
    </div>
    <h2 class="section-title">小时级预测曲线</h2>
    <section class="chart-grid hourly-chart-grid">
      <div class="chart-panel wide">
        {hourly_tabs}
        {hourly_panels}
      </div>
    </section>
    <h2 class="section-title">按日汇总</h2>
    <section class="chart-grid">
      {daily_panels}
    </section>
    <h2 class="section-title">小时明细</h2>
    <div class="table-wrap compact">
      <table>
        <thead>{hourly_header}</thead>
        <tbody>{''.join(hourly_rows)}</tbody>
      </table>
    </div>
    <h2 class="section-title">日度汇总</h2>
    <div class="table-wrap">
      <table>
        <thead>{summary_table_header}</thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    {error_note}
    <p class="note">本页面仅用于 PowerPals 社区共建、评分和复盘，不构成交易建议、报价建议、投资建议或收益承诺。</p>
  </main>
  <script id="download-payload" type="application/json">{download_payload_script}</script>
  <script>
  (() => {{
    const payloadNode = document.getElementById("download-payload");
    const statusNode = document.getElementById("download-status");
    const payload = payloadNode ? JSON.parse(payloadNode.textContent || "{{}}") : {{}};
    const autoDownloadKind = {auto_download_script};
    const setStatus = (text) => {{ if (statusNode) statusNode.textContent = text; }};
    const tooltipNode = document.createElement("div");
    tooltipNode.className = "chart-tooltip";
    tooltipNode.setAttribute("role", "tooltip");
    document.body.appendChild(tooltipNode);
    const hideTooltip = () => {{
      tooltipNode.style.display = "none";
    }};
    const downloadFrame = () => {{
      let frame = document.getElementById("download-frame");
      if (!frame) {{
        frame = document.createElement("iframe");
        frame.id = "download-frame";
        frame.name = "download-frame";
        frame.style.display = "none";
        frame.setAttribute("aria-hidden", "true");
        document.body.appendChild(frame);
      }}
      return frame;
    }};
    const startUrlDownload = (button) => {{
      const url = button.dataset.downloadUrl;
      if (!url) return false;
      const filename = button.dataset.filename || "powerpals-weather";
      const separator = url.includes("?") ? "&" : "?";
      downloadFrame().src = `${{url}}${{separator}}_download_ts=${{Date.now()}}`;
      setStatus(`已开始下载 ${{filename}}`);
      return true;
    }};
    const showTooltip = (text, clientX, clientY) => {{
      if (!text) return;
      tooltipNode.textContent = text;
      tooltipNode.style.display = "block";
      tooltipNode.style.left = "0px";
      tooltipNode.style.top = "0px";
      const rect = tooltipNode.getBoundingClientRect();
      const gap = 12;
      const padding = 10;
      let left = clientX + gap;
      let top = clientY - rect.height - gap;
      if (left + rect.width + padding > window.innerWidth) left = clientX - rect.width - gap;
      if (top < padding) top = clientY + gap;
      left = Math.max(padding, Math.min(left, window.innerWidth - rect.width - padding));
      top = Math.max(padding, Math.min(top, window.innerHeight - rect.height - padding));
      tooltipNode.style.left = `${{left}}px`;
      tooltipNode.style.top = `${{top}}px`;
    }};
    const saveFile = (button, content) => {{
      const filename = button.dataset.filename || "powerpals-weather";
      const mime = button.dataset.mime || "text/plain;charset=utf-8";
      try {{
        const blob = new Blob([content], {{ type: mime }});
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = filename;
        link.rel = "noopener";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
        setStatus(`已开始下载 ${{filename}}`);
      }} catch (error) {{
        setStatus("下载失败，请刷新报告后重试");
      }}
    }};
    document.querySelectorAll("[data-download-kind]").forEach((button) => {{
      button.addEventListener("click", (event) => {{
        event.preventDefault();
        event.stopPropagation();
        if (startUrlDownload(button)) return;
        const kind = button.dataset.downloadKind;
        const content = payload[kind];
        if (!content) return;
        saveFile(button, content);
      }});
    }});
    if (autoDownloadKind) {{
      const button = document.querySelector(`[data-download-kind="${{autoDownloadKind}}"]`);
      if (button) setTimeout(() => button.click(), 250);
    }}
    document.querySelectorAll("[data-metric]").forEach((button) => {{
      button.addEventListener("click", () => {{
        const metric = button.dataset.metric;
        document.querySelectorAll("[data-metric]").forEach((item) => item.classList.toggle("is-active", item === button));
        document.querySelectorAll("[data-metric-chart]").forEach((panel) => {{
          panel.classList.toggle("is-active", panel.dataset.metricChart === metric);
        }});
      }});
    }});
    document.querySelectorAll("[data-tooltip]").forEach((node) => {{
      node.addEventListener("mouseenter", (event) => showTooltip(node.dataset.tooltip, event.clientX, event.clientY));
      node.addEventListener("mousemove", (event) => showTooltip(node.dataset.tooltip, event.clientX, event.clientY));
      node.addEventListener("mouseleave", hideTooltip);
      node.addEventListener("focus", () => {{
        const rect = node.getBoundingClientRect();
        showTooltip(node.dataset.tooltip, rect.left + rect.width / 2, rect.top);
      }});
      node.addEventListener("blur", hideTooltip);
    }});
  }})();
    </script>
</body>
</html>"""


def _hourly_metric_tabs(metrics: list[str]) -> str:
    labels = {"temperature": "温度", "rain": "降水", "wind": "风速", "cloud": "云量"}
    buttons = []
    active_metric = metrics[0]
    for metric in metrics:
        active_class = " is-active" if metric == active_metric else ""
        buttons.append(
            f'<button class="metric-tab{active_class}" type="button" data-metric="{html.escape(metric)}">{html.escape(labels[metric])}</button>'
        )
    return f'<div class="metric-tabs" role="tablist" aria-label="气象要素">{"".join(buttons)}</div>'


def _hourly_metric_panels(metrics: list[str], charts: dict[str, tuple[str, str]]) -> str:
    panels = []
    active_metric = metrics[0]
    for metric in metrics:
        title, chart = charts[metric]
        active_class = " is-active" if metric == active_metric else ""
        panels.append(
            f'<div class="metric-chart{active_class}" data-metric-chart="{html.escape(metric)}">'
            f'<h2 class="chart-title">{html.escape(title)}</h2>'
            f"{chart}"
            "</div>"
        )
    return "".join(panels)


def _daily_metric_panels(metrics: list[str], charts: dict[str, tuple[str, str]]) -> str:
    panels = []
    for metric in metrics:
        title, chart = charts[metric]
        panels.append(
            '<div class="chart-panel">'
            f'<h2 class="chart-title">{html.escape(title)}</h2>'
            f"{chart}"
            "</div>"
        )
    return "".join(panels)


def _is_multi_region_report(submissions: list[WeatherSubmission], download_query: dict[str, Any]) -> bool:
    regions = {submission.region for submission in submissions}
    if len(regions) > 1:
        return True
    query_regions = str(download_query.get("regions", "")).strip()
    return "," in query_regions or "，" in query_regions


def _report_scope_html(
    submissions: list[WeatherSubmission],
    download_query: dict[str, Any],
    comparison_report: bool,
) -> str:
    regions = _ordered_unique([submission.region for submission in submissions])
    if not regions and download_query.get("regions"):
        regions = [region.strip() for region in str(download_query.get("regions", "")).split(",") if region.strip()]
    dates = _ordered_unique([submission.target_date for submission in submissions])
    if dates:
        date_label = f"{dates[0]} 至 {dates[-1]}（{len(dates)}天）" if len(dates) > 1 else dates[0]
    else:
        date_label = str(download_query.get("target_date", ""))
    label = "对比地区" if comparison_report else "区域"
    region_text = " / ".join(regions) if regions else "-"
    return (
        '<p class="report-scope">'
        f"<strong>{html.escape(label)}：</strong>{html.escape(region_text)}"
        f"　<strong>预测范围：</strong>{html.escape(date_label)}"
        "</p>"
    )


def _summary_table_header(metrics: list[str]) -> str:
    columns = ["日期", "区域"]
    if "temperature" in metrics:
        columns.extend(["最高温", "最低温"])
    if "rain" in metrics:
        columns.append("降水概率")
    if "wind" in metrics:
        columns.append("风速")
    if "cloud" in metrics:
        columns.append("云量")
    if any(metric in metrics for metric in ("rain", "cloud")):
        columns.append("主要天气")
    if any(metric in metrics for metric in ("rain", "wind")):
        columns.append("高风险时段")
    return "<tr>" + "".join(f"<th>{html.escape(column)}</th>" for column in columns) + "</tr>"


def _summary_table_rows(submissions: list[WeatherSubmission], metrics: list[str]) -> list[str]:
    rows = []
    for submission in submissions:
        summary = submission.aggregated_forecast.summary
        cells = [html.escape(submission.target_date), html.escape(submission.region)]
        if "temperature" in metrics:
            cells.extend([_format_value(summary.max_temperature, "℃"), _format_value(summary.min_temperature, "℃")])
        if "rain" in metrics:
            cells.append(_format_value(summary.rain_probability, "%"))
        if "wind" in metrics:
            cells.append(_format_value(summary.wind_speed, " m/s"))
        if "cloud" in metrics:
            cells.append(_format_value(summary.cloud_cover, "%"))
        if any(metric in metrics for metric in ("rain", "cloud")):
            cells.append(html.escape(summary.main_weather))
        if any(metric in metrics for metric in ("rain", "wind")):
            cells.append(html.escape(summary.high_risk_period))
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    if rows:
        return rows
    return ['<tr><td colspan="2">暂无日度汇总数据。</td></tr>']


def _hourly_table_header(metrics: list[str], include_region: bool = False) -> str:
    columns = ["区域", "时间"] if include_region else ["时间"]
    if "temperature" in metrics:
        columns.append("温度")
    if "rain" in metrics:
        columns.append("降水概率")
    if "wind" in metrics:
        columns.append("风速")
    if "cloud" in metrics:
        columns.append("云量")
    return "<tr>" + "".join(f"<th>{html.escape(column)}</th>" for column in columns) + "</tr>"


def _summary_points(submissions: list[WeatherSubmission], field: str, include_region: bool = False) -> list[tuple[str, float]]:
    points = []
    for submission in submissions:
        value = getattr(submission.aggregated_forecast.summary, field)
        if value is None:
            continue
        date_label = submission.target_date[5:] if len(submission.target_date) >= 10 else submission.target_date
        label = f"{_short_region_label(submission.region)} {date_label}" if include_region else date_label
        points.append((label, float(value)))
    return points


def _hourly_points(submissions: list[WeatherSubmission], field: str, include_region: bool = False) -> list[tuple[str, float]]:
    points = []
    for submission in submissions:
        for point in submission.aggregated_forecast.points:
            value = getattr(point, field)
            if value is None:
                continue
            time_label = _hour_label(point.time)
            label = f"{_short_region_label(submission.region)} {time_label}" if include_region else time_label
            points.append((label, float(value)))
    return points


def _summary_series(submissions: list[WeatherSubmission], field: str) -> list[dict[str, Any]]:
    series_by_region: dict[str, list[tuple[str, float]]] = {}
    for submission in submissions:
        value = getattr(submission.aggregated_forecast.summary, field)
        if value is None:
            continue
        label = submission.target_date[5:] if len(submission.target_date) >= 10 else submission.target_date
        series_by_region.setdefault(submission.region, []).append((label, float(value)))
    return _chart_series(series_by_region)


def _hourly_series(submissions: list[WeatherSubmission], field: str) -> list[dict[str, Any]]:
    series_by_region: dict[str, list[tuple[str, float]]] = {}
    for submission in submissions:
        region_points = series_by_region.setdefault(submission.region, [])
        for point in submission.aggregated_forecast.points:
            value = getattr(point, field)
            if value is None:
                continue
            region_points.append((_hour_label(point.time), float(value)))
    return _chart_series(series_by_region)


def _chart_series(series_by_region: dict[str, list[tuple[str, float]]]) -> list[dict[str, Any]]:
    series = []
    for region, points in series_by_region.items():
        if not points:
            continue
        series.append(
            {
                "name": region,
                "label": _short_region_label(region),
                "points": points,
            }
        )
    return series


def _hourly_table_rows(submissions: list[WeatherSubmission], metrics: list[str], include_region: bool = False) -> list[str]:
    rows = []
    column_count = 1 + len(metrics) + (1 if include_region else 0)
    for submission in submissions:
        for point in submission.aggregated_forecast.points:
            cells = []
            if include_region:
                cells.append(html.escape(submission.region))
            cells.append(html.escape(_hour_label(point.time, include_date=True)))
            if "temperature" in metrics:
                cells.append(_format_value(point.temperature, "℃"))
            if "rain" in metrics:
                cells.append(_format_value(point.precipitation_probability, "%"))
            if "wind" in metrics:
                cells.append(_format_value(point.wind_speed, " m/s"))
            if "cloud" in metrics:
                cells.append(_format_value(point.cloud_cover, "%"))
            rows.append(
                "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
            )
    if rows:
        return rows
    return [f'<tr><td colspan="{column_count}">暂无小时级明细数据。</td></tr>']


def _ordered_unique(values: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _short_region_label(region: str) -> str:
    if "省" in region and not region.endswith("省"):
        return region.split("省", 1)[1] or region
    if "自治区" in region and not region.endswith("自治区"):
        return region.split("自治区", 1)[1] or region
    if "特别行政区" in region and not region.endswith("特别行政区"):
        return region.split("特别行政区", 1)[1] or region
    return region


def _download_payload(
    submissions: list[WeatherSubmission],
    download_query: dict[str, Any],
    errors: list[dict[str, str]] | None,
) -> dict[str, str]:
    json_payload = {
        "status": "partial" if errors else "ok",
        "start_date": download_query.get("target_date"),
        "days": download_query.get("days", len(submissions)),
        "submissions": [submission.model_dump(mode="json") for submission in submissions],
        "errors": errors or [],
    }
    if "regions" in download_query:
        json_payload["regions"] = [region.strip() for region in str(download_query.get("regions", "")).split(",") if region.strip()]
    else:
        json_payload["region"] = submissions[0].region if submissions else str(download_query.get("region", ""))
    return {
        "csv": "\ufeff" + weather_csv(submissions),
        "json": json.dumps(json_payload, ensure_ascii=False, indent=2),
    }


def _json_for_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _hour_label(value: str, include_date: bool = False) -> str:
    if "T" not in value:
        return value
    date_part, time_part = value.split("T", 1)
    hour = time_part[:5]
    if include_date:
        return f"{date_part} {hour}"
    return f"{date_part[5:]} {hour}" if len(date_part) >= 10 else hour


def _format_value(value: float | None, unit: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:g}{unit}"


def _tooltip_text(label: str, value: float, unit: str, series_label: str = "") -> str:
    label_text = f"{label} {series_label}".strip()
    return f"{label_text}：{value:g}{unit}"


def _tooltip_attr(label: str, value: float, unit: str, series_label: str = "") -> str:
    return html.escape(_tooltip_text(label, value, unit, series_label), quote=True)


def _combo_chart_svg(
    title: str,
    *,
    line_points: list[tuple[str, float]],
    bar_points: list[tuple[str, float]],
    line_label: str,
    bar_label: str,
    line_color: str,
    bar_color: str,
    line_unit: str,
    bar_unit: str,
    bar_max_value: float | None = None,
) -> str:
    labels = _merged_labels(line_points, bar_points)
    if not labels:
        return '<p class="note">暂无可绘制数据。</p>'

    line_map = {label: value for label, value in line_points}
    bar_map = {label: value for label, value in bar_points}
    line_values = list(line_map.values())
    bar_values = list(bar_map.values())
    width, height = 720, 280
    left, right, top, bottom = 56, 56, 34, 52
    plot_width = width - left - right
    plot_height = height - top - bottom

    line_min = min(line_values) if line_values else 0
    line_max = max(line_values) if line_values else 1
    if line_min == line_max:
        line_min -= 1
        line_max += 1
    bar_max = max(bar_max_value or 0, max(bar_values) if bar_values else 0, 1)
    slot = plot_width / len(labels)
    bar_width = max(2, min(16, slot * 0.55))

    def x_for(index: int) -> float:
        return left + plot_width * (index / max(1, len(labels) - 1))

    def y_for_line(value: float) -> float:
        return top + plot_height * (1 - ((value - line_min) / (line_max - line_min)))

    def y_for_bar(value: float) -> float:
        return top + plot_height * (1 - (value / bar_max))

    bars = []
    for index, label in enumerate(labels):
        value = bar_map.get(label)
        if value is None:
            continue
        bar_height = plot_height * (value / bar_max)
        x = left + slot * index + (slot - bar_width) / 2
        y = height - bottom - bar_height
        tooltip = _tooltip_attr(label, value, bar_unit, bar_label)
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{bar_color}" opacity="0.58" rx="2" data-tooltip="{tooltip}" tabindex="0">'
            f"<title>{html.escape(_tooltip_text(label, value, bar_unit, bar_label))}</title>"
            "</rect>"
        )

    coords = []
    for index, label in enumerate(labels):
        value = line_map.get(label)
        if value is None:
            continue
        coords.append(f"{x_for(index):.1f},{y_for_line(value):.1f}")
    line = (
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{line_color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />'
        if coords
        else ""
    )
    label_nodes = _axis_label_nodes(labels, left, right, width, height)
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
  <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#cbd5e1" />
  <line x1="{width - right}" y1="{top}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1" />
  <line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1" />
  <text x="8" y="{top + 4}" font-size="12" fill="#64748b">{line_max:.1f}{html.escape(line_unit)}</text>
  <text x="8" y="{height - bottom}" font-size="12" fill="#64748b">{line_min:.1f}{html.escape(line_unit)}</text>
  <text x="{width - right + 8}" y="{top + 4}" font-size="12" fill="#64748b">{bar_max:.0f}{html.escape(bar_unit)}</text>
  <text x="{width - right + 14}" y="{height - bottom}" font-size="12" fill="#64748b">0{html.escape(bar_unit)}</text>
  {''.join(bars)}
  {line}
  <rect x="{left}" y="10" width="11" height="11" fill="{bar_color}" opacity="0.58" rx="2" />
  <text x="{left + 16}" y="20" font-size="12" fill="#475569">{html.escape(bar_label)}</text>
  <line x1="{left + 106}" y1="16" x2="{left + 124}" y2="16" stroke="{line_color}" stroke-width="3" stroke-linecap="round" />
  <text x="{left + 130}" y="20" font-size="12" fill="#475569">{html.escape(line_label)}</text>
  {label_nodes}
</svg>"""


def _merged_labels(*series: list[tuple[str, float]]) -> list[str]:
    labels = []
    seen = set()
    for points in series:
        for label, _value in points:
            if label in seen:
                continue
            seen.add(label)
            labels.append(label)
    return labels


def _multi_series_line_chart_svg(
    title: str,
    series: list[dict[str, Any]],
    unit: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> str:
    usable_series = [item for item in series if item.get("points")]
    all_points = [point for item in usable_series for point in item["points"]]
    if not all_points:
        return '<p class="note">暂无可绘制数据。</p>'

    labels = _merged_labels(*[item["points"] for item in usable_series])
    label_index = {label: index for index, label in enumerate(labels)}
    values = [value for _label, value in all_points]
    minimum = min_value if min_value is not None else min(values)
    maximum = max_value if max_value is not None else max(values)
    if minimum == maximum:
        minimum -= 1
        maximum += 1

    width, height = 720, 260
    left, right, top, bottom = 56, 20, 40, 50
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x_for(label: str) -> float:
        index = label_index[label]
        return left + plot_width * (index / max(1, len(labels) - 1))

    def y_for(value: float) -> float:
        return top + plot_height * (1 - ((value - minimum) / (maximum - minimum)))

    chart_nodes = []
    legend_nodes = []
    for index, item in enumerate(usable_series):
        color = _series_color(index)
        display_name = str(item.get("label") or item.get("name") or f"系列{index + 1}")
        full_name = str(item.get("name") or display_name)
        points = item["points"]
        coords = " ".join(f"{x_for(label):.1f},{y_for(value):.1f}" for label, value in points)
        chart_nodes.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />'
        )
        for label, value in points:
            x = x_for(label)
            y = y_for(value)
            tooltip = _tooltip_attr(label, value, unit, full_name)
            tooltip_text = html.escape(_tooltip_text(label, value, unit, full_name))
            chart_nodes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}" opacity="0.86" />'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="transparent" data-tooltip="{tooltip}" tabindex="0" style="pointer-events: all;">'
                f"<title>{tooltip_text}</title>"
                "</circle>"
            )
        legend_x = left + (index % 4) * 150
        legend_y = 18 + (index // 4) * 16
        legend_nodes.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 20}" y2="{legend_y}" stroke="{color}" stroke-width="3" stroke-linecap="round" />'
            f'<text x="{legend_x + 26}" y="{legend_y + 4}" font-size="12" fill="#475569">{html.escape(display_name)}</text>'
        )

    label_nodes = _axis_label_nodes(labels, left, right, width, height, max_labels=7)
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
  <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#cbd5e1" />
  <line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1" />
  <text x="8" y="{top + 4}" font-size="12" fill="#64748b">{maximum:.1f}{html.escape(unit)}</text>
  <text x="8" y="{height - bottom}" font-size="12" fill="#64748b">{minimum:.1f}{html.escape(unit)}</text>
  {''.join(legend_nodes)}
  {''.join(chart_nodes)}
  {label_nodes}
</svg>"""


def _series_color(index: int) -> str:
    colors = ["#dc2626", "#2563eb", "#0f766e", "#7c3aed", "#ea580c", "#64748b"]
    return colors[index % len(colors)]


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

    def point_nodes(series: list[tuple[str, float]], point_color: str, series_label: str = "") -> str:
        nodes = []
        for index, (label, value) in enumerate(series):
            x = left + (width - left - right) * (index / max(1, len(series) - 1))
            y = top + (height - top - bottom) * (1 - ((value - minimum) / (maximum - minimum)))
            tooltip = _tooltip_attr(label, value, unit, series_label)
            tooltip_text = html.escape(_tooltip_text(label, value, unit, series_label))
            nodes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{point_color}" opacity="0.88" />'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="transparent" data-tooltip="{tooltip}" tabindex="0" style="pointer-events: all;">'
                f"<title>{tooltip_text}</title>"
                "</circle>"
            )
        return "".join(nodes)

    label_nodes = _axis_label_nodes(labels, left, right, width, height)
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
  {point_nodes(points, color, "最高温" if second_points else "")}
  {point_nodes(second_points or [], second_color, "最低温") if second_points else ""}
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
    label_stride = _label_stride(len(points))
    bars = []
    labels = []
    for index, (label, value) in enumerate(points):
        x = left + slot * index + (slot - bar_width) / 2
        bar_height = (height - top - bottom) * (value / maximum)
        y = height - bottom - bar_height
        tooltip = _tooltip_attr(label, value, unit)
        tooltip_text = html.escape(_tooltip_text(label, value, unit))
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}" rx="3" data-tooltip="{tooltip}" tabindex="0">'
            f"<title>{tooltip_text}</title>"
            "</rect>"
        )
        if index % label_stride == 0 or index == len(points) - 1:
            labels.append(
                f'<text x="{x + bar_width / 2:.1f}" y="{height - 16}" text-anchor="middle" font-size="12" fill="#64748b">{html.escape(_display_axis_label(label))}</text>'
            )
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
  <line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#cbd5e1" />
  <line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1" />
  <text x="8" y="{top + 4}" font-size="12" fill="#64748b">{maximum:.0f}{html.escape(unit)}</text>
  <text x="20" y="{height - bottom}" font-size="12" fill="#64748b">0{html.escape(unit)}</text>
  {''.join(bars)}
  {''.join(labels)}
</svg>"""


def _axis_label_nodes(
    labels: list[str],
    left: int,
    right: int,
    width: int,
    height: int,
    max_labels: int | None = None,
) -> str:
    if not labels:
        return ""
    stride = _label_stride(len(labels), max_labels=max_labels)
    nodes = []
    for index, label in enumerate(labels):
        if index % stride != 0 and index != len(labels) - 1:
            continue
        x = left + (width - left - right) * (index / max(1, len(labels) - 1))
        nodes.append(
            f'<text x="{x:.1f}" y="{height - 16}" text-anchor="middle" font-size="12" fill="#64748b">{html.escape(_display_axis_label(label))}</text>'
        )
    return "".join(nodes)


def _label_stride(count: int, max_labels: int | None = None) -> int:
    if max_labels and max_labels > 0:
        return max(1, (count + max_labels - 1) // max_labels)
    if count <= 10:
        return 1
    if count <= 24:
        return 3
    if count <= 72:
        return 6
    return max(8, count // 12)


def _display_axis_label(label: str) -> str:
    if " " not in label:
        return label
    date_part, hour = label.split(" ", 1)
    return label if hour == "00:00" else hour
