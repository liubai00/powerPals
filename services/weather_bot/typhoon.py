from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from services.weather_bot.data_provenance import DataAvailabilityGate, ExternalDataRecord
from services.weather_bot.logging_safety import safe_error_summary
from services.weather_bot.source_registry import (
    SourcePolicy,
    SourceRegistry,
    same_source_endpoint,
)

logger = logging.getLogger(__name__)
SHANGHAI_TZ = timezone(timedelta(hours=8))

QWEATHER_TYPHOON_PROVIDER = "qweather_tropical_cyclone"
_TYPHOON_PATHS = (
    "/v7/tropical/storm-list",
    "/v7/tropical/storm-track",
    "/v7/tropical/storm-forecast",
)
_REQUIRED_POLICY_METRICS = frozenset(
    {
        "storm_id",
        "storm_name",
        "is_active",
        "observation_time",
        "latitude",
        "longitude",
        "wind_speed",
        "forecast_time",
    }
)
_STORM_FIELDS = ("id", "name", "basin", "year", "isActive")
_NOW_FIELDS = (
    "pubTime",
    "type",
    "lat",
    "lon",
    "pressure",
    "windSpeed",
    "moveSpeed",
    "moveDir",
)
_FORECAST_FIELDS = ("fxTime", "type", "lat", "lon", "windSpeed")
_MISSING_STORM_NAMES = frozenset(
    {"null", "none", "nan", "undefined", "unknown", "-", "--"}
)


class TyphoonDataUnavailable(RuntimeError):
    """The tropical-cyclone source did not pass its explicit data gate."""


@dataclass(frozen=True)
class _AdmittedTyphoonResponse:
    body: dict[str, Any]
    provenance: dict[str, str]

# 台风强度英文代码 → 中文
_STORM_TYPE_CN = {
    "TD": "热带低压",
    "TS": "热带风暴",
    "STS": "强热带风暴",
    "TY": "台风",
    "STY": "强台风",
    "SuperTY": "超强台风",
}

# 移动方向英文 → 中文
_DIR_CN = {
    "N": "北", "NNE": "北东北", "NE": "东北", "ENE": "东东北",
    "E": "东", "ESE": "东东南", "SE": "东南", "SSE": "南东南",
    "S": "南", "SSW": "南西南", "SW": "西南", "WSW": "西西南",
    "W": "西", "WNW": "西北偏西", "NW": "西北", "NNW": "北西北",
}

# 触发台风数据抓取的关键词
_TYPHOON_KEYWORDS = ("台风", "颱風", "飓风", "颶風", "typhoon", "热带气旋", "熱帶氣旋", "热带风暴")

# 主海域 NP=西北太平洋(含南海编号台风); 兜底海域覆盖飓风/其它气旋
_FALLBACK_BASINS = ("EP", "NI", "AT", "SI", "SP")


def mentions_typhoon(text: str) -> bool:
    lowered = text.lower()
    return any(k in text or k in lowered for k in _TYPHOON_KEYWORDS)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wind_ms_to_level(ms: float | None) -> str:
    """近中心最大风速(m/s) → 中国风力等级近似。"""
    if ms is None:
        return ""
    scale = [
        (24.4, "10级"), (28.4, "11级"), (32.6, "12级"), (36.9, "13级"),
        (41.4, "14级"), (46.1, "15级"), (50.9, "16级"), (61.2, "17级"),
    ]
    for upper, label in scale:
        if ms <= upper:
            return label
    return "17级以上"


def typhoon_years() -> list[str]:
    """当年 + 上一年: 覆盖跨年活跃或用户提到的上年台风。"""
    y = date.today().year
    return [str(y), str(y - 1)]


class TyphoonClient:
    """和风气象台风(热带气旋)接口: 台风名 → 实时路径 + 预报, 给 LLM 做权威 grounding。"""

    def __init__(
        self,
        api_key: str | None,
        api_host: str | None = None,
        timeout: float = 10.0,
        *,
        source_registry: SourceRegistry | None = None,
        source_policy: SourcePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.api_key = api_key
        self.api_host = (api_host or "devapi.qweather.com").strip().rstrip("/")
        self.timeout = timeout
        self.source_registry = source_registry
        self.source_policy = source_policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def enabled(self) -> bool:
        return bool((self.api_key or "").strip() and self._source_policy_allows_all_endpoints())

    def _source_policy_allows_all_endpoints(self) -> bool:
        registry = self.source_registry
        policy = self.source_policy
        if registry is None or policy is None:
            return False
        endpoints = tuple(self._endpoint(path) for path in _TYPHOON_PATHS)
        if any(endpoint is None for endpoint in endpoints):
            return False
        if (
            policy.provider != QWEATHER_TYPHOON_PROVIDER
            or policy.environment != registry.environment
            or policy.license_status != "verified"
            or "text_reference" not in policy.allowed_uses
            or policy.retention_policy not in {"derived_only", "metadata_only"}
            or not policy.attribution_required
            or not (policy.attribution_text or "").strip()
            or not _REQUIRED_POLICY_METRICS.issubset(policy.required_metrics)
        ):
            return False
        for endpoint in endpoints:
            assert endpoint is not None
            if registry.resolve(QWEATHER_TYPHOON_PROVIDER, endpoint) != policy:
                return False
            if not any(
                same_source_endpoint(prefix, endpoint)
                for prefix in policy.source_url_prefixes
            ):
                return False
        return True

    def _endpoint(self, path: str) -> str | None:
        candidate = f"https://{self.api_host}{path}"
        return candidate if same_source_endpoint(candidate, candidate) else None

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        **params: Any,
    ) -> _AdmittedTyphoonResponse:
        url = self._endpoint(path)
        if not self.enabled or path not in _TYPHOON_PATHS or url is None:
            raise TyphoonDataUnavailable("source_policy_rejected")
        response = await client.get(url, params=params, headers={"X-QW-Api-Key": self.api_key or ""})
        response.raise_for_status()
        response_endpoint = str(response.request.url.copy_with(query=None))
        if response.history or not same_source_endpoint(response_endpoint, url):
            raise TyphoonDataUnavailable("endpoint_mismatch")
        body = response.json()
        if not isinstance(body, dict):
            raise TyphoonDataUnavailable("provider_response_rejected")
        retrieved_at = self.clock()
        provider_issued_at = _provider_timestamp(body.get("updateTime"))
        policy = self.source_policy
        if (
            policy is None
            or not _timezone_aware(retrieved_at)
            or provider_issued_at is None
            or policy.max_age_seconds is None
            or retrieved_at >= provider_issued_at + timedelta(seconds=policy.max_age_seconds)
        ):
            raise TyphoonDataUnavailable("runtime_provenance_rejected")
        content_hash = sha256(response.content).hexdigest()
        source_url = str(response.request.url)
        record = ExternalDataRecord(
            source_id=QWEATHER_TYPHOON_PROVIDER,
            source_kind="structured_api",
            source_url=source_url,
            retrieved_at=retrieved_at,
            provider_issued_at=provider_issued_at,
            license_status=policy.license_status,
            allowed_uses={"text_reference"},
            content_sha256=content_hash,
            structured_values=True,
            retention_policy=policy.retention_policy,
        )
        decision = DataAvailabilityGate().evaluate(record, now=retrieved_at)
        if decision.status != "text_only":
            raise TyphoonDataUnavailable(f"data_availability_rejected:{decision.reason}")
        return _AdmittedTyphoonResponse(
            body=body,
            provenance={
                "provider": QWEATHER_TYPHOON_PROVIDER,
                "source_url": source_url,
                "retrieved_at": retrieved_at.isoformat(),
                "provider_issued_at": provider_issued_at.isoformat(),
                "content_sha256": content_hash,
                "attribution": (policy.attribution_text or "").strip(),
                "retention_policy": policy.retention_policy,
            },
        )

    async def list_storms(self, client: httpx.AsyncClient, basin: str, year: str) -> list[dict[str, Any]]:
        admitted = await self._get(client, "/v7/tropical/storm-list", basin=basin, year=year)
        body = admitted.body
        if str(body.get("code")) != "200":
            return []
        return [
            _minimal_storm_item(storm, admitted.provenance)
            for storm in (body.get("storm") or [])
            if isinstance(storm, dict)
        ]

    async def _match_in(self, client: httpx.AsyncClient, basin: str, year: str, text: str) -> dict[str, Any] | None:
        try:
            storms = await self.list_storms(client, basin, year)
        except httpx.HTTPError:
            return None
        matches = [
            storm
            for storm in storms
            if (name := _normalized_storm_name(storm.get("name"))) and name in text
        ]
        if not matches:
            return None
        matches.sort(key=lambda s: str(s.get("isActive")) in ("1", "true", "True"), reverse=True)
        return matches[0]

    async def resolve_storm(self, client: httpx.AsyncClient, text: str, years: list[str]) -> dict[str, Any] | None:
        """按台风中文名解析: 先查西北太平洋(NP, 含南海编号台风)当年→上年, 命中即返回(同年内优先活跃);
        再兜底其它海域(飓风/气旋)当年, 让非西太台风按中文名也能接。"""
        for year in years:
            hit = await self._match_in(client, "NP", year, text)
            if hit:
                return hit
        for basin in _FALLBACK_BASINS:
            hit = await self._match_in(client, basin, years[0], text)
            if hit:
                return hit
        return None

    async def storm_now(self, client: httpx.AsyncClient, stormid: str) -> dict[str, Any]:
        admitted = await self._get(client, "/v7/tropical/storm-track", stormid=stormid)
        body = admitted.body
        now = body.get("now")
        return (
            _minimal_item(now, _NOW_FIELDS, admitted.provenance)
            if isinstance(now, dict)
            else {}
        )

    async def storm_forecast(self, client: httpx.AsyncClient, stormid: str) -> list[dict[str, Any]]:
        admitted = await self._get(client, "/v7/tropical/storm-forecast", stormid=stormid)
        body = admitted.body
        return [
            _minimal_item(point, _FORECAST_FIELDS, admitted.provenance)
            for point in (body.get("forecast") or [])
            if isinstance(point, dict)
        ]

    async def brief_for_text(self, text: str, years: list[str] | None = None) -> str | None:
        """文本提到某个(当年/上年)台风 → 返回中文实时数据块; 否则 None。"""
        if not self.enabled or not mentions_typhoon(text):
            return None
        years = years or typhoon_years()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                storm = await self.resolve_storm(client, text, years)
                if not storm:
                    return None
                stormid = str(storm.get("id") or "")
                now = await self.storm_now(client, stormid)
                forecast = await self.storm_forecast(client, stormid)
        except (httpx.HTTPError, TyphoonDataUnavailable, ValueError) as exc:
            logger.warning("typhoon brief failed error_type=%s", safe_error_summary(exc))
            return None
        return _format_brief(storm, now, forecast)

    async def active_storms(self, basins: tuple[str, ...] = ("NP",), year: str | None = None) -> list[dict[str, Any]]:
        """当前活跃台风(默认西北太平洋含南海), 每条附实况 now; 供晨报播报。"""
        if not self.enabled:
            return []
        year = year or typhoon_years()[0]
        out: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for basin in basins:
                    for storm in await self.list_storms(client, basin, year):
                        if str(storm.get("isActive")) not in ("1", "true", "True"):
                            continue
                        now = await self.storm_now(client, str(storm.get("id") or ""))
                        out.append({"storm": storm, "now": now})
        except (httpx.HTTPError, TyphoonDataUnavailable, ValueError) as exc:
            logger.warning("active storms failed error_type=%s", safe_error_summary(exc))
            raise TyphoonDataUnavailable("provider_unavailable") from None
        return out


def _minimal_item(
    item: dict[str, Any],
    fields: tuple[str, ...],
    provenance: dict[str, str],
) -> dict[str, Any]:
    return {
        **{field: item[field] for field in fields if field in item},
        "_provenance": dict(provenance),
    }


def _minimal_storm_item(
    item: dict[str, Any],
    provenance: dict[str, str],
) -> dict[str, Any]:
    minimized = _minimal_item(item, _STORM_FIELDS, provenance)
    normalized_name = _normalized_storm_name(minimized.get("name"))
    if normalized_name is None:
        minimized.pop("name", None)
    else:
        minimized["name"] = normalized_name
    return minimized


def _normalized_storm_name(value: object) -> str | None:
    name = str(value or "").strip()
    if not name or name.casefold() in _MISSING_STORM_NAMES:
        return None
    return name


def _provider_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if _timezone_aware(parsed) else None


def _timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _storm_number(stormid: str, year: str) -> str:
    try:
        num = int(stormid[-2:])
        return f"{year}年第{num}号台风，" if num else ""
    except (ValueError, IndexError):
        return ""


def _format_brief(storm: dict[str, Any], now: dict[str, Any], forecast: list[dict[str, Any]]) -> str:
    evidence_items = [storm, now, *forecast]
    if any(not _verified_provenance(item.get("_provenance")) for item in evidence_items):
        return (
            "【台风实时数据不可用】来源或可追溯性校验未通过，不展示台风事实；"
            "这不代表无活跃台风。"
        )
    name = _normalized_storm_name(storm.get("name")) or "该台风"
    stormid = str(storm.get("id") or "")
    year = str(storm.get("year") or "")
    active = str(storm.get("isActive")) in ("1", "true", "True")

    lines = ["【可追溯台风接口数据·QWeather】"]
    head = f"台风：{name}（{_storm_number(stormid, year)}编号{stormid}）"
    head += "，当前状态：活跃" if active else "，当前状态：已停止编号/历史台风"
    lines.append(head)

    if now:
        typ = _STORM_TYPE_CN.get(str(now.get("type")), str(now.get("type") or ""))
        wind = _to_float(now.get("windSpeed"))
        level = _wind_ms_to_level(wind)
        cur = f"实况（{now.get('pubTime', '')}）：{typ}"
        if now.get("lat") and now.get("lon"):
            cur += f"，中心位于 {now.get('lat')}°N，{now.get('lon')}°E"
        if now.get("pressure"):
            cur += f"，中心气压 {now.get('pressure')}hPa"
        if wind is not None:
            cur += f"，近中心最大风速约 {int(wind)}m/s" + (f"（{level}）" if level else "")
        if now.get("moveSpeed") and now.get("moveDir"):
            direction = _DIR_CN.get(str(now.get("moveDir")), str(now.get("moveDir")))
            cur += f"，以约 {now.get('moveSpeed')}km/h 向{direction}方向移动"
        lines.append(cur)

    if forecast:
        lines.append("未来路径预报（QWeather 接口，取前5个节点）：")
        for point in forecast[:5]:
            typ = _STORM_TYPE_CN.get(str(point.get("type")), str(point.get("type") or ""))
            wind = _to_float(point.get("windSpeed"))
            seg = f"  {point.get('fxTime', '')} → {point.get('lat')}°N，{point.get('lon')}°E {typ}"
            if wind is not None:
                seg += f"，风{int(wind)}m/s"
            lines.append(seg)

    provenance = [item["_provenance"] for item in evidence_items]
    latest_retrieval = max(str(item["retrieved_at"]) for item in provenance)
    attributions = sorted({str(item["attribution"]) for item in provenance})
    source_urls = sorted({str(item["source_url"]) for item in provenance})
    lines.append(
        "来源：%s｜抓取：%s｜可追溯接口：%s"
        % (" / ".join(attributions), latest_retrieval, "、".join(source_urls))
    )
    lines.append(
        "边界：台风路径、近中心风和气压仅为气象侧证据；登陆影响、场站出力、负荷及交易影响"
        "须结合官方预警与电力业务数据另行判断。"
    )
    return "\n".join(lines)


def format_active_for_briefing(
    active: list[dict[str, Any]],
    *,
    market_ids: set[str] | None = None,
) -> str | None:
    """晨报用: 把活跃台风列成一段 lark_md; 无活跃台风返回 None(晨报当天不显示该段)。"""
    if not active:
        return None
    if any(not _verified_active_item(item) for item in active):
        return (
            "**🌀 台风实时数据不可用**\n"
            "台风数据未通过来源与可追溯性校验，本期不展示相关事实；这不代表无活跃台风。"
        )
    if market_ids is not None:
        active = [
            item
            for item in active
            if _verified_market_relevance(item, market_ids)
        ]
        if not active:
            return None
    lines = ["**🌀 当前活跃台风**"]
    for item in active:
        storm = item.get("storm") or {}
        now = item.get("now") or {}
        name = _normalized_storm_name(storm.get("name")) or "未命名台风"
        number = _storm_number(str(storm.get("id") or ""), str(storm.get("year") or ""))
        typ = _STORM_TYPE_CN.get(str(now.get("type")), str(now.get("type") or ""))
        details = "，".join(part for part in (number.rstrip("，"), typ) if part)
        seg = f"🌀 **{name}**" + (f"（{details}）" if details else "")
        if now.get("lat") and now.get("lon"):
            seg += f" 中心 {now.get('lat')}°N/{now.get('lon')}°E"
        wind = _to_float(now.get("windSpeed"))
        if wind is not None:
            seg += f"、近中心{int(wind)}m/s"
        if now.get("moveDir"):
            seg += f"、向{_DIR_CN.get(str(now.get('moveDir')), now.get('moveDir'))}移动"
        lines.append(seg)
        if market_ids is not None:
            affected = sorted(set(item.get("affected_market_ids") or []) & market_ids)
            valid_time = item.get("impact_valid_time") or {}
            lines.append(f"关联关注分析区：{'、'.join(affected)}")
            lines.append(
                "影响窗口："
                f"{_briefing_timestamp(valid_time.get('start'), '%m/%d %H:%M')}–"
                f"{_briefing_timestamp(valid_time.get('end'), '%m/%d %H:%M')}"
            )
    provenance = [
        item[part]["_provenance"]
        for item in active
        for part in ("storm", "now")
    ]
    latest_retrieval = max(str(item["retrieved_at"]) for item in provenance)
    attributions = sorted({str(item["attribution"]) for item in provenance})
    lines.append(
        "来源：%s｜更新时间：%s"
        % (
            "、".join(attributions),
            _briefing_timestamp(latest_retrieval, "%H:%M"),
        )
    )
    lines.append(
        "边界：台风路径与10米近中心风仅为气象侧证据；风电切出、光伏出力、负荷及交易影响"
        "须结合场站和电力数据另行判断。"
    )
    return "\n".join(lines)


def _briefing_timestamp(value: Any, pattern: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "未记录"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "未记录"
    return parsed.astimezone(SHANGHAI_TZ).strftime(pattern)


def _verified_market_relevance(item: dict[str, Any], market_ids: set[str]) -> bool:
    affected = item.get("affected_market_ids")
    valid_time = item.get("impact_valid_time")
    if (
        not isinstance(affected, list)
        or not set(str(value) for value in affected) & market_ids
        or not isinstance(valid_time, dict)
        or str(valid_time.get("timezone") or "") != "Asia/Shanghai"
    ):
        return False
    try:
        start = datetime.fromisoformat(str(valid_time.get("start")))
        end = datetime.fromisoformat(str(valid_time.get("end")))
    except (TypeError, ValueError):
        return False
    return bool(
        start.tzinfo is not None
        and start.utcoffset() is not None
        and end.tzinfo is not None
        and end.utcoffset() is not None
        and end > start
    )


def _verified_active_item(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    storm = item.get("storm")
    now = item.get("now")
    return bool(
        isinstance(storm, dict)
        and isinstance(now, dict)
        and _verified_provenance(storm.get("_provenance"))
        and _verified_provenance(now.get("_provenance"))
    )


def _verified_provenance(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("provider") != QWEATHER_TYPHOON_PROVIDER:
        return False
    if value.get("retention_policy") not in {"metadata_only", "derived_only"}:
        return False
    if not isinstance(value.get("attribution"), str) or not str(value["attribution"]).strip():
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(value.get("content_sha256") or "")):
        return False
    source_url = str(value.get("source_url") or "")
    parsed = urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    return _provider_timestamp(value.get("retrieved_at")) is not None
