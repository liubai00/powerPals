from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService


class FakeProvider:
    name = "open_meteo"

    def __init__(self, source_metadata: dict[str, str]):
        self._source_metadata = source_metadata
        self.source_endpoints = (source_metadata["source_url"],)

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=[
                ForecastPoint(
                    time=f"{request.target_date}T{hour:02d}:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=30.0,
                    wind_speed=3.0,
                    cloud_cover=70.0,
                )
                for hour in range(24)
            ],
            **self._source_metadata,
        )


async def test_forecast_service_outputs_document_style_official_submission(
    external_source_metadata,
    verified_test_source_registry,
    test_source_clock,
):
    service = ForecastService(
        providers={"open_meteo": FakeProvider(external_source_metadata("open_meteo"))},
        settings=Settings(_env_file=None, app_env="test"),
        source_registry=verified_test_source_registry(
            {"open_meteo": "https://open_meteo.weather.test/v1/forecast"}
        ),
        clock=test_source_clock,
    )
    result = await service.forecast(
        ForecastRequest(region="深圳", target_date="2026-06-10", providers=["open_meteo"])
    )

    assert result.submission_type == "official_submission"
    assert result.track == "weather_forecast"
    assert result.bot.bot_name == "PowerPals Weather Bot"
    assert result.scope.location["code"] == "440300"
    assert result.scope.applicable_scenarios == ["负荷预测参考", "新能源出力观察", "电价复盘辅助", "储能运行观察"]
    assert result.time_info.submit_time.endswith("+08:00")
    assert result.time_info.data_cutoff_time == "2026-06-09T16:00:00+08:00"
    assert result.data_profile.data_source_group == "公开数据组"
    assert result.payload.summary["main_weather"]
    assert result.explanation.business_readable_summary
    assert result.scoring_profile.participate_in_public_scorecard is True
    assert "自动交易指令" in result.scoring_profile.not_suitable_for
