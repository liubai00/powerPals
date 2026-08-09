from __future__ import annotations

import re


_PRICE_DIRECTION_REQUEST = re.compile(
    r"(?:电价|价格|现货|日前|实时).{0,12}(?:涨|跌|上涨|下跌|走高|走低|走强|走弱|偏强|偏弱|上行|下行|利多|利空|看涨|看跌|维持高位|维持低位|保持高位|保持低位|保持强势|保持弱势|高位运行|低位运行|坚挺|疲软|抬升|回落|方向|承压)|"
    r"(?:涨|跌|上涨|下跌|走高|走低|走强|走弱|偏强|偏弱|上行|下行|利多|利空|看涨|看跌|维持高位|维持低位|高位运行|低位运行|坚挺|疲软|抬升|回落).{0,12}(?:电价|价格|现货)"
)
_BID_REQUEST = re.compile(
    r"报价|申报价|报多少|出价|竞价|(?:提高|降低|上调|下调)申报|申报(?:价格|电量|策略)|"
    r"(?:申报|报)(?:高|低)(?:一点|一些|点)?|(?:多报|少报)(?!告)(?:一点|一些|点)?|元/MWh|元每兆瓦时",
    re.IGNORECASE,
)
_POSITION_REQUEST = re.compile(
    r"仓位|持仓|做多|做空|买入|卖出|加仓|减仓|建仓|平仓|"
    r"(?:多头|空头).{0,8}(?:有利|不利|占优|优势|劣势)|"
    r"(?:买|卖).{0,8}(?:多少|\d+(?:\.\d+)?).{0,6}(?:MWh|兆瓦时)",
    re.IGNORECASE,
)
_TRADING_STRATEGY_REQUEST = re.compile(r"交易策略|套利策略|交易建议|怎么交易|如何交易")
_ACTUAL_LOAD_REQUEST = re.compile(
    r"(?:实际|当前|实时|系统|全网).{0,6}负荷|"
    r"负荷.{0,6}(?:实际|当前|实时|多少|几|MW|GW|兆瓦)",
    re.IGNORECASE,
)
_ACTUAL_GENERATION_REQUEST = re.compile(
    r"实际.{0,8}(?:出力|发电量|功率)|"
    r"(?:出力|发电量|少发|多发).{0,10}(?:多少|几|MW|GW|MWh|兆瓦)",
    re.IGNORECASE,
)
_HYDROPOWER_QUANT_REQUEST = re.compile(
    r"(?:降雨|来水|水文).{0,12}水电.{0,10}(?:增加|减少|多发|少发|增量|多少)|"
    r"水电.{0,12}(?:增加|减少|多发|少发|增量|多少)",
    re.IGNORECASE,
)
_HUB_HEIGHT_WIND_REQUEST = re.compile(
    r"(?:轮毂|机舱|风机高度).{0,10}(?:风速|风电出力|功率)|"
    r"(?:风速|风电出力|功率).{0,10}(?:轮毂|机舱|风机高度)",
    re.IGNORECASE,
)
_GRID_FAILURE_REQUEST = re.compile(
    r"(?:强对流|雷电|大风|暴雨|冰冻).{0,16}(?:电网|线路).{0,10}(?:故障|跳闸|断电|风险)|"
    r"(?:电网|线路).{0,12}(?:故障|跳闸|断电).{0,12}(?:强对流|雷电|大风|暴雨|冰冻)",
    re.IGNORECASE,
)
_UNSAFE_MARKET_CLAIM = re.compile(
    r"(?:建议|应该|应当|适合|可以).{0,8}(?:做多|做空|买入|卖出|加仓|减仓|建仓|平仓)|"
    r"(?:电价|价格|现货).{0,12}(?:涨|跌|上涨|下跌|走高|走低|走强|走弱|偏强|偏弱|上行|下行|利多|利空|看涨|看跌|维持高位|维持低位|保持高位|保持低位|保持强势|保持弱势|高位运行|低位运行|坚挺|疲软|抬升|回落|承压)|"
    r"(?:涨|跌|上涨|下跌|走高|走低|走强|走弱|偏强|偏弱|上行|下行|利多|利空|看涨|看跌|维持高位|维持低位|高位运行|低位运行|坚挺|疲软|抬升|回落).{0,8}(?:电价|价格|现货)|"
    r"(?:建议|应该|应当).{0,10}(?:提高|降低|上调|下调)?(?:报价|申报价|申报价格|出价)|"
    r"(?:建议|应该|应当|可以)?.{0,8}(?:(?:申报|报)(?:高|低)(?:一点|一些|点)?|(?:多报|少报)(?!告)(?:一点|一些|点)?)|"
    r"(?:天气|气象)?.{0,6}(?:多头|空头).{0,8}(?:有利|不利|占优|优势|劣势)|"
    r"(?:报价|申报价|申报价格|出价).{0,8}\d|"
    r"(?:实际)?(?:负荷|出力|发电量).{0,10}(?:增加|减少|少发|多发|达到|为|约).{0,6}"
    r"\d+(?:\.\d+)?(?:MW|GW|MWh|兆瓦|吉瓦|兆瓦时)",
    re.IGNORECASE,
)

_UNSAFE_OUTPUT_BOUNDARY = (
    "当前回答只能提供气象事实和气象侧风险代理。由于缺少可核验的实际负荷、出力、价格、"
    "机组、联络线和持仓数据，我不能判断实际负荷、出力或价格方向，也不能给出报价、仓位或交易指令。"
)


def weather_only_boundary_answer(user_text: str) -> str | None:
    """Return a deterministic answer when a weather-only interface lacks required power data."""

    compact = re.sub(r"\s+", "", user_text or "")
    if _HYDROPOWER_QUANT_REQUEST.search(compact):
        return (
            "当前只有气象信息时，降雨只能作为水文气象代理，不能换算水电增量。"
            "实际水电变化还需要可回溯的流域来水、前期土壤含水量、积雪融化、"
            "水库水位与调度、机组可用率等外部数据；我不会补造 MW、MWh 或流量数值。"
        )
    if _HUB_HEIGHT_WIND_REQUEST.search(compact):
        return (
            "当前仅有10米地面风时，不能给出轮毂高度风速或风电出力。"
            "这至少需要可核验的轮毂高度、粗糙度或稳定度参数、测风塔或对应高度数值模式，"
            "以及场站功率曲线；缺失时只能展示10米地面风资源代理。"
        )
    if _GRID_FAILURE_REQUEST.search(compact):
        return (
            "当前只能说明气象危险及其可能影响窗口，不能断言一定发生电网故障、"
            "跳闸或停电。实际风险还需结合官方预警、雷达临近资料、线路与设备状态、"
            "运维记录等可回溯外部数据核查。"
        )
    if _ACTUAL_LOAD_REQUEST.search(compact):
        return (
            "当前没有可核验的实际负荷数据，也不会根据天气补造 MW、GW 或 MWh 数值。"
            "如未提供可回溯的第三方或官方数据，我只能说明气象事实和气象侧负荷压力代理。"
        )
    if _ACTUAL_GENERATION_REQUEST.search(compact):
        return (
            "当前没有可核验的实际出力数据，也不会根据天气补造 MW、GW、MWh 或具体少发、多发数值。"
            "如未提供可回溯的第三方或官方数据，我只能说明光伏、风资源等气象侧资源代理。"
        )
    if _TRADING_STRATEGY_REQUEST.search(compact):
        return (
            "我不能仅凭天气给出交易策略或操作建议。交易决策需要可核验的负荷、出力、价格、"
            "机组、联络线、持仓、市场规则和风险约束数据；当前只能提供气象事实和气象侧风险代理。"
        )
    if _BID_REQUEST.search(compact):
        return (
            "我不能仅凭天气给出具体报价、申报价格或电量。报价还需要可核验的负荷与新能源预测、"
            "机组可用率、联络线、持仓、市场规则和风险约束数据；当前只能提供气象侧风险代理。"
        )
    if _POSITION_REQUEST.search(compact):
        return (
            "我不能仅凭天气给出买入、卖出、做多、做空或仓位指令，也不能建议具体兆瓦时数量。"
            "这类决策需要可核验的负荷、出力、价格、机组、联络线、持仓及风险约束数据；"
            "当前只能提供气象事实和气象侧风险代理。"
        )
    if _PRICE_DIRECTION_REQUEST.search(compact):
        return (
            "当前只能确认气象事实和气象侧风险代理。是否导致日前或实时价格上涨、下跌，仍需结合"
            "系统负荷预测、新能源预测、机组可用率、联络线、报价及市场规则复核；"
            "当前不能仅凭天气判断价格方向。"
        )
    return None


def enforce_weather_only_llm_answer(answer: str | None, *, fallback: str) -> str:
    """Fail closed if an LLM adds unsupported market facts or trading instructions."""

    if not answer:
        return fallback
    if contains_unsafe_weather_only_claim(answer):
        return _UNSAFE_OUTPUT_BOUNDARY
    return answer


def contains_unsafe_weather_only_claim(text: str | None) -> bool:
    """Detect unsupported power-market facts or instructions in generated text."""

    return bool(text and _UNSAFE_MARKET_CLAIM.search(re.sub(r"\s+", "", text)))
