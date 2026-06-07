from services.weather_bot.models import ForecastPoint, ForecastRequest, ProviderForecast
from services.weather_bot.service import ForecastService


class FakeProvider:
    name = "open_meteo"

    async def fetch(self, request: ForecastRequest) -> ProviderForecast:
        return ProviderForecast(
            provider=self.name,
            status="ok",
            points=[
                ForecastPoint(
                    time="2026-06-10T00:00:00+08:00",
                    temperature=28.0,
                    precipitation_probability=30.0,
                    wind_speed=3.0,
                    cloud_cover=70.0,
                )
            ],
        )


async def test_forecast_service_outputs_document_style_official_submission():
    result = await ForecastService(providers={"open_meteo": FakeProvider()}).forecast(
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
