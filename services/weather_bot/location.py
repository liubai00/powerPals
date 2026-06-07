from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastRequest


class ResolvedLocation(BaseModel):
    name: str
    code: str | None = None
    latitude: float
    longitude: float
    source: str
    country: str = "中国"
    province: str | None = None
    city: str | None = None


class FavoriteLocation(BaseModel):
    alias: str
    name: str
    latitude: float
    longitude: float
    code: str | None = None
    notes: str = ""


class LocationBook:
    def __init__(self, settings: Settings):
        self.path = Path(settings.local_locations_path)

    def list(self) -> list[FavoriteLocation]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        locations: list[FavoriteLocation] = []
        for item in payload:
            try:
                locations.append(FavoriteLocation.model_validate(item))
            except ValueError:
                continue
        return locations

    def get(self, alias: str) -> FavoriteLocation | None:
        normalized = alias.strip()
        for location in self.list():
            if location.alias == normalized:
                return location
        return None

    def upsert(self, location: FavoriteLocation) -> FavoriteLocation:
        locations = [item for item in self.list() if item.alias != location.alias]
        locations.append(location)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([item.model_dump(mode="json") for item in locations], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return location

    def delete(self, alias: str) -> bool:
        locations = self.list()
        kept = [item for item in locations if item.alias != alias]
        if len(kept) == len(locations):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([item.model_dump(mode="json") for item in kept], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True

    def resolve(self, alias: str) -> ResolvedLocation | None:
        favorite = self.get(alias)
        if not favorite:
            return None
        return ResolvedLocation(
            name=favorite.name,
            code=favorite.code,
            latitude=favorite.latitude,
            longitude=favorite.longitude,
            source="favorite",
        )


BUILTIN_LOCATIONS: dict[str, ResolvedLocation] = {
    "深圳": ResolvedLocation(
        name="广东省深圳市",
        code="440300",
        latitude=22.5431,
        longitude=114.0579,
        source="builtin",
        province="广东省",
        city="深圳市",
    ),
    "深圳市": ResolvedLocation(
        name="广东省深圳市",
        code="440300",
        latitude=22.5431,
        longitude=114.0579,
        source="builtin",
        province="广东省",
        city="深圳市",
    ),
    "广东省深圳市": ResolvedLocation(
        name="广东省深圳市",
        code="440300",
        latitude=22.5431,
        longitude=114.0579,
        source="builtin",
        province="广东省",
        city="深圳市",
    ),
    "广州": ResolvedLocation(
        name="广东省广州市",
        code="440100",
        latitude=23.1291,
        longitude=113.2644,
        source="builtin",
        province="广东省",
        city="广州市",
    ),
    "广州市": ResolvedLocation(
        name="广东省广州市",
        code="440100",
        latitude=23.1291,
        longitude=113.2644,
        source="builtin",
        province="广东省",
        city="广州市",
    ),
    "北京": ResolvedLocation(
        name="北京市",
        code="110000",
        latitude=39.9042,
        longitude=116.4074,
        source="builtin",
        province="北京市",
        city="北京市",
    ),
    "上海": ResolvedLocation(
        name="上海市",
        code="310000",
        latitude=31.2304,
        longitude=121.4737,
        source="builtin",
        province="上海市",
        city="上海市",
    ),
}


class LocationResolver:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.location_book = LocationBook(self.settings)

    async def resolve(self, request: ForecastRequest) -> ResolvedLocation:
        if request.latitude is not None and request.longitude is not None:
            return ResolvedLocation(
                name=request.region,
                code=request.location_code,
                latitude=request.latitude,
                longitude=request.longitude,
                source=request.location_source or "coordinates",
            )

        region = request.region.strip()
        builtin = BUILTIN_LOCATIONS.get(region)
        if builtin:
            return builtin

        favorite = self.location_book.resolve(region)
        if favorite:
            return favorite

        if self.settings.qweather_api_key:
            qweather = await self._resolve_with_qweather(region)
            if qweather:
                return qweather

        open_meteo = await self._resolve_with_open_meteo(region)
        if open_meteo:
            return open_meteo

        raise ValueError(f"Cannot resolve location: {region}")

    async def _resolve_with_qweather(self, region: str) -> ResolvedLocation | None:
        params = {"location": region, "key": self.settings.qweather_api_key, "range": "cn"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get("https://geoapi.qweather.com/v2/city/lookup", params=params)
            response.raise_for_status()
            body = response.json()
        locations = body.get("location") or []
        if not locations:
            return None
        item = locations[0]
        province = item.get("adm1")
        city = item.get("adm2") or item.get("name")
        return ResolvedLocation(
            name=_display_name(province, city, item.get("name")),
            code=item.get("id"),
            latitude=_to_float(item.get("lat")),
            longitude=_to_float(item.get("lon")),
            source="qweather_geo",
            country=item.get("country") or "中国",
            province=province,
            city=city,
        )

    async def _resolve_with_open_meteo(self, region: str) -> ResolvedLocation | None:
        params = {"name": region, "count": 1, "language": "zh", "format": "json"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get("https://geocoding-api.open-meteo.com/v1/search", params=params)
            response.raise_for_status()
            body = response.json()
        results: list[dict[str, Any]] = body.get("results") or []
        if not results:
            return None
        item = results[0]
        admin = item.get("admin1")
        name = item.get("name") or region
        return ResolvedLocation(
            name=_display_name(admin, name, None),
            code=None,
            latitude=float(item["latitude"]),
            longitude=float(item["longitude"]),
            source="open_meteo_geo",
            country=item.get("country") or "中国",
            province=admin,
            city=name,
        )


def apply_location(request: ForecastRequest, location: ResolvedLocation) -> ForecastRequest:
    return request.model_copy(
        update={
            "region": location.name,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "location_code": location.code,
            "location_source": location.source,
        }
    )


def location_payload(location: ResolvedLocation) -> dict[str, Any]:
    return {
        "name": location.name,
        "code": location.code,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "source": location.source,
        "country": location.country,
        "province": location.province,
        "city": location.city,
    }


def location_slug(location: ResolvedLocation) -> str:
    if location.code:
        return location.code
    return f"COORD-{_coordinate_token(location.latitude)}-{_coordinate_token(location.longitude)}"


def _coordinate_token(value: float) -> str:
    return f"{value:.4f}".replace(".", "_").replace("-", "M")


def _display_name(province: str | None, city: str | None, fallback: str | None) -> str:
    parts = [part for part in [province, city] if part]
    if parts:
        return "".join(dict.fromkeys(parts))
    return fallback or ""


def _to_float(value: Any) -> float:
    return float(value)
