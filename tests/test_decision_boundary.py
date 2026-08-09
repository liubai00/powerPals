from services.weather_bot.llm import answer_role_question, answer_weather_knowledge_question


class FailsIfCalledLlmClient:
    enabled = True

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        raise AssertionError("weather-only market boundary must run before the LLM")


class KnowledgeLlmClient:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        self.calls += 1
        return "高温会提高空调制冷需求，但实际负荷仍受生产活动等因素影响。"


class UnsafeMarketClaimLlmClient:
    enabled = True

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        return "高温会推高负荷，明天电价会上涨，建议做多100MWh。"


class EchoUnsafeMarketClaimLlmClient:
    enabled = True

    def __init__(self, answer: str):
        self.answer = answer

    async def chat(self, messages, *, temperature=0.2, max_tokens=600):
        return self.answer


async def test_weather_only_price_direction_question_returns_deterministic_boundary():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="根据明天山东的高温，日前电价会涨吗？",
        fallback="fallback",
    )

    assert "不能仅凭天气判断价格方向" in answer
    assert "负荷预测" in answer
    assert "机组可用率" in answer


async def test_weather_only_position_request_returns_deterministic_boundary():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="明天高温，建议我做多还是做空，买多少兆瓦时？",
        fallback="fallback",
    )

    assert "不能仅凭天气给出" in answer
    assert "仓位" in answer
    assert "兆瓦时" in answer


async def test_plain_buy_quantity_request_is_treated_as_a_position_instruction():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="应该买多少兆瓦时？",
        fallback="fallback",
    )

    assert "不能仅凭天气给出" in answer
    assert "兆瓦时" in answer


async def test_weather_only_bid_request_returns_deterministic_boundary():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="根据明天的天气，我应该报多少元/MWh？",
        fallback="fallback",
    )

    assert "不能仅凭天气给出具体报价" in answer
    assert "市场规则" in answer


async def test_weather_only_actual_load_request_does_not_invent_power_values():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="山东当前实际负荷是多少GW？",
        fallback="fallback",
    )

    assert "没有可核验的实际负荷数据" in answer
    assert "不会根据天气补造" in answer
    assert "GW" in answer


async def test_weather_only_actual_generation_request_does_not_invent_power_values():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="明天浙江光伏实际出力会少发500MWh吗？",
        fallback="fallback",
    )

    assert "没有可核验的实际出力数据" in answer
    assert "不会根据天气补造" in answer
    assert "资源代理" in answer


async def test_general_power_weather_knowledge_still_uses_llm():
    client = KnowledgeLlmClient()

    answer = await answer_weather_knowledge_question(
        client,
        user_text="为什么高温容易增加制冷负荷？",
        fallback="fallback",
    )

    assert client.calls == 1
    assert answer == "高温会提高空调制冷需求，但实际负荷仍受生产活动等因素影响。"


async def test_weather_role_answer_uses_the_same_price_boundary():
    answer = await answer_role_question(
        FailsIfCalledLlmClient(),
        bot_role="weather_forecast_bot",
        user_text="就按天气说，明天现货价格是涨还是跌？",
        fallback="fallback",
    )

    assert "不能仅凭天气判断价格方向" in answer


async def test_llm_cannot_add_market_claims_to_a_general_weather_answer():
    answer = await answer_weather_knowledge_question(
        UnsafeMarketClaimLlmClient(),
        user_text="高温会怎样影响电力系统？",
        fallback="fallback",
    )

    assert "电价会上涨" not in answer
    assert "做多100MWh" not in answer
    assert "只能提供气象事实和气象侧风险代理" in answer
    assert "不能判断实际负荷、出力或价格方向" in answer


async def test_non_trading_submission_question_is_not_misclassified_as_a_bid():
    client = KnowledgeLlmClient()

    answer = await answer_weather_knowledge_question(
        client,
        user_text="气象共测任务怎么申报？",
        fallback="fallback",
    )

    assert client.calls == 1
    assert answer == "高温会提高空调制冷需求，但实际负荷仍受生产活动等因素影响。"


async def test_weather_only_trading_strategy_request_returns_deterministic_boundary():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="只根据明天的高温，给我一套交易策略。",
        fallback="fallback",
    )

    assert "不能仅凭天气给出交易策略" in answer
    assert "风险约束数据" in answer


async def test_weather_only_boundary_covers_market_strength_bid_and_long_bias_phrasing():
    for question in (
        "山东现货大概率偏强，建议提高申报价",
        "明天电价存在上行空间",
        "天气对多头更有利",
    ):
        answer = await answer_weather_knowledge_question(
            FailsIfCalledLlmClient(),
            user_text=question,
            fallback="fallback",
        )

        assert answer != "fallback"
        assert any(term in answer for term in ("价格方向", "报价", "仓位", "交易"))


async def test_post_llm_boundary_rejects_market_strength_bid_and_long_bias_claims():
    for unsafe_answer in (
        "山东现货大概率偏强，建议提高申报价。",
        "明天电价存在上行空间。",
        "天气对多头更有利。",
    ):
        answer = await answer_weather_knowledge_question(
            EchoUnsafeMarketClaimLlmClient(unsafe_answer),
            user_text="解释高温对电力系统的一般影响",
            fallback="fallback",
        )

        assert unsafe_answer not in answer
        assert "只能提供气象事实和气象侧风险代理" in answer


async def test_weather_only_boundary_covers_colloquial_price_and_bid_phrasing():
    for question in (
        "气象条件支持电价维持高位",
        "明天现货价格看跌，可以少报一些",
        "预计明日电价看涨，建议申报高一点",
    ):
        answer = await answer_weather_knowledge_question(
            FailsIfCalledLlmClient(),
            user_text=question,
            fallback="fallback",
        )

        assert answer != "fallback"
        assert any(term in answer for term in ("价格方向", "报价", "申报", "交易"))


async def test_post_llm_boundary_rejects_colloquial_price_and_bid_claims():
    for unsafe_answer in (
        "预计明日电价看涨，建议申报高一点。",
        "明天现货价格看跌，可以少报一些。",
        "气象条件支持电价维持高位。",
    ):
        answer = await answer_weather_knowledge_question(
            EchoUnsafeMarketClaimLlmClient(unsafe_answer),
            user_text="解释高温对电力系统的一般影响",
            fallback="fallback",
        )

        assert unsafe_answer not in answer
        assert "只能提供气象事实和气象侧风险代理" in answer


async def test_price_direction_synonyms_fail_closed_before_and_after_the_llm():
    for unsafe_claim in (
        "气象条件利好现货，明日电价或将反弹",
        "高温对电价形成支撑，价格有望冲高",
        "天气使现货盘面转强，电价继续处在高位",
    ):
        pre_llm_answer = await answer_weather_knowledge_question(
            FailsIfCalledLlmClient(),
            user_text=unsafe_claim,
            fallback="fallback",
        )
        post_llm_answer = await answer_weather_knowledge_question(
            EchoUnsafeMarketClaimLlmClient(unsafe_claim),
            user_text="解释天气变化的一般影响",
            fallback="fallback",
        )

        assert "不能仅凭天气判断价格方向" in pre_llm_answer
        assert unsafe_claim not in post_llm_answer
        assert "不能判断实际负荷、出力或价格方向" in post_llm_answer


async def test_bid_price_and_quantity_synonyms_fail_closed_before_and_after_the_llm():
    for unsafe_claim in (
        "天气偏紧，建议把申报量往上调",
        "明日电量可以多申报一点",
        "气象转弱，可以压减报量",
        "申报价适当抬高更稳妥",
        "天气宽松时可以报便宜一些",
    ):
        pre_llm_answer = await answer_weather_knowledge_question(
            FailsIfCalledLlmClient(),
            user_text=unsafe_claim,
            fallback="fallback",
        )
        post_llm_answer = await answer_weather_knowledge_question(
            EchoUnsafeMarketClaimLlmClient(unsafe_claim),
            user_text="解释天气变化的一般影响",
            fallback="fallback",
        )

        assert "不能仅凭天气给出具体报价" in pre_llm_answer
        assert unsafe_claim not in post_llm_answer
        assert "报价、仓位或交易指令" in post_llm_answer


async def test_position_and_long_short_synonyms_fail_closed_before_and_after_the_llm():
    for unsafe_claim in (
        "高温延续，建议开多单",
        "天气转凉，现货可以偏空操作",
        "气象条件改善，可以增持多头头寸",
        "天气偏暖，建议看多现货",
        "把空头敞口适当加大",
        "当前仓位可以加重一些",
    ):
        pre_llm_answer = await answer_weather_knowledge_question(
            FailsIfCalledLlmClient(),
            user_text=unsafe_claim,
            fallback="fallback",
        )
        post_llm_answer = await answer_weather_knowledge_question(
            EchoUnsafeMarketClaimLlmClient(unsafe_claim),
            user_text="解释天气变化的一般影响",
            fallback="fallback",
        )

        assert "不能仅凭天气给出买入、卖出、做多、做空或仓位指令" in pre_llm_answer
        assert unsafe_claim not in post_llm_answer
        assert "报价、仓位或交易指令" in post_llm_answer


async def test_weather_only_hydropower_increment_request_does_not_invent_generation():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="四川降雨会让水电增加多少？",
        fallback="fallback",
    )

    assert "不能换算水电增量" in answer
    assert "流域来水" in answer
    assert "水文气象代理" in answer


async def test_ground_wind_is_not_misrepresented_as_hub_height_wind():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="没有轮毂高度模型时，甘肃轮毂高度风速是多少？",
        fallback="fallback",
    )

    assert "仅有10米地面风" in answer
    assert "不能给出轮毂高度风速" in answer
    assert "风电出力" in answer


async def test_severe_convection_is_not_presented_as_a_certain_grid_failure():
    answer = await answer_weather_knowledge_question(
        FailsIfCalledLlmClient(),
        user_text="广东强对流会不会导致电网故障？",
        fallback="fallback",
    )

    assert "只能说明气象危险" in answer
    assert "不能断言" in answer
    assert "电网故障" in answer
