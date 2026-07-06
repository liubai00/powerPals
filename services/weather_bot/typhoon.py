from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

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

    def __init__(self, api_key: str | None, api_host: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.api_host = (api_host or "devapi.qweather.com").strip().rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, client: httpx.AsyncClient, path: str, **params: Any) -> dict[str, Any]:
        url = f"https://{self.api_host}{path}"
        response = await client.get(url, params=params, headers={"X-QW-Api-Key": self.api_key or ""})
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def list_storms(self, client: httpx.AsyncClient, basin: str, year: str) -> list[dict[str, Any]]:
        body = await self._get(client, "/v7/tropical/storm-list", basin=basin, year=year)
        if str(body.get("code")) != "200":
            return []
        return [s for s in (body.get("storm") or []) if isinstance(s, dict)]

    async def resolve_storm(self, client: httpx.AsyncClient, text: str, years: list[str]) -> dict[str, Any] | None:
        """在西北太平洋(NP)当年→上年台风列表里, 找名字出现在文本中的台风(同年内优先活跃)。"""
        for year in years:
            try:
                storms = await self.list_storms(client, "NP", year)
            except httpx.HTTPError:
                continue
            matches = [s for s in storms if str(s.get("name") or "").strip() and str(s.get("name")).strip() in text]
            if matches:
                matches.sort(key=lambda s: str(s.get("isActive")) in ("1", "true", "True"), reverse=True)
                return matches[0]
        return None

    async def storm_now(self, client: httpx.AsyncClient, stormid: str) -> dict[str, Any]:
        body = await self._get(client, "/v7/tropical/storm-track", stormid=stormid)
        now = body.get("now")
        return now if isinstance(now, dict) else {}

    async def storm_forecast(self, client: httpx.AsyncClient, stormid: str) -> list[dict[str, Any]]:
        body = await self._get(client, "/v7/tropical/storm-forecast", stormid=stormid)
        return [p for p in (body.get("forecast") or []) if isinstance(p, dict)]

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
        except httpx.HTTPError as exc:
            logger.warning("typhoon brief failed: %s", exc)
            return None
        return _format_brief(storm, now, forecast)


def _storm_number(stormid: str, year: str) -> str:
    try:
        num = int(stormid[-2:])
        return f"{year}年第{num}号台风，" if num else ""
    except (ValueError, IndexError):
        return ""


def _format_brief(storm: dict[str, Any], now: dict[str, Any], forecast: list[dict[str, Any]]) -> str:
    name = str(storm.get("name") or "该台风")
    stormid = str(storm.get("id") or "")
    year = str(storm.get("year") or "")
    active = str(storm.get("isActive")) in ("1", "true", "True")

    lines = ["【实时台风数据·来自和风气象，是最新权威事实，请以此为准】"]
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
        lines.append("未来路径预报（和风，取每日节点）：")
        for point in forecast[:5]:
            typ = _STORM_TYPE_CN.get(str(point.get("type")), str(point.get("type") or ""))
            wind = _to_float(point.get("windSpeed"))
            seg = f"  {point.get('fxTime', '')} → {point.get('lat')}°N，{point.get('lon')}°E {typ}"
            if wind is not None:
                seg += f"，风{int(wind)}m/s"
            lines.append(seg)

    lines.append(
        "说明：以上是最新监测与官方预报路径，属权威实时数据；若用户假设的登陆地点/时间/情景与该预报路径不一致，"
        "请明确指出那是情景假设，并给出台风目前的真实位置与最新预报路径，不要把用户举例里往年的历史台风数据当成当前事实。"
    )
    return "\n".join(lines)
