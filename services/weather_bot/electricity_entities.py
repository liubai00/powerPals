"""Deterministic electricity-weather entity parsing.

This module is deliberately data-free: it normalizes user-supplied analysis
scope and time entities, but never retrieves or manufactures weather, load,
generation, price, position, or asset facts. Call it after ``NormalizedTurn``
and before context completion. Current-turn entities can then override saved
state, while ``PowerDataBoundary`` remains fail-closed until a separate,
traceable external-data gate has approved the required power datasets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from services.weather_bot.dates import parse_date_span
from services.weather_bot.location import PROVINCE_LEVEL_LOCATIONS
from services.weather_bot.power_briefing_markets import NATIONAL_MARKETS, MarketZone


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


@dataclass(frozen=True)
class EntityEvidence:
    """A grounded text span supporting one normalized entity."""

    entity_type: str
    raw_text: str
    normalized_value: str
    start: int
    end: int
    rule_id: str
    confidence: float


@dataclass(frozen=True)
class AnalysisAreaEntity:
    kind: Literal["provincial_area", "analysis_zone", "regional_collection", "national"]
    area_id: str
    name: str
    provincial_area: str | None
    analysis_zone_ids: tuple[str, ...]
    evidence: EntityEvidence
    confidence: float


@dataclass(frozen=True)
class ForecastPeriod:
    start_date: date
    end_date: date
    days: int
    requested_days: int
    status: Literal["ok", "truncated", "past", "beyond"]
    horizon_kind: Literal["single_day", "multi_day_trend", "extended_outlook"]
    evidence: EntityEvidence
    confidence: float


@dataclass(frozen=True)
class TradingWindow:
    kind: str
    label: str
    start_time: time
    end_time: time
    start_at: datetime | None
    end_at: datetime | None
    repeats_daily: bool
    window_source: str
    evidence: EntityEvidence
    confidence: float


@dataclass(frozen=True)
class MarketStageEntity:
    stage: Literal["day_ahead", "intraday", "real_time"]
    label: str
    evidence: EntityEvidence
    confidence: float


@dataclass(frozen=True)
class PowerDataRequirement:
    fact_type: Literal["actual_load", "actual_generation", "price"]
    evidence: EntityEvidence
    confidence: float


@dataclass(frozen=True)
class PowerDataBoundary:
    """Fail-closed contract for power facts when no external dataset is attached."""

    can_emit_power_facts: bool
    allowed_output_level: Literal["weather_proxy_only"]
    blocked_fact_types: tuple[str, ...]
    required_external_data: tuple[str, ...]
    reason: str
    requirements: tuple[PowerDataRequirement, ...]


@dataclass(frozen=True)
class ElectricityEntities:
    raw_text: str
    analysis_areas: tuple[AnalysisAreaEntity, ...]
    forecast_period: ForecastPeriod | None
    trading_window: TradingWindow | None
    data_boundary: PowerDataBoundary
    market_stage: MarketStageEntity | None = None
    confidence: float = 0.0
    clarification_required: bool = False
    clarification_reasons: tuple[str, ...] = ()


_ZONE_IDS_BY_PROVINCE: dict[str, tuple[str, ...]] = {}
for _market in NATIONAL_MARKETS:
    _ZONE_IDS_BY_PROVINCE.setdefault(_market.provincial_area, tuple())
    _ZONE_IDS_BY_PROVINCE[_market.provincial_area] += (_market.market_id,)


def _province_short_name(aliases: tuple[str, ...]) -> str:
    return min(aliases, key=len)


_PROVINCE_ALIASES: tuple[tuple[str, str, str], ...] = tuple(
    sorted(
        (
            (alias, _province_short_name(aliases), code[:2])
            for aliases, _canonical, code, _lat, _lon, _province, _city in PROVINCE_LEVEL_LOCATIONS
            for alias in aliases
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_PROVINCE_ABBREVIATION_TO_NAME: dict[str, str] = {
    "京": "北京", "津": "天津", "冀": "河北", "晋": "山西", "蒙": "内蒙古",
    "辽": "辽宁", "吉": "吉林", "黑": "黑龙江", "沪": "上海", "苏": "江苏",
    "浙": "浙江", "皖": "安徽", "闽": "福建", "赣": "江西", "鲁": "山东",
    "豫": "河南", "鄂": "湖北", "湘": "湖南", "粤": "广东", "桂": "广西",
    "琼": "海南", "渝": "重庆", "川": "四川", "蜀": "四川", "贵": "贵州",
    "黔": "贵州", "云": "云南", "滇": "云南", "藏": "西藏", "陕": "陕西",
    "秦": "陕西", "甘": "甘肃", "陇": "甘肃", "青": "青海", "宁": "宁夏",
    "新": "新疆", "台": "台湾", "港": "香港", "澳": "澳门",
}
_PROVINCE_CODE_BY_NAME: dict[str, str] = {
    _province_short_name(aliases): code[:2]
    for aliases, _canonical, code, _lat, _lon, _province, _city in PROVINCE_LEVEL_LOCATIONS
}
_ABBREVIATION_FOLLOW_RE = re.compile(
    r"(?:今天|今日|明天|明日|后天|未来|接下来|早峰|晚峰|午间|日前|日内|实时|全天|"
    r"天气|气象|高温|降雨|降水|负荷|光伏|风资源|强对流)"
)
_PROVINCE_SCOPE_FOLLOW_RE = re.compile(
    r"(?:\s|[，,、。；;：:]|和|与|及|全省|全区|全市|全境|境内|省内|区内|市内|"
    r"地区|范围|今天|今日|明天|明日|后天|未来|接下来|早峰|晚峰|午间|日前|日内|实时|全天|"
    r"天气|气象|高温|体感|温度|降雨|降水|负荷|光伏|风资源|风速|强对流|风险|提醒|Top|$)"
)

_REGIONAL_COLLECTIONS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("cn-region-north", "华北", ("北京", "天津", "河北", "山西", "内蒙古"), ("华北地区", "华北")),
    ("cn-region-northeast", "东北", ("辽宁", "吉林", "黑龙江"), ("东北地区", "东北")),
    ("cn-region-east", "华东", ("上海", "江苏", "浙江", "安徽", "福建", "江西", "山东"), ("华东地区", "华东")),
    ("cn-region-central", "华中", ("河南", "湖北", "湖南"), ("华中地区", "华中")),
    ("cn-region-south", "华南", ("广东", "广西", "海南"), ("华南地区", "华南")),
    ("cn-region-southwest", "西南", ("重庆", "四川", "贵州", "云南", "西藏"), ("西南地区", "西南")),
    ("cn-region-northwest", "西北", ("陕西", "甘肃", "青海", "宁夏", "新疆"), ("西北地区", "西北")),
)


def _collection_matches(text: str) -> list[tuple[int, int, AnalysisAreaEntity]]:
    configured_zone_ids = tuple(market.market_id for market in NATIONAL_MARKETS)
    candidates: list[tuple[str, str, str, tuple[str, ...], str]] = [
        (alias, "national", "cn-national", configured_zone_ids, "全国")
        for alias in ("全部分析区", "所有分析区", "全国范围", "全国")
    ]
    for area_id, name, provinces, aliases in _REGIONAL_COLLECTIONS:
        zone_ids = tuple(
            market.market_id for market in NATIONAL_MARKETS if market.provincial_area in provinces
        )
        candidates.extend((alias, "regional_collection", area_id, zone_ids, name) for alias in aliases)

    matches: list[tuple[int, int, AnalysisAreaEntity]] = []
    occupied: list[tuple[int, int]] = []
    for alias, kind, area_id, zone_ids, name in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        start = text.find(alias)
        if start < 0:
            continue
        end = start + len(alias)
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        evidence = EntityEvidence(
            entity_type="analysis_area",
            raw_text=text[start:end],
            normalized_value=area_id,
            start=start,
            end=end,
            rule_id="configured_area_collection",
            confidence=0.99,
        )
        matches.append(
            (
                start,
                end,
                AnalysisAreaEntity(
                    kind=kind,
                    area_id=area_id,
                    name=name,
                    provincial_area=None,
                    analysis_zone_ids=zone_ids,
                    evidence=evidence,
                    confidence=evidence.confidence,
                ),
            )
        )
        occupied.append((start, end))
    return matches


def _analysis_zone_matches(text: str) -> list[tuple[int, int, AnalysisAreaEntity]]:
    aliases: list[tuple[str, MarketZone]] = []
    for market in NATIONAL_MARKETS:
        base_name = market.market_name.removesuffix("样本区")
        market_aliases = {market.market_name, f"{base_name}分析区"}
        if market.scope_kind == "grid_region_sample":
            market_aliases.add(base_name)
        aliases.extend((alias, market) for alias in market_aliases)

    matches: list[tuple[int, int, AnalysisAreaEntity]] = []
    occupied: list[tuple[int, int]] = []
    seen_ids: set[str] = set()
    for alias, market in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        start = text.find(alias)
        while start >= 0:
            end = start + len(alias)
            if (
                market.market_id not in seen_ids
                and not any(start < used_end and end > used_start for used_start, used_end in occupied)
            ):
                name = market.market_name.removesuffix("样本区")
                evidence = EntityEvidence(
                    entity_type="analysis_area",
                    raw_text=text[start:end],
                    normalized_value=market.market_id,
                    start=start,
                    end=end,
                    rule_id="configured_analysis_zone",
                    confidence=1.0,
                )
                matches.append(
                    (
                        start,
                        end,
                        AnalysisAreaEntity(
                            kind="analysis_zone",
                            area_id=market.market_id,
                            name=name,
                            provincial_area=market.provincial_area,
                            analysis_zone_ids=(market.market_id,),
                            evidence=evidence,
                            confidence=evidence.confidence,
                        ),
                    )
                )
                occupied.append((start, end))
                seen_ids.add(market.market_id)
            start = text.find(alias, start + len(alias))
    return matches


def _province_entities(
    text: str,
    *,
    occupied: tuple[tuple[int, int], ...] = (),
) -> tuple[AnalysisAreaEntity, ...]:
    matches: list[tuple[int, int, AnalysisAreaEntity]] = []
    used_spans = list(occupied)
    for alias, name, code in _PROVINCE_ALIASES:
        start = text.find(alias)
        while start >= 0:
            end = start + len(alias)
            grounded_scope = end == len(text) or _PROVINCE_SCOPE_FOLLOW_RE.match(text, end) is not None
            if grounded_scope and not any(
                start < used_end and end > used_start for used_start, used_end in used_spans
            ):
                evidence = EntityEvidence(
                    entity_type="analysis_area",
                    raw_text=text[start:end],
                    normalized_value=name,
                    start=start,
                    end=end,
                    rule_id="province_alias",
                    confidence=0.98,
                )
                entity = AnalysisAreaEntity(
                    kind="provincial_area",
                    area_id=f"cn-province-{code}",
                    name=name,
                    provincial_area=name,
                    analysis_zone_ids=_ZONE_IDS_BY_PROVINCE.get(name, ()),
                    evidence=evidence,
                    confidence=evidence.confidence,
                )
                matches.append((start, end, entity))
                used_spans.append((start, end))
            start = text.find(alias, start + len(alias))
    return tuple(entity for _start, _end, entity in sorted(matches, key=lambda item: item[0]))


def _province_abbreviation_entities(
    text: str,
    *,
    occupied: tuple[tuple[int, int], ...],
) -> tuple[AnalysisAreaEntity, ...]:
    entities: list[AnalysisAreaEntity] = []
    used_spans = list(occupied)
    for index, raw in enumerate(text):
        name = _PROVINCE_ABBREVIATION_TO_NAME.get(raw)
        if name is None:
            continue
        if index > 0 and "\u4e00" <= text[index - 1] <= "\u9fff":
            continue
        if not _ABBREVIATION_FOLLOW_RE.match(text, index + 1):
            continue
        end = index + 1
        if any(index < used_end and end > used_start for used_start, used_end in used_spans):
            continue
        code = _PROVINCE_CODE_BY_NAME[name]
        evidence = EntityEvidence(
            entity_type="analysis_area",
            raw_text=raw,
            normalized_value=name,
            start=index,
            end=end,
            rule_id="province_abbreviation",
            confidence=0.8,
        )
        entities.append(
            AnalysisAreaEntity(
                kind="provincial_area",
                area_id=f"cn-province-{code}",
                name=name,
                provincial_area=name,
                analysis_zone_ids=_ZONE_IDS_BY_PROVINCE.get(name, ()),
                evidence=evidence,
                confidence=evidence.confidence,
            )
        )
        used_spans.append((index, end))
    return tuple(entities)


_DATE_EVIDENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:未来|接下来|最近|近)\s*(?:十六|十五|十四|十三|十二|十一|十|[一二两三四五六七八九]|\d+)\s*[天日]"),
    re.compile(r"(?:大后天|后天|明天|明日|今天|今日|今晚|今夜)"),
    re.compile(r"(?:下\s*)?(?:周|星期|礼拜)\s*[一二三四五六日天]?"),
    re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"),
    re.compile(r"(?:十[一二]?|[一二三四五六七八九]|\d{1,2})\s*月(?:\s*(?:上|中|下)\s*旬|\s*\d{1,2}\s*[日号])?"),
)
_LEAD_DAY_RANGE_RE = re.compile(
    r"(?:第\s*)?(?P<start>\d{1,2})\s*(?:[-–—~至到])\s*"
    r"(?P<end>\d{1,2})\s*天"
)


def _date_evidence(text: str) -> re.Match[str] | None:
    matches = [match for pattern in _DATE_EVIDENCE_PATTERNS if (match := pattern.search(text))]
    return min(matches, key=lambda match: (match.start(), -len(match.group(0))), default=None)


def _explicit_period(text: str, now: datetime) -> ForecastPeriod | None:
    lead_match = _LEAD_DAY_RANGE_RE.search(text)
    if lead_match is not None:
        lead_start = int(lead_match.group("start"))
        lead_end = int(lead_match.group("end"))
        if 1 <= lead_start <= lead_end:
            start_date = now.date() + timedelta(days=lead_start - 1)
            days = lead_end - lead_start + 1
            status: Literal["ok", "truncated", "past", "beyond"] = (
                "ok" if lead_end <= 16 else "beyond"
            )
            evidence = EntityEvidence(
                entity_type="forecast_period",
                raw_text=lead_match.group(0),
                normalized_value=f"lead_day_{lead_start}_{lead_end}",
                start=lead_match.start(),
                end=lead_match.end(),
                rule_id="lead_day_range",
                confidence=0.99,
            )
            return ForecastPeriod(
                start_date=start_date,
                end_date=start_date + timedelta(days=days - 1),
                days=days,
                requested_days=lead_end,
                status=status,
                horizon_kind="extended_outlook" if lead_start >= 8 or lead_end > 7 else "multi_day_trend",
                evidence=evidence,
                confidence=evidence.confidence,
            )
    match = _date_evidence(text)
    if match is not None:
        start_iso, days, requested_days, status = parse_date_span(text, today=now.date())
        start_date = date.fromisoformat(start_iso)
        raw = match.group(0)
        start = match.start()
        evidence = EntityEvidence(
            entity_type="forecast_period",
            raw_text=raw,
            normalized_value=f"{start_date.isoformat()}/{days}",
            start=start,
            end=start + len(raw),
            rule_id="date_span_parser",
            confidence=0.98,
        )
        return ForecastPeriod(
            start_date=start_date,
            end_date=start_date.fromordinal(start_date.toordinal() + days - 1),
            days=days,
            requested_days=requested_days,
            status=status,
            horizon_kind=(
                "single_day"
                if requested_days == 1
                else "multi_day_trend"
                if requested_days <= 7
                else "extended_outlook"
            ),
            evidence=evidence,
            confidence=evidence.confidence,
        )
    return None


_NAMED_WINDOWS: tuple[tuple[str, str, int, int, tuple[str, ...]], ...] = (
    ("midday_solar", "午间光伏", 11, 16, ("午间光伏", "光伏午间")),
    ("early_peak", "早峰", 7, 10, ("早高峰", "早峰")),
    ("evening_peak", "晚峰", 17, 21, ("晚高峰", "晚峰")),
)

_EXPLICIT_CLOCK_RANGE_RE = re.compile(
    r"(?<!\d)(?P<start>[01]?\d|2[0-3])"
    r"(?:(?:点|时)(?P<start_cn>[0-5]?\d)?分?|:(?P<start_colon>[0-5]\d))"
    r"\s*(?:到|至|-|~|—|－)\s*"
    r"(?P<end>[01]?\d|2[0-3])"
    r"(?:(?:点|时)(?P<end_cn>[0-5]?\d)?分?|:(?P<end_colon>[0-5]\d))"
)
_CLOCK_TOKEN_RE = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2})(?::(?P<minute>\d{2})|(?:点|时)(?P<minute_cn>\d{1,2})?分?)"
)
_AMBIGUOUS_SCOPE_RE = re.compile(r"(?:各|每个|各个)(?:省|地区|市场|分析区)|(?:所有|全部)(?:地区|市场)")
_RELATIVE_HOURS_RE = re.compile(r"未来\s*(?P<hours>\d{1,2})\s*小时")
_FULL_DAY_RE = re.compile(r"全天|全日")
_ELAPSED_WINDOW_RE = re.compile(r"(?:谷段|低谷时段).{0,8}(?:已过|过去|刚过)|(?:已过|过去|刚过).{0,8}(?:谷段|低谷时段)")


def _has_invalid_clock_time(text: str) -> bool:
    for match in _CLOCK_TOKEN_RE.finditer(text):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or match.group("minute_cn") or 0)
        if hour > 23 or minute > 59:
            return True
    return False


def _clarification_reasons(
    text: str,
    areas: tuple[AnalysisAreaEntity, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if _has_invalid_clock_time(text):
        reasons.append("invalid_clock_time")
    if not areas and _AMBIGUOUS_SCOPE_RE.search(text):
        reasons.append("ambiguous_analysis_scope")
    if _ELAPSED_WINDOW_RE.search(text):
        reasons.append("elapsed_trading_window")
    return tuple(reasons)


def _explicit_clock_window(text: str, period: ForecastPeriod | None) -> TradingWindow | None:
    match = _EXPLICIT_CLOCK_RANGE_RE.search(text)
    if match is None:
        return None
    start_minute = int(match.group("start_cn") or match.group("start_colon") or 0)
    end_minute = int(match.group("end_cn") or match.group("end_colon") or 0)
    start_time = time(int(match.group("start")), start_minute)
    end_time = time(int(match.group("end")), end_minute)
    start_at = end_at = None
    if period is not None:
        start_at = datetime.combine(period.start_date, start_time, tzinfo=SHANGHAI)
        end_date = period.start_date + (timedelta(days=1) if end_time <= start_time else timedelta())
        end_at = datetime.combine(end_date, end_time, tzinfo=SHANGHAI)
    evidence = EntityEvidence(
        entity_type="trading_window",
        raw_text=match.group(0),
        normalized_value="explicit_clock_range",
        start=match.start(),
        end=match.end(),
        rule_id="explicit_clock_range",
        confidence=1.0,
    )
    return TradingWindow(
        kind="explicit_clock_range",
        label="显式时段",
        start_time=start_time,
        end_time=end_time,
        start_at=start_at,
        end_at=end_at,
        repeats_daily=bool(period and period.days > 1),
        window_source="explicit_user_text",
        evidence=evidence,
        confidence=evidence.confidence,
    )


def _full_day_window(text: str, period: ForecastPeriod | None, now: datetime) -> TradingWindow | None:
    match = _FULL_DAY_RE.search(text)
    if match is None:
        return None
    target_date = period.start_date if period is not None else now.date()
    start_at = datetime.combine(target_date, time(0, 0), tzinfo=SHANGHAI)
    end_at = start_at + timedelta(days=1)
    evidence = EntityEvidence(
        entity_type="trading_window",
        raw_text=match.group(0),
        normalized_value="full_day",
        start=match.start(),
        end=match.end(),
        rule_id="explicit_full_day",
        confidence=1.0,
    )
    return TradingWindow(
        kind="full_day",
        label="全天",
        start_time=time(0, 0),
        end_time=time(23, 59, 59),
        start_at=start_at,
        end_at=end_at,
        repeats_daily=bool(period and period.days > 1),
        window_source="explicit_user_text",
        evidence=evidence,
        confidence=evidence.confidence,
    )


def _relative_hours_window(text: str, now: datetime) -> TradingWindow | None:
    match = _RELATIVE_HOURS_RE.search(text)
    if match is None:
        return None
    hours = int(match.group("hours"))
    if not 1 <= hours <= 72:
        return None
    end_at = now + timedelta(hours=hours)
    evidence = EntityEvidence(
        entity_type="trading_window",
        raw_text=match.group(0),
        normalized_value=f"relative_{hours}_hours",
        start=match.start(),
        end=match.end(),
        rule_id="relative_hour_window",
        confidence=0.99,
    )
    return TradingWindow(
        kind="relative_hours",
        label=f"未来{hours}小时",
        start_time=now.time().replace(tzinfo=None),
        end_time=end_at.time().replace(tzinfo=None),
        start_at=now,
        end_at=end_at,
        repeats_daily=False,
        window_source="current_clock",
        evidence=evidence,
        confidence=evidence.confidence,
    )
_POWER_MARKET_CONTEXT_RE = re.compile(
    r"(?:电力|电网|市场|交易|现货|价格|电价|负荷|出力|新能源|风电|光伏|分析区|风险)"
)
_POWER_FACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "actual_load",
        re.compile(
            r"(?:当前|实时|实际|系统|全网).{0,6}(?:实际)?负荷|"
            r"负荷.{0,6}(?:多少|几|实绩|实际值|MW|GW|兆瓦|万千瓦)"
        ),
    ),
    (
        "actual_generation",
        re.compile(
            r"(?:当前|实时|实际).{0,8}(?:光伏|风电|新能源|水电)?.{0,4}出力|"
            r"(?:光伏|风电|新能源|水电).{0,8}出力.{0,6}(?:多少|几|实际|当前|MW|GW|兆瓦|万千瓦)"
        ),
    ),
    (
        "price",
        re.compile(
            r"(?:电价|现货价格|日前价格|实时价格)|"
            r"价格.{0,6}(?:涨|跌|上涨|下跌|走高|走低|方向)"
        ),
    ),
)
_MARKET_STAGES: tuple[tuple[str, str, str, bool], ...] = (
    ("day_ahead", "日前", "日前", True),
    ("intraday", "日内", "日内", False),
    ("real_time", "实时", "实时", True),
)


def _market_stage(text: str) -> MarketStageEntity | None:
    for stage, label, marker, requires_power_context in _MARKET_STAGES:
        start = text.find(marker)
        if start < 0:
            continue
        if requires_power_context and not _POWER_MARKET_CONTEXT_RE.search(text):
            continue
        evidence = EntityEvidence(
            entity_type="market_stage",
            raw_text=text[start : start + len(marker)],
            normalized_value=stage,
            start=start,
            end=start + len(marker),
            rule_id="electricity_market_stage",
            confidence=0.95,
        )
        return MarketStageEntity(
            stage=stage,
            label=label,
            evidence=evidence,
            confidence=evidence.confidence,
        )
    return None


def _power_data_boundary(text: str) -> PowerDataBoundary:
    requirements: list[PowerDataRequirement] = []
    for fact_type, pattern in _POWER_FACT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        evidence = EntityEvidence(
            entity_type="power_data_requirement",
            raw_text=match.group(0),
            normalized_value=fact_type,
            start=match.start(),
            end=match.end(),
            rule_id="external_power_data_requirement",
            confidence=0.99,
        )
        requirements.append(
            PowerDataRequirement(
                fact_type=fact_type,
                evidence=evidence,
                confidence=evidence.confidence,
            )
        )
    blocked = tuple(requirement.fact_type for requirement in requirements)
    return PowerDataBoundary(
        can_emit_power_facts=False,
        allowed_output_level="weather_proxy_only",
        blocked_fact_types=blocked,
        required_external_data=blocked,
        reason="traceable_external_power_data_required",
        requirements=tuple(requirements),
    )


def _stage_trading_window(stage: MarketStageEntity | None, now: datetime) -> TradingWindow | None:
    if stage is None or stage.stage == "day_ahead":
        return None
    if stage.stage == "intraday":
        end_at = datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=SHANGHAI)
        kind = "intraday_remaining"
        label = "日内剩余时段"
    else:
        end_at = now
        kind = "real_time_snapshot"
        label = "实时快照"
    evidence = EntityEvidence(
        entity_type="trading_window",
        raw_text=stage.evidence.raw_text,
        normalized_value=kind,
        start=stage.evidence.start,
        end=stage.evidence.end,
        rule_id="market_stage_clock_window",
        confidence=stage.confidence,
    )
    return TradingWindow(
        kind=kind,
        label=label,
        start_time=now.time().replace(tzinfo=None),
        end_time=end_at.time().replace(tzinfo=None),
        start_at=now,
        end_at=end_at,
        repeats_daily=False,
        window_source="current_clock",
        evidence=evidence,
        confidence=evidence.confidence,
    )


def _named_trading_window(text: str, period: ForecastPeriod | None) -> TradingWindow | None:
    matched: tuple[str, str, int, int, str, int] | None = None
    for kind, label, start_hour, end_hour, aliases in _NAMED_WINDOWS:
        for alias in sorted(aliases, key=len, reverse=True):
            start = text.find(alias)
            if start < 0:
                continue
            candidate = (kind, label, start_hour, end_hour, alias, start)
            if matched is None or start < matched[-1] or (start == matched[-1] and len(alias) > len(matched[-2])):
                matched = candidate
    if matched is None:
        return None
    kind, label, start_hour, end_hour, marker, start = matched
    start_time = time(start_hour, 0)
    end_time = time(end_hour, 0)
    start_at = end_at = None
    if period is not None:
        start_at = datetime.combine(period.start_date, start_time, tzinfo=SHANGHAI)
        end_at = datetime.combine(period.start_date, end_time, tzinfo=SHANGHAI)
    evidence = EntityEvidence(
        entity_type="trading_window",
        raw_text=marker,
        normalized_value=kind,
        start=start,
        end=start + len(marker),
        rule_id="named_trading_window",
        confidence=0.95,
    )
    return TradingWindow(
        kind=kind,
        label=label,
        start_time=start_time,
        end_time=end_time,
        start_at=start_at,
        end_at=end_at,
        repeats_daily=bool(period and period.days > 1),
        window_source="default_trading_window",
        evidence=evidence,
        confidence=evidence.confidence,
    )


def parse_electricity_entities(text: str, *, now: datetime | None = None) -> ElectricityEntities:
    """Parse grounded electricity-weather scope and time entities."""

    current = now or datetime.now(tz=SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    collection_matches = _collection_matches(text)
    collection_spans = tuple((start, end) for start, end, _entity in collection_matches)
    zone_matches = [
        match
        for match in _analysis_zone_matches(text)
        if not any(
            match[0] < used_end and match[1] > used_start
            for used_start, used_end in collection_spans
        )
    ]
    province_entities = _province_entities(
        text,
        occupied=tuple(
            (start, end) for start, end, _entity in [*collection_matches, *zone_matches]
        ),
    )
    occupied_spans = tuple(
        (start, end) for start, end, _entity in [*collection_matches, *zone_matches]
    ) + tuple((entity.evidence.start, entity.evidence.end) for entity in province_entities)
    abbreviation_entities = _province_abbreviation_entities(text, occupied=occupied_spans)
    areas = tuple(
        entity
        for _start, _end, entity in sorted(
            [
                *collection_matches,
                *zone_matches,
                *((entity.evidence.start, entity.evidence.end, entity) for entity in province_entities),
                *((entity.evidence.start, entity.evidence.end, entity) for entity in abbreviation_entities),
            ],
            key=lambda item: item[0],
        )
    )
    market_stage = _market_stage(text)
    data_boundary = _power_data_boundary(text)
    period = _explicit_period(text, current)
    window = (
        _explicit_clock_window(text, period)
        or _full_day_window(text, period, current)
        or _relative_hours_window(text, current)
        or _named_trading_window(text, period)
        or _stage_trading_window(market_stage, current)
    )
    clarification_reasons = _clarification_reasons(text, areas)
    if "invalid_clock_time" in clarification_reasons:
        window = None
    confidences = [area.confidence for area in areas]
    if period is not None:
        confidences.append(period.confidence)
    if window is not None:
        confidences.append(window.confidence)
    if market_stage is not None:
        confidences.append(market_stage.confidence)
    confidences.extend(requirement.confidence for requirement in data_boundary.requirements)
    return ElectricityEntities(
        raw_text=text,
        analysis_areas=areas,
        forecast_period=period,
        trading_window=window,
        data_boundary=data_boundary,
        market_stage=market_stage,
        confidence=min(confidences, default=0.0),
        clarification_required=bool(clarification_reasons),
        clarification_reasons=clarification_reasons,
    )


__all__ = [
    "AnalysisAreaEntity",
    "ElectricityEntities",
    "EntityEvidence",
    "ForecastPeriod",
    "MarketStageEntity",
    "PowerDataBoundary",
    "PowerDataRequirement",
    "TradingWindow",
    "parse_electricity_entities",
]
