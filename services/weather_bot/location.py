from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from services.weather_bot.config import Settings
from services.weather_bot.models import ForecastRequest


PROVINCE_SHORT_NAMES = {
    "河北",
    "山西",
    "辽宁",
    "吉林",
    "黑龙江",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "海南",
    "四川",
    "贵州",
    "云南",
    "陕西",
    "甘肃",
    "青海",
    "台湾",
}
MUNICIPALITY_SHORT_NAMES = {"北京", "天津", "上海", "重庆"}
AUTONOMOUS_REGION_SHORT_NAMES = {
    "内蒙古": "内蒙古自治区",
    "广西": "广西壮族自治区",
    "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区",
    "新疆": "新疆维吾尔自治区",
}


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

PROVINCE_LEVEL_LOCATIONS: tuple[tuple[tuple[str, ...], str, str, float, float, str, str], ...] = (
    (("北京市", "北京"), "北京市", "110000", 39.9042, 116.4074, "北京市", "北京市"),
    (("天津市", "天津"), "天津市", "120000", 39.3434, 117.3616, "天津市", "天津市"),
    (("河北省", "河北"), "河北省", "130000", 38.0428, 114.5149, "河北省", "石家庄市"),
    (("山西省", "山西"), "山西省", "140000", 37.8706, 112.5489, "山西省", "太原市"),
    (("内蒙古自治区", "内蒙古"), "内蒙古自治区", "150000", 40.8426, 111.7492, "内蒙古自治区", "呼和浩特市"),
    (("辽宁省", "辽宁"), "辽宁省", "210000", 41.8057, 123.4315, "辽宁省", "沈阳市"),
    (("吉林省", "吉林"), "吉林省", "220000", 43.8171, 125.3235, "吉林省", "长春市"),
    (("黑龙江省", "黑龙江"), "黑龙江省", "230000", 45.8038, 126.5349, "黑龙江省", "哈尔滨市"),
    (("上海市", "上海"), "上海市", "310000", 31.2304, 121.4737, "上海市", "上海市"),
    (("江苏省", "江苏"), "江苏省", "320000", 32.0603, 118.7969, "江苏省", "南京市"),
    (("浙江省", "浙江"), "浙江省", "330000", 30.2741, 120.1551, "浙江省", "杭州市"),
    (("安徽省", "安徽"), "安徽省", "340000", 31.8206, 117.2272, "安徽省", "合肥市"),
    (("福建省", "福建"), "福建省", "350000", 26.0745, 119.2965, "福建省", "福州市"),
    (("江西省", "江西"), "江西省", "360000", 28.6829, 115.8582, "江西省", "南昌市"),
    (("山东省", "山东"), "山东省", "370000", 36.6512, 117.1201, "山东省", "济南市"),
    (("河南省", "河南"), "河南省", "410000", 34.7466, 113.6254, "河南省", "郑州市"),
    (("湖北省", "湖北"), "湖北省", "420000", 30.5928, 114.3055, "湖北省", "武汉市"),
    (("湖南省", "湖南"), "湖南省", "430000", 28.2282, 112.9388, "湖南省", "长沙市"),
    (("广东省", "广东"), "广东省", "440000", 23.1291, 113.2644, "广东省", "广州市"),
    (("广西壮族自治区", "广西"), "广西壮族自治区", "450000", 22.8170, 108.3669, "广西壮族自治区", "南宁市"),
    (("海南省", "海南"), "海南省", "460000", 20.0444, 110.1999, "海南省", "海口市"),
    (("重庆市", "重庆"), "重庆市", "500000", 29.5630, 106.5516, "重庆市", "重庆市"),
    (("四川省", "四川"), "四川省", "510000", 30.5728, 104.0668, "四川省", "成都市"),
    (("贵州省", "贵州"), "贵州省", "520000", 26.6470, 106.6302, "贵州省", "贵阳市"),
    (("云南省", "云南"), "云南省", "530000", 25.0389, 102.7183, "云南省", "昆明市"),
    (("西藏自治区", "西藏"), "西藏自治区", "540000", 29.6500, 91.1000, "西藏自治区", "拉萨市"),
    (("陕西省", "陕西"), "陕西省", "610000", 34.3416, 108.9398, "陕西省", "西安市"),
    (("甘肃省", "甘肃"), "甘肃省", "620000", 36.0611, 103.8343, "甘肃省", "兰州市"),
    (("青海省", "青海"), "青海省", "630000", 36.6171, 101.7782, "青海省", "西宁市"),
    (("宁夏回族自治区", "宁夏"), "宁夏回族自治区", "640000", 38.4872, 106.2309, "宁夏回族自治区", "银川市"),
    (("新疆维吾尔自治区", "新疆"), "新疆维吾尔自治区", "650000", 43.8256, 87.6168, "新疆维吾尔自治区", "乌鲁木齐市"),
    (("台湾省", "台湾"), "台湾省", "710000", 25.0330, 121.5654, "台湾省", "台北市"),
    (("香港特别行政区", "香港"), "香港特别行政区", "810000", 22.3193, 114.1694, "香港特别行政区", "香港"),
    (("澳门特别行政区", "澳门"), "澳门特别行政区", "820000", 22.1987, 113.5439, "澳门特别行政区", "澳门"),
)

for aliases, name, code, latitude, longitude, province, city in PROVINCE_LEVEL_LOCATIONS:
    location = ResolvedLocation(
        name=name,
        code=code,
        latitude=latitude,
        longitude=longitude,
        source="builtin",
        province=province,
        city=city,
    )
    for alias in aliases:
        BUILTIN_LOCATIONS.setdefault(alias, location)

for location in list(BUILTIN_LOCATIONS.values()):
    BUILTIN_LOCATIONS.setdefault(location.name, location)


# Register provincial capital city names using the capital coordinates that are
# already present in PROVINCE_LEVEL_LOCATIONS (no new coordinate data introduced).
for _aliases, _name, _code, _lat, _lon, _province, _city in PROVINCE_LEVEL_LOCATIONS:
    if not _city.endswith("市") or _city == _name:
        continue
    _full = _name + _city
    _bare = _city[:-1]
    _city_loc = ResolvedLocation(
        name=_full,
        code=_code[:2] + "0100",
        latitude=_lat,
        longitude=_lon,
        source="builtin",
        province=_name,
        city=_city,
    )
    for _alias in (_bare, _city, _full):
        BUILTIN_LOCATIONS.setdefault(_alias, _city_loc)


# Other frequently-queried non-capital cities.
MAJOR_CITY_LOCATIONS: tuple[tuple[tuple[str, ...], str, str, float, float, str, str], ...] = (
    (("苏州", "苏州市"), "江苏省苏州市", "320500", 31.2989, 120.5853, "江苏省", "苏州市"),
    (("无锡", "无锡市"), "江苏省无锡市", "320200", 31.4912, 120.3119, "江苏省", "无锡市"),
    (("青岛", "青岛市"), "山东省青岛市", "370200", 36.0671, 120.3826, "山东省", "青岛市"),
    (("厦门", "厦门市"), "福建省厦门市", "350200", 24.4798, 118.0894, "福建省", "厦门市"),
    (("大连", "大连市"), "辽宁省大连市", "210200", 38.9140, 121.6147, "辽宁省", "大连市"),
    (("宁波", "宁波市"), "浙江省宁波市", "330200", 29.8683, 121.5440, "浙江省", "宁波市"),
    (("温州", "温州市"), "浙江省温州市", "330300", 27.9938, 120.6994, "浙江省", "温州市"),
    (("东莞", "东莞市"), "广东省东莞市", "441900", 23.0207, 113.7518, "广东省", "东莞市"),
    (("佛山", "佛山市"), "广东省佛山市", "440600", 23.0218, 113.1219, "广东省", "佛山市"),
    (("珠海", "珠海市"), "广东省珠海市", "440400", 22.2710, 113.5768, "广东省", "珠海市"),
    (("中山", "中山市"), "广东省中山市", "442000", 22.5170, 113.3927, "广东省", "中山市"),
    (("惠州", "惠州市"), "广东省惠州市", "441300", 23.1115, 114.4161, "广东省", "惠州市"),
)
for _aliases, _name, _code, _lat, _lon, _province, _city in MAJOR_CITY_LOCATIONS:
    _loc = ResolvedLocation(
        name=_name,
        code=_code,
        latitude=_lat,
        longitude=_lon,
        source="builtin",
        province=_province,
        city=_city,
    )
    for _alias in (*_aliases, _name):
        BUILTIN_LOCATIONS.setdefault(_alias, _loc)


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

        nominatim = await self._resolve_with_nominatim(region)
        if nominatim:
            return nominatim

        open_meteo = await self._resolve_with_open_meteo(region)
        if open_meteo:
            return open_meteo

        raise ValueError(f"Cannot resolve location: {region}")

    async def _resolve_with_qweather(self, region: str) -> ResolvedLocation | None:
        host = (self.settings.qweather_api_host or "geoapi.qweather.com").strip().rstrip("/")
        params = {"location": region, "range": "cn", "number": 5, "lang": "zh"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"https://{host}/geo/v2/city/lookup",
                params=params,
                headers={"X-QW-Api-Key": self.settings.qweather_api_key or ""},
            )
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

    async def _resolve_with_nominatim(self, region: str) -> ResolvedLocation | None:
        params = {
            "q": f"{region}, 中国",
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "cn",
            "accept-language": "zh-CN",
        }
        headers = {"User-Agent": "powerpals-weather-agent/0.1"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers)
                response.raise_for_status()
                results = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(results, list) or not results:
            return None
        item = _best_nominatim_result(results, region)
        if not item:
            return None
        name = item.get("name") or region
        province = _nominatim_province(item.get("display_name"))
        return ResolvedLocation(
            name=_display_name(province, name, None),
            code=None,
            latitude=float(item["lat"]),
            longitude=float(item["lon"]),
            source="nominatim_geo",
            country="中国",
            province=province,
            city=name,
        )

    async def _resolve_with_open_meteo(self, region: str) -> ResolvedLocation | None:
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=20.0) as client:
            for search_name in _open_meteo_search_terms(region):
                params = {"name": search_name, "count": 10, "language": "zh", "format": "json"}
                try:
                    response = await client.get("https://geocoding-api.open-meteo.com/v1/search", params=params)
                    response.raise_for_status()
                    body = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                for item in body.get("results") or []:
                    item["_query"] = search_name
                    results.append(item)
        if not results:
            return None
        item = _best_open_meteo_result(results, region)
        admin = _normalize_china_admin(item.get("admin1"))
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


def _best_nominatim_result(results: list[dict[str, Any]], region: str) -> dict[str, Any] | None:
    candidates = [item for item in results if item.get("lat") is not None and item.get("lon") is not None]
    if not candidates:
        return None

    def score(item: dict[str, Any]) -> float:
        name = str(item.get("name") or "")
        display_name = str(item.get("display_name") or "")
        addresstype = str(item.get("addresstype") or "")
        place_type = str(item.get("type") or "")
        normalized_name = name.rstrip("省市区县")
        normalized_region = region.rstrip("省市区县")
        value = 0.0
        if normalized_name == normalized_region:
            value += 40
        elif name.startswith(region) or region.startswith(normalized_name):
            value += 22
        if place_type == "administrative":
            value += 20
        if addresstype in {"city", "town", "county", "state", "municipality"}:
            value += 18
        if "中国" in display_name:
            value += 5
        value += _safe_float(item.get("importance")) * 10
        place_rank = _safe_float(item.get("place_rank"))
        if place_rank:
            value += max(0, 30 - place_rank)
        return value

    return max(candidates, key=score)


def _open_meteo_search_terms(region: str) -> list[str]:
    terms = [region]
    if _is_bare_chinese_place_name(region):
        terms.extend([f"{region}市", f"{region}县", f"{region}区"])
    return list(dict.fromkeys(terms))


def _is_bare_chinese_place_name(region: str) -> bool:
    if not region or any(char in region for char in "省市区县州盟"):
        return False
    return all("\u4e00" <= char <= "\u9fff" for char in region)


def _best_open_meteo_result(results: list[dict[str, Any]], region: str) -> dict[str, Any]:
    def score(item: dict[str, Any]) -> float:
        name = str(item.get("name") or "")
        query = str(item.get("_query") or "")
        feature_code = str(item.get("feature_code") or "")
        country_code = str(item.get("country_code") or "")
        normalized_name = name.rstrip("省市区县")
        normalized_region = region.rstrip("省市区县")
        value = 0.0
        if country_code == "CN":
            value += 30
        if normalized_name == normalized_region:
            value += 40
        elif name.startswith(region) or region.startswith(normalized_name):
            value += 18
        if query != region and normalized_name == normalized_region:
            value += 22
        if feature_code in {"PPLA", "PPLA2", "PPLA3", "PPLC"}:
            value += 25
        elif feature_code in {"ADM1", "ADM2", "ADM3"}:
            value += 18
        elif feature_code == "PPL":
            value += 6
        value += min(_safe_float(item.get("population")) / 100000, 30)
        return value

    return max(results, key=score)


def _nominatim_province(display_name: Any) -> str | None:
    if not isinstance(display_name, str):
        return None
    parts = [part.strip() for part in display_name.split(",")]
    for part in parts:
        if part.endswith(("省", "自治区", "特别行政区")):
            return part
    for part in parts:
        if part.endswith("市") and part in {"北京市", "天津市", "上海市", "重庆市"}:
            return part
    return None


def _normalize_china_admin(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.endswith(("省", "市", "自治区", "特别行政区")):
        return value
    if value in PROVINCE_SHORT_NAMES:
        return f"{value}省"
    if value in MUNICIPALITY_SHORT_NAMES:
        return f"{value}市"
    return AUTONOMOUS_REGION_SHORT_NAMES.get(value, value)


def _display_name(province: str | None, city: str | None, fallback: str | None) -> str:
    parts = [part for part in [province, city] if part]
    if parts:
        return "".join(dict.fromkeys(parts))
    return fallback or ""


def _to_float(value: Any) -> float:
    return float(value)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
