from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.main import (
    _days_from_text,
    _needs_region_clarification,
    _needs_task_region_clarification,
    _region_from_text,
    _request_from_text,
    _task_request_from_text,
    create_app,
)
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


class CapturingForecastService:
    def __init__(self):
        self.seen_requests = []

    async def forecast(self, request):
        self.seen_requests.append(request)
        return WeatherSubmission(
            task_id=f"WEATHER-CN-WORKBENCH-{request.target_date.replace('-', '')}-DAYAHEAD-001",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time="2026-06-09T16:00:00+08:00",
            scope={
                "region": request.region,
                "target_date": request.target_date,
                "time_granularity": request.granularity,
                "location": {
                    "name": request.region,
                    "code": request.location_code,
                    "latitude": request.latitude,
                    "longitude": request.longitude,
                    "source": request.location_source,
                },
                "applicable_scenarios": ["负荷预测参考"],
            },
            provider_results=[],
            aggregated_forecast=AggregatedForecast(
                providers_used=["open_meteo"],
                points=[
                    ForecastPoint(
                        time=f"{request.target_date}T00:00:00+08:00",
                        temperature=28.0,
                        precipitation_probability=20.0,
                        wind_speed=2.0,
                        cloud_cover=60.0,
                    )
                ],
                summary=ForecastSummary(
                    max_temperature=32.0,
                    min_temperature=26.0,
                    rain_probability=20.0,
                    wind_speed=2.0,
                    cloud_cover=60.0,
                    main_weather="多云",
                    high_risk_period="无明显高风险时段",
                ),
            ),
            confidence={"score": 0.7, "description": "中等"},
            key_factors=["多源气象预报融合"],
            risk_notes=["局地短时天气存在不确定性"],
        )


class FailingOnDateForecastService(CapturingForecastService):
    def __init__(self, failing_date: str):
        super().__init__()
        self.failing_date = failing_date

    async def forecast(self, request):
        if request.target_date == self.failing_date:
            raise ValueError("No usable provider forecasts")
        return await super().forecast(request)


def test_weather_range_supports_16_days():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post("/api/weather/forecast/range", json={"region": "广州", "target_date": "2026-06-10", "days": 16})

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 16
    assert len(body["submissions"]) == 16


def test_weather_range_returns_partial_when_one_day_has_no_provider_data():
    service = FailingOnDateForecastService("2026-06-11")
    client = TestClient(create_app(forecast_service=service))

    response = client.post("/api/weather/forecast/range", json={"region": "广州", "target_date": "2026-06-10", "days": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert [item["target_date"] for item in body["submissions"]] == ["2026-06-10"]
    assert body["errors"] == [{"target_date": "2026-06-11", "error": "No usable provider forecasts"}]


def test_weather_command_extracts_province_region_and_days():
    request = _request_from_text("帮我查下辽宁未来三天的气象信息")

    assert request.region == "辽宁"
    assert request.days == 3


def test_weather_command_prefers_city_suffix_over_bare_province_alias():
    assert _region_from_text("帮我查下吉林市未来三天天气") == "吉林市"


def test_task_command_extracts_arbitrary_bare_city_without_changing_weather_parser():
    task_request = _task_request_from_text("发布珠海最近四天的气象任务")

    assert task_request.region == "珠海"
    assert task_request.days == 4
    assert _needs_task_region_clarification("发布珠海最近四天的气象任务") is False
    assert _needs_region_clarification("帮我查珠海最近四天的气象信息") is True


def test_weather_export_returns_excel_compatible_csv():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post("/api/weather/export", json={"region": "广州", "target_date": "2026-06-10", "days": 2})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "target_date,region,max_temperature" in response.text
    assert "2026-06-10" in response.text
    assert "2026-06-11" in response.text
    assert "task_id" not in response.text.lstrip("\ufeff").splitlines()[0].split(",")


def test_weather_export_json_returns_standard_submissions():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.get("/api/weather/export/json", params={"region": "广州", "target_date": "2026-06-10", "days": 2})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["status"] == "ok"
    assert len(body["submissions"]) == 2
    assert body["submissions"][0]["task_id"].startswith("WEATHER-CN-WORKBENCH-")


def test_weather_report_page_contains_download_and_table():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.get("/reports/weather", params={"region": "广州", "target_date": "2026-06-10", "days": 2})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "广州气象数据工作台" in response.text
    assert "/api/weather/export" in response.text
    assert "/api/weather/export/json" in response.text
    assert "chart-panel" in response.text
    assert "hourly-chart-grid" in response.text
    assert 'data-metric="temperature"' in response.text
    assert 'data-metric="rain"' in response.text
    assert 'data-metric="wind"' in response.text
    assert 'data-metric="cloud"' in response.text
    assert 'data-metric-chart="temperature"' in response.text
    assert 'data-metric-chart="cloud"' in response.text
    assert 'id="download-payload"' in response.text
    assert 'data-download-kind="csv"' in response.text
    assert 'data-download-kind="json"' in response.text
    assert 'class="page-shell"' in response.text
    assert "chart-tooltip" in response.text
    assert 'data-tooltip="' in response.text
    assert 'style="pointer-events: all;"' in response.text
    assert "<svg" in response.text
    assert "下载CSV" in response.text
    assert "下载JSON" in response.text
    assert 'data-download-url="/api/weather/export?' in response.text
    assert 'href="/api/weather/export?' not in response.text
    assert 'frame.id = "download-frame"' in response.text
    assert "startUrlDownload(button)" in response.text
    assert "小时明细" in response.text
    assert "最高/最低温趋势" in response.text
    assert "最高温" in response.text
    assert "最低温" in response.text
    assert "2026-06-10" in response.text


def test_weather_compare_report_contains_multi_region_downloads():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.get(
        "/reports/weather/compare",
        params={"regions": "广州,深圳", "target_date": "2026-06-10", "days": 2},
    )

    assert response.status_code == 200
    assert "多地区气象对比报告" in response.text
    assert "对比地区" in response.text
    assert "广东省广州市 / 广东省深圳市" in response.text
    assert "广州" in response.text
    assert "深圳" in response.text
    assert response.text.count("<th>区域</th>") >= 2
    assert "06-10 广东省广州市" in response.text
    assert "06-10 广东省深圳市" in response.text
    assert "06-10 00:00 广东省广州市" in response.text
    assert "06-10 00:00 广东省深圳市" in response.text
    assert 'data-download-url="/api/weather/compare/export?' in response.text
    assert "/api/weather/compare/export/json" in response.text
    assert [request.region for request in service.seen_requests] == ["广东省广州市"] * 2 + ["广东省深圳市"] * 2


def test_weather_compare_export_returns_all_requested_regions():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    csv_response = client.get(
        "/api/weather/compare/export",
        params={"regions": "广州,深圳", "target_date": "2026-06-10", "days": 2},
    )
    json_response = client.get(
        "/api/weather/compare/export/json",
        params={"regions": "广州,深圳", "target_date": "2026-06-10", "days": 2},
    )

    assert csv_response.status_code == 200
    assert json_response.status_code == 200
    assert csv_response.text.count("广州") == 2
    assert csv_response.text.count("深圳") == 2
    assert json_response.json()["regions"] == ["广东省广州市", "广东省深圳市"]
    assert len(json_response.json()["submissions"]) == 4


def test_weather_report_get_preserves_coordinate_query():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.get(
        "/reports/weather",
        params={
            "region": "经纬度 39.9042,116.4074",
            "latitude": 39.9042,
            "longitude": 116.4074,
            "target_date": "2026-06-10",
            "days": 1,
        },
    )

    assert response.status_code == 200
    assert service.seen_requests[0].latitude == 39.9042
    assert service.seen_requests[0].longitude == 116.4074
    assert "latitude=39.9042" in response.text
    assert "longitude=116.4074" in response.text


def test_weather_report_supports_auto_download_mode():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.get(
        "/reports/weather",
        params={"region": "广州", "target_date": "2026-06-10", "days": 1, "autodownload": "csv"},
    )

    assert response.status_code == 200
    assert 'const autoDownloadKind = "csv";' in response.text


def test_weather_report_can_focus_on_requested_metrics():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.get(
        "/reports/weather",
        params={"region": "广州", "target_date": "2026-06-10", "days": 2, "metrics": "rain,wind"},
    )

    assert response.status_code == 200
    assert 'data-metric="rain"' in response.text
    assert 'data-metric="wind"' in response.text
    assert 'data-metric="temperature"' not in response.text
    assert 'data-metric="cloud"' not in response.text
    assert "<th>降水概率</th>" in response.text
    assert "<th>风速</th>" in response.text
    assert "<th>温度</th>" not in response.text


def test_weather_report_downloads_reuse_recent_report_data():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    client.get("/reports/weather", params={"region": "广州", "target_date": "2026-06-10", "days": 2})
    assert len(service.seen_requests) == 2

    csv_response = client.get("/api/weather/export", params={"region": "广州", "target_date": "2026-06-10", "days": 2})
    json_response = client.get("/api/weather/export/json", params={"region": "广州", "target_date": "2026-06-10", "days": 2})

    assert csv_response.status_code == 200
    assert json_response.status_code == 200
    assert len(service.seen_requests) == 2


def test_weather_batch_forecasts_multiple_locations():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/api/weather/batch",
        json={
            "requests": [
                {"region": "广州", "target_date": "2026-06-10"},
                {"region": "深圳", "target_date": "2026-06-10"},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [item["region"] for item in body["submissions"]] == ["广州", "广东省深圳市"]


def test_location_favorite_can_be_used_as_forecast_alias(tmp_path):
    settings = Settings(local_locations_path=str(tmp_path / "locations.json"))
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    create_response = client.post(
        "/api/locations",
        json={"alias": "南沙基地", "name": "广州南沙", "latitude": 22.8016, "longitude": 113.5252},
    )
    forecast_response = client.post("/api/weather/forecast", json={"region": "南沙基地", "target_date": "2026-06-10"})

    assert create_response.status_code == 200
    assert forecast_response.status_code == 200
    body = forecast_response.json()
    assert body["scope"]["location"]["source"] == "favorite"
    assert body["scope"]["location"]["latitude"] == 22.8016


def test_news_digest_and_hydrology_export(tmp_path):
    settings = Settings(
        local_news_jsonl_path=str(tmp_path / "news.jsonl"),
        local_hydrology_jsonl_path=str(tmp_path / "hydrology.jsonl"),
    )
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    news_response = client.post(
        "/api/news/items",
        json={"title": "广东电力市场动态", "source": "示例来源", "url": "https://example.com/news", "tags": ["电力交易"]},
    )
    digest_response = client.get("/api/news/digest")
    hydro_response = client.post(
        "/api/hydrology/records",
        json={"station": "示例水库", "basin": "珠江", "water_level": 12.3, "flow": 456.7, "observed_at": "2026-06-10T08:00:00+08:00"},
    )
    hydro_export = client.get("/api/hydrology/export")

    assert news_response.status_code == 200
    assert digest_response.json()["count"] == 1
    assert "广东电力市场动态" in digest_response.json()["items"][0]["title"]
    assert hydro_response.status_code == 200
    assert hydro_export.status_code == 200
    assert "station,basin,water_level,flow,observed_at" in hydro_export.text


def test_feishu_weather_card_includes_report_and_download_links_when_public_base_url_is_set():
    settings = Settings(public_base_url="https://powerpals.example.com")
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 广州未来两天天气"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["report_url"].startswith("https://powerpals.example.com/reports/weather")
    assert body["download_url"].startswith("https://powerpals.example.com/api/weather/export")
    assert body["json_url"].startswith("https://powerpals.example.com/api/weather/export/json")
    assert "打开网页报告" in str(body["card"])
    assert "下载CSV" in str(body["card"])
    assert "下载JSON" in str(body["card"])
    assert "'tag': 'chart'" in str(body["card"])


def test_feishu_days_parser_supports_16_day_forecast_window():
    assert _days_from_text("广州未来16天天气") == 16
    assert _days_from_text("广州未来十六天气象预测") == 16


def test_feishu_days_parser_supports_recent_and_plain_day_ranges():
    assert _days_from_text("预测下最近四天的气象数据") == 4
    assert _days_from_text("广州近4天气象数据") == 4
    assert _days_from_text("广州接下来四日降雨趋势") == 4
    assert _days_from_text("广州4天天气") == 4
