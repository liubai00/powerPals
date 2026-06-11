from fastapi.testclient import TestClient

from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuClient
from services.weather_bot.location import LocationResolver, ResolvedLocation
from services.weather_bot.main import create_app
from services.weather_bot.models import AggregatedForecast, ForecastPoint, ForecastSummary, WeatherSubmission


class CapturingForecastService:
    def __init__(self):
        self.seen_request = None
        self.seen_requests = []

    async def forecast(self, request):
        self.seen_request = request
        self.seen_requests.append(request)
        return WeatherSubmission(
            task_id="WEATHER-CN-440100-20260610-DAYAHEAD-001",
            region=request.region,
            target_date=request.target_date,
            data_cutoff_time="2026-06-09T16:00:00+08:00",
            provider_results=[],
            aggregated_forecast=AggregatedForecast(
                providers_used=["open_meteo"],
                points=[
                    ForecastPoint(
                        time="2026-06-10T00:00:00+08:00",
                        temperature=28.0,
                        precipitation_probability=20.0,
                        wind_speed=2.0,
                        cloud_cover=60.0,
                    )
                ],
                summary=ForecastSummary(
                    max_temperature=28.0,
                    min_temperature=28.0,
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


def test_feishu_event_supports_city_weather_forecast_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 广州明天天气"}}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "handled"
    assert response.json()["bot_role"] == "weather_forecast_bot"
    assert service.seen_request.region == "广州"


def test_feishu_event_supports_coordinate_weather_forecast_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 22.8016,113.5252 明天天气"}}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "handled"
    assert response.json()["bot_role"] == "weather_forecast_bot"
    assert service.seen_request.region == "经纬度 22.8016,113.5252"
    assert service.seen_request.latitude == 22.8016
    assert service.seen_request.longitude == 113.5252


def test_coordinate_weather_report_links_include_coordinates():
    service = CapturingForecastService()
    settings = Settings(public_base_url="https://powerpals.example.com")
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 39.9042,116.4074 最近三天气象信息"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert "latitude=39.9042" in body["report_url"]
    assert "longitude=116.4074" in body["report_url"]
    assert "latitude=39.9042" in body["download_url"]
    assert "longitude=116.4074" in body["json_url"]
    assert "下载CSV" in str(body["card"])
    assert "下载JSON" in str(body["card"])


def test_feishu_event_supports_city_weather_task_command():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 今日广州气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert body["task"]["task_id"].startswith("WEATHER-CN-440100-")
    assert body["task"]["region"] == "广东省广州市"


def test_feishu_event_supports_city_multi_day_weather_task_command():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 发布广州未来3天气象任务 2026-06-10"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert body["task"]["task_id"] == "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    assert body["task"]["forecast_days"] == 3
    assert body["task"]["forecast_end"] == "2026-06-12T23:00:00+08:00"


def test_task_feishu_event_supports_arbitrary_bare_city(monkeypatch):
    async def fake_resolve(self, request):
        assert request.region == "珠海"
        return ResolvedLocation(
            name="广东省珠海市",
            code="440400",
            latitude=22.2711,
            longitude=113.5767,
            source="geocoding",
            province="广东省",
            city="珠海市",
        )

    monkeypatch.setattr(LocationResolver, "resolve", fake_resolve)
    settings = Settings(feishu_task_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/task",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"发布珠海最近四天的气象任务"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert body["task"]["region"] == "广东省珠海市"
    assert body["task"]["location_code"] == "440400"
    assert body["task"]["forecast_days"] == 4


def test_feishu_task_command_without_region_asks_for_region():
    settings = Settings(feishu_task_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/task",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"发布最近四天气象任务"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_region"
    assert body["bot_role"] == "weather_task_bot"
    assert body["mode"] == "clarification"
    assert body["days"] == 4
    assert "缺少城市或区域" in body["text"]
    assert body.get("card") is None


def test_feishu_event_supports_coordinate_weather_task_command():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 22.8016,113.5252 今日气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert body["task"]["task_id"].startswith("WEATHER-CN-COORD-22_8016-113_5252-")
    assert body["task"]["region"] == "经纬度 22.8016,113.5252"


def test_feishu_event_supports_city_multi_day_weather_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 广州未来三天天气"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["days"] == 3
    assert len(body["submissions"]) == 3
    assert [request.region for request in service.seen_requests] == ["广州", "广州", "广州"]


def test_feishu_event_supports_recent_four_day_weather_command():
    service = CapturingForecastService()
    client = TestClient(create_app(forecast_service=service))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 广州最近四天的气象数据"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["days"] == 4
    assert len(body["submissions"]) == 4
    assert [request.region for request in service.seen_requests] == ["广州", "广州", "广州", "广州"]


def test_feishu_weather_command_without_region_asks_for_region():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"预测下最近四天的气象数据"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_region"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["mode"] == "clarification"
    assert body["days"] == 4
    assert "缺少城市或区域" in body["text"]
    assert body.get("card") is None
    assert service.seen_requests == []


def test_feishu_weather_region_clarification_can_continue_from_city_reply(monkeypatch):
    sent_texts = []
    sent_cards = []

    async def fake_send_text_message(self, chat_id, text):
        sent_texts.append((chat_id, text))
        return "clarify-message-id"

    async def fake_send_interactive_card(self, chat_id, card):
        sent_cards.append((chat_id, card))
        return "weather-card-message-id"

    monkeypatch.setattr(FeishuClient, "send_text_message", fake_send_text_message)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_interactive_card)
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    first = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_weather_chat",
                    "chat_type": "p2p",
                    "content": '{"text":"预测下最近四天的气象数据"}',
                }
            }
        },
    )
    second = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_weather_chat",
                    "chat_type": "p2p",
                    "content": '{"text":"广州"}',
                }
            }
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "needs_region"
    assert sent_texts and "缺少城市或区域" in sent_texts[0][1]
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "handled"
    assert body["days"] == 4
    assert len(body["submissions"]) == 4
    assert [request.region for request in service.seen_requests] == ["广州", "广州", "广州", "广州"]
    assert sent_cards


def test_feishu_event_treats_rain_trend_as_weather_command():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"上海未来7天降雨趋势"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["days"] == 7
    assert len(body["submissions"]) == 7
    assert [request.region for request in service.seen_requests] == ["上海"] * 7


def test_weather_feishu_event_focuses_on_requested_metric():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None, public_base_url="https://powerpals.example.com")
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"上海未来7天降雨趋势"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["metrics"] == ["rain"]
    assert "metrics=rain" in body["report_url"]
    assert "小时降水概率" in str(body["card"])
    assert "小时温度趋势" not in str(body["card"])


def test_weather_feishu_event_compares_multiple_regions():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None, public_base_url="https://powerpals.example.com")
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"帮我对比下广州和深圳未来三天的气象信息"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "weather_comparison"
    assert body["days"] == 3
    assert body["regions"] == ["广东省广州市", "广东省深圳市"]
    assert len(body["submissions"]) == 6
    assert [request.region for request in service.seen_requests] == ["广东省广州市"] * 3 + ["广东省深圳市"] * 3
    assert body["report_url"].startswith("https://powerpals.example.com/reports/weather/compare")
    assert body["download_url"].startswith("https://powerpals.example.com/api/weather/compare/export")
    assert body["json_url"].startswith("https://powerpals.example.com/api/weather/compare/export/json")
    assert body["card"]["card"]["header"]["title"]["content"] == "多地区气象对比"
    assert "对比结论" in str(body["card"])
    assert "打开网页报告" in str(body["card"])
    assert "下载CSV" in str(body["card"])
    assert "下载JSON" in str(body["card"])


def test_weather_feishu_event_rejects_more_than_four_comparison_regions():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_type": "p2p",
                    "content": '{"text":"帮我对比广东、深圳、上海、北京、辽宁未来三天气象信息"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "too_many_regions"
    assert body["mode"] == "weather_comparison"
    assert body["max_regions"] == 4
    assert len(body["regions"]) == 5
    assert "一次最多支持 4 个地区" in body["text"]
    assert service.seen_requests == []


def test_weather_feishu_event_answers_knowledge_question_without_region():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None, tavily_api_key=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_type": "p2p",
                    "content": '{"text":"解释天气数据来源、更新时间、预测不确定性"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "knowledge_answer"
    assert body.get("card") is None
    assert "数据来源" in body["text"]
    assert "更新时间" in body["text"]
    assert "不确定性" in body["text"]
    assert service.seen_requests == []


def test_weather_feishu_event_rejects_unsupported_metric_without_forecast_call():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"event": {"message": {"chat_type": "p2p", "content": '{"text":"广州湿度"}'}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsupported_metric"
    assert body["unsupported_metrics"] == ["湿度"]
    assert "还没有真正接入数据模型" in body["text"]
    assert service.seen_requests == []


def test_feishu_help_describes_two_weather_bots():
    client = TestClient(create_app(forecast_service=CapturingForecastService()))

    response = client.post(
        "/feishu/events",
        json={"event": {"message": {"content": "@机器人 帮助"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert "全国气象预测机器人" in body["text"]
    assert "气象任务发布机器人" in body["text"]


def test_weather_feishu_endpoint_only_handles_forecast_commands():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={"event": {"message": {"content": "@AI气象预测小助手 今日广州气象任务"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "redirect"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["suggested_bot_role"] == "weather_task_bot"
    assert body["suggested_bot_name"] == "气象任务发布机器人"
    assert body["suggested_event_path"] == "/feishu/events/task"
    assert "请找气象任务发布机器人" in body["text"]
    assert service.seen_request is None


def test_task_feishu_endpoint_only_handles_task_commands():
    service = CapturingForecastService()
    settings = Settings(feishu_task_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/task",
        json={"event": {"message": {"content": "@AI任务小助手 广州明天天气"}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "redirect"
    assert body["bot_role"] == "weather_task_bot"
    assert body["suggested_bot_role"] == "weather_forecast_bot"
    assert body["suggested_bot_name"] == "全国气象预测机器人"
    assert body["suggested_event_path"] == "/feishu/events/weather"
    assert "请找全国气象预测机器人" in body["text"]
    assert service.seen_request is None


def test_role_specific_feishu_tokens_are_checked_independently():
    settings = Settings(
        feishu_weather_verification_token="weather-token",
        feishu_task_verification_token="task-token",
    )
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    rejected = client.post(
        "/feishu/events/weather",
        json={
            "type": "url_verification",
            "token": "task-token",
            "challenge": "wrong-channel",
        },
    )
    accepted = client.post(
        "/feishu/events/weather",
        json={
            "type": "url_verification",
            "token": "weather-token",
            "challenge": "weather-channel",
        },
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {"challenge": "weather-channel"}


def test_weather_feishu_event_sends_card_to_event_chat(monkeypatch):
    sent = {}

    async def fake_send_interactive_card(self, chat_id, card):
        sent["chat_id"] = chat_id
        sent["card"] = card
        return "message-id-1"

    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_interactive_card)
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_test_chat",
                    "content": '{"text":"@AI气象预测小助手 广州明天天气"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["event_reply_message_id"] == "message-id-1"
    assert sent["chat_id"] == "oc_test_chat"
    assert sent["card"]["msg_type"] == "interactive"


def test_weather_feishu_event_replies_inside_topic(monkeypatch):
    calls = {}

    async def fake_reply_interactive_card(self, message_id, card):
        calls["reply_message_id"] = message_id
        calls["card"] = card
        return "topic-reply-message-id"

    async def fake_send_interactive_card(self, chat_id, card):
        calls["unexpected_chat_id"] = chat_id
        return "unexpected-main-chat-message-id"

    monkeypatch.setattr(FeishuClient, "reply_interactive_card", fake_reply_interactive_card)
    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_interactive_card)
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "message_id": "om_topic_user_message",
                    "root_id": "om_topic_root",
                    "chat_id": "oc_topic_chat",
                    "chat_type": "group",
                    "content": '{"text":"@_user_1 广州明天天气"}',
                    "mentions": [{"key": "@_user_1", "name": "云云"}],
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["event_reply_message_id"] == "topic-reply-message-id"
    assert calls["reply_message_id"] == "om_topic_user_message"
    assert calls["card"]["msg_type"] == "interactive"
    assert "unexpected_chat_id" not in calls


def test_weather_feishu_endpoint_handles_external_group_alias_mention():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_type": "group",
                    "content": '{"text":"@_user_1 广州明天天气"}',
                    "mentions": [{"key": "@_user_1", "name": "云云"}],
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert service.seen_request.region == "广州"


def test_weather_feishu_endpoint_handles_bot_app_id_mention():
    service = CapturingForecastService()
    settings = Settings(
        feishu_weather_app_id="cli_weather_test",
        feishu_weather_app_secret="secret",
        feishu_weather_verification_token=None,
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_type": "group",
                    "content": '{"text":"@_user_1 广州明天天气"}',
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "name": "外部显示名",
                            "id": {"app_id": "cli_weather_test"},
                        }
                    ],
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert service.seen_request.region == "广州"


def test_instant_weather_query_does_not_record_submission(monkeypatch, tmp_path):
    async def fake_send_interactive_card(self, chat_id, card):
        return "message-id-instant"

    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_interactive_card)
    submission_log = tmp_path / "weather_submissions.jsonl"
    settings = Settings(feishu_weather_verification_token=None, local_jsonl_path=str(submission_log))
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_test_chat",
                    "chat_type": "p2p",
                    "content": '{"text":"广州明天天气"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "instant_query"
    assert not submission_log.exists()


def test_weather_task_submission_records_after_card_reply(monkeypatch, tmp_path):
    sent = {}

    async def fake_send_interactive_card(self, chat_id, card):
        sent["chat_id"] = chat_id
        sent["card"] = card
        return "message-id-task"

    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_interactive_card)
    submission_log = tmp_path / "weather_submissions.jsonl"
    task_log = tmp_path / "weather_tasks.jsonl"
    task_id = "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    service = CapturingForecastService()
    settings = Settings(
        feishu_weather_verification_token=None,
        local_jsonl_path=str(submission_log),
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_test_chat",
                    "chat_type": "p2p",
                    "content": f'{{"text":"{task_id}"}}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "task_submission"
    assert body["task_id"] == task_id
    assert body["submission_record_status"] == "accepted"
    assert body["event_reply_message_id"] == "message-id-task"
    assert service.seen_request.region == "广东省广州市"
    assert service.seen_request.target_date == "2026-06-10"
    assert service.seen_request.days == 1
    assert sent["chat_id"] == "oc_test_chat"
    assert submission_log.exists()
    saved = submission_log.read_text(encoding="utf-8")
    assert task_id in saved
    assert "submitted_to_task" in saved

    submissions = client.get(f"/api/tasks/weather/{task_id}/submissions")
    assert submissions.status_code == 200
    assert submissions.json()["count"] == 1


def test_multi_day_weather_task_submission_records_all_days(monkeypatch, tmp_path):
    async def fake_send_interactive_card(self, chat_id, card):
        return "message-id-task-3d"

    monkeypatch.setattr(FeishuClient, "send_interactive_card", fake_send_interactive_card)
    submission_log = tmp_path / "weather_submissions.jsonl"
    task_log = tmp_path / "weather_tasks.jsonl"
    task_id = "WEATHER-CN-440100-20260610-DAYAHEAD-001"
    service = CapturingForecastService()
    settings = Settings(
        feishu_weather_verification_token=None,
        feishu_task_verification_token=None,
        local_jsonl_path=str(submission_log),
        local_task_jsonl_path=str(task_log),
    )
    client = TestClient(create_app(forecast_service=service, settings=settings))

    published = client.post(
        "/feishu/events/task",
        json={"event": {"message": {"content": "@AI任务小助手 发布广州未来3天气象任务 2026-06-10"}}},
    )
    assert published.status_code == 200
    assert published.json()["task"]["forecast_days"] == 3

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_test_chat",
                    "chat_type": "p2p",
                    "content": f'{{"text":"领取任务 {task_id}"}}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "task_submission"
    assert body["task_id"] == task_id
    assert body["days"] == 3
    assert body["submission_record_status"] == "accepted"
    assert body["submission_record_count"] == 3
    assert [request.target_date for request in service.seen_requests] == [
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    assert all(request.days == 3 for request in service.seen_requests)

    submissions = client.get(f"/api/tasks/weather/{task_id}/submissions")
    assert submissions.status_code == 200
    assert submissions.json()["count"] == 3


def test_task_feishu_endpoint_random_publish_creates_real_weather_task(monkeypatch):
    from services.weather_bot.location import BUILTIN_LOCATIONS

    monkeypatch.setattr("services.weather_bot.main._random_task_location", lambda: BUILTIN_LOCATIONS["深圳"])
    settings = Settings(feishu_task_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/task",
        json={
            "event": {
                "message": {
                    "chat_type": "p2p",
                    "content": '{"text":"先帮我随机发布个任务"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["mode"] == "random_weather_task"
    assert body["task"]["task_id"].startswith("WEATHER-CN-440300-")
    assert body["task"]["region"] == "广东省深圳市"
    assert body["card"]["card"]["header"]["title"]["content"] == "广东省深圳市气象预测任务"
    assert "WEATHER-CN-440300-" in body["text"]


def test_task_feishu_endpoint_ignores_messages_addressed_to_weather_bot():
    service = CapturingForecastService()
    settings = Settings(feishu_task_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/task",
        json={
            "event": {
                "message": {
                    "chat_id": "oc_test_chat",
                    "content": '{"text":"@AI气象预测小助手 广州明天天气"}',
                }
            }
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "bot_role": "weather_task_bot"}
    assert service.seen_request is None


def test_weather_feishu_endpoint_handles_direct_chat_without_mention():
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "event": {
                "message": {
                    "chat_type": "p2p",
                    "content": '{"text":"?"}',
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_forecast_bot"
    assert body["text"]


def test_task_feishu_endpoint_uses_mentions_to_detect_help_question():
    settings = Settings(feishu_task_verification_token=None)
    client = TestClient(create_app(forecast_service=CapturingForecastService(), settings=settings))

    response = client.post(
        "/feishu/events/task",
        json={
            "event": {
                "message": {
                    "content": '{"text":"@_user_1 你有什么作用"}',
                    "mentions": [{"key": "@_user_1", "name": "AI任务小助手"}],
                }
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "handled"
    assert body["bot_role"] == "weather_task_bot"
    assert "气象任务发布机器人" in body["text"]


def test_feishu_endpoint_deduplicates_retried_message_id():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_retry_once",
                "chat_type": "p2p",
                "content": '{"text":"广州明天天气"}',
            }
        },
    }

    first = client.post("/feishu/events/weather", json=payload)
    second = client.post("/feishu/events/weather", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "handled"
    assert second.status_code == 200
    assert second.json()["status"] == "ignored"
    assert second.json()["reason"] == "duplicate_message"
    assert len(service.seen_requests) == 1


def test_feishu_endpoint_ignores_read_events():
    service = CapturingForecastService()
    settings = Settings(feishu_weather_verification_token=None)
    client = TestClient(create_app(forecast_service=service, settings=settings))

    response = client.post(
        "/feishu/events/weather",
        json={
            "schema": "2.0",
            "header": {"event_type": "im.message.message_read_v1"},
            "event": {"message_id_list": ["om_read"]},
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert service.seen_requests == []
