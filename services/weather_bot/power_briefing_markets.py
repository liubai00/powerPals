"""全国电力气象晨报的分析区与代表点配置。

这里的 33 个区域是“电力气象分析区”，不是对独立交易市场数量的声明。
代表点用于全国扫描；在接入负荷、装机等业务权重前，不构成省级聚合。
"""

from __future__ import annotations

from dataclasses import dataclass


MARKET_CONFIG_VERSION = "cn-31-areas-33-zones-75-points-v2"
ANALYTIC_ROLES = frozenset({"load", "solar", "wind"})
ALLOWED_POINT_ROLES = ANALYTIC_ROLES | frozenset(
    {"industrial", "coastal", "cold", "hydrology"}
)
MAINLAND_PROVINCIAL_AREAS = frozenset(
    {
        "北京",
        "天津",
        "河北",
        "山西",
        "内蒙古",
        "辽宁",
        "吉林",
        "黑龙江",
        "上海",
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
        "广西",
        "海南",
        "重庆",
        "四川",
        "贵州",
        "云南",
        "西藏",
        "陕西",
        "甘肃",
        "青海",
        "宁夏",
        "新疆",
    }
)


@dataclass(frozen=True)
class RepresentativePoint:
    point_id: str
    city: str
    query: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisWindow:
    """One configurable local-time window used by an analysis area."""

    window_id: str
    label: str
    start_hour: int
    end_hour: int


DEFAULT_ANALYSIS_WINDOWS: tuple[AnalysisWindow, ...] = (
    AnalysisWindow("overnight", "凌晨", 0, 6),
    AnalysisWindow("early_peak", "早峰", 6, 10),
    AnalysisWindow("midday_solar", "午间光伏", 10, 16),
    AnalysisWindow("afternoon_transition", "下午过渡", 16, 17),
    AnalysisWindow("evening_peak", "晚峰", 17, 21),
    AnalysisWindow("night", "夜间", 21, 24),
)


@dataclass(frozen=True)
class MarketZone:
    market_id: str
    market_name: str
    provincial_area: str
    provincial_code: str
    points: tuple[RepresentativePoint, ...]
    scope_kind: str = "provincial_sample"
    analysis_windows: tuple[AnalysisWindow, ...] = DEFAULT_ANALYSIS_WINDOWS


def _point(point_id: str, city: str, query: str, *roles: str) -> RepresentativePoint:
    return RepresentativePoint(point_id=point_id, city=city, query=query, roles=tuple(roles))


NATIONAL_MARKETS: tuple[MarketZone, ...] = (
    MarketZone(
        "cn-11-beijing",
        "北京样本区",
        "北京",
        "11",
        (
            _point("beijing-main", "北京", "北京市", "load"),
            _point("beijing-yanqing", "延庆", "北京市延庆区", "solar", "wind", "cold"),
        ),
    ),
    MarketZone(
        "cn-12-tianjin",
        "天津样本区",
        "天津",
        "12",
        (
            _point("tianjin-main", "天津", "天津市", "load"),
            _point("tianjin-binhai", "滨海新区", "天津市滨海新区", "load", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-13-jibei",
        "冀北样本区",
        "河北",
        "13",
        (
            _point("jibei-tangshan", "唐山", "河北省唐山市", "load", "industrial", "coastal"),
            _point("jibei-zhangjiakou", "张家口", "河北省张家口市", "solar", "wind", "cold"),
        ),
        scope_kind="grid_region_sample",
    ),
    MarketZone(
        "cn-13-hebeisouth",
        "河北南网样本区",
        "河北",
        "13",
        (
            _point("hebeisouth-shijiazhuang", "石家庄", "河北省石家庄市", "load"),
            _point("hebeisouth-handan", "邯郸", "河北省邯郸市", "load", "solar", "wind", "industrial"),
        ),
        scope_kind="grid_region_sample",
    ),
    MarketZone(
        "cn-14-shanxi",
        "山西样本区",
        "山西",
        "14",
        (
            _point("shanxi-taiyuan", "太原", "山西省太原市", "load"),
            _point("shanxi-datong", "大同", "山西省大同市", "solar", "wind", "cold"),
        ),
    ),
    MarketZone(
        "cn-15-mengxi",
        "蒙西样本区",
        "内蒙古",
        "15",
        (
            _point("mengxi-hohhot", "呼和浩特", "内蒙古自治区呼和浩特市", "load"),
            _point("mengxi-ordos", "鄂尔多斯", "内蒙古自治区鄂尔多斯市", "load", "solar", "industrial"),
            _point("mengxi-xilinhot", "锡林浩特", "内蒙古自治区锡林浩特市", "wind", "cold"),
        ),
        scope_kind="grid_region_sample",
    ),
    MarketZone(
        "cn-15-mengdong",
        "蒙东样本区",
        "内蒙古",
        "15",
        (
            _point("mengdong-chifeng", "赤峰", "内蒙古自治区赤峰市", "solar", "wind"),
            _point("mengdong-tongliao", "通辽", "内蒙古自治区通辽市", "load", "wind"),
            _point("mengdong-hailar", "海拉尔", "内蒙古自治区呼伦贝尔市海拉尔区", "wind", "cold"),
        ),
        scope_kind="grid_region_sample",
    ),
    MarketZone(
        "cn-21-liaoning",
        "辽宁样本区",
        "辽宁",
        "21",
        (
            _point("liaoning-shenyang", "沈阳", "辽宁省沈阳市", "load"),
            _point("liaoning-dalian", "大连", "辽宁省大连市", "load", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-22-jilin",
        "吉林样本区",
        "吉林",
        "22",
        (
            _point("jilin-changchun", "长春", "吉林省长春市", "load", "cold"),
            _point("jilin-baicheng", "白城", "吉林省白城市", "solar", "wind", "cold"),
        ),
    ),
    MarketZone(
        "cn-23-heilongjiang",
        "黑龙江样本区",
        "黑龙江",
        "23",
        (
            _point("heilongjiang-harbin", "哈尔滨", "黑龙江省哈尔滨市", "load", "cold"),
            _point("heilongjiang-daqing", "大庆", "黑龙江省大庆市", "load", "solar", "wind", "industrial", "cold"),
        ),
    ),
    MarketZone(
        "cn-31-shanghai",
        "上海样本区",
        "上海",
        "31",
        (
            _point("shanghai-main", "上海", "上海市", "load"),
            _point("shanghai-chongming", "崇明", "上海市崇明区", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-32-jiangsu",
        "江苏样本区",
        "江苏",
        "32",
        (
            _point("jiangsu-nanjing", "南京", "江苏省南京市", "load"),
            _point("jiangsu-suzhou", "苏州", "江苏省苏州市", "load", "industrial"),
            _point("jiangsu-yancheng", "盐城", "江苏省盐城市", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-33-zhejiang",
        "浙江样本区",
        "浙江",
        "33",
        (
            _point("zhejiang-hangzhou", "杭州", "浙江省杭州市", "load"),
            _point("zhejiang-ningbo", "宁波", "浙江省宁波市", "load", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-34-anhui",
        "安徽样本区",
        "安徽",
        "34",
        (
            _point("anhui-hefei", "合肥", "安徽省合肥市", "load"),
            _point("anhui-fuyang", "阜阳", "安徽省阜阳市", "load", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-35-fujian",
        "福建样本区",
        "福建",
        "35",
        (
            _point("fujian-fuzhou", "福州", "福建省福州市", "load", "coastal"),
            _point("fujian-xiamen", "厦门", "福建省厦门市", "load", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-36-jiangxi",
        "江西样本区",
        "江西",
        "36",
        (
            _point("jiangxi-nanchang", "南昌", "江西省南昌市", "load"),
            _point("jiangxi-ganzhou", "赣州", "江西省赣州市", "load", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-37-shandong",
        "山东样本区",
        "山东",
        "37",
        (
            _point("shandong-jinan", "济南", "山东省济南市", "load"),
            _point("shandong-qingdao", "青岛", "山东省青岛市", "load", "coastal"),
            _point("shandong-dongying", "东营", "山东省东营市", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-41-henan",
        "河南样本区",
        "河南",
        "41",
        (
            _point("henan-zhengzhou", "郑州", "河南省郑州市", "load"),
            _point("henan-nanyang", "南阳", "河南省南阳市", "load", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-42-hubei",
        "湖北样本区",
        "湖北",
        "42",
        (
            _point("hubei-wuhan", "武汉", "湖北省武汉市", "load"),
            _point("hubei-yichang", "宜昌", "湖北省宜昌市", "load", "solar", "wind", "hydrology"),
        ),
    ),
    MarketZone(
        "cn-43-hunan",
        "湖南样本区",
        "湖南",
        "43",
        (
            _point("hunan-changsha", "长沙", "湖南省长沙市", "load"),
            _point("hunan-chenzhou", "郴州", "湖南省郴州市", "load", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-44-guangdong",
        "广东样本区",
        "广东",
        "44",
        (
            _point("guangdong-guangzhou", "广州", "广东省广州市", "load"),
            _point("guangdong-shenzhen", "深圳", "广东省深圳市", "load", "coastal"),
            _point("guangdong-zhanjiang", "湛江", "广东省湛江市", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-45-guangxi",
        "广西样本区",
        "广西",
        "45",
        (
            _point("guangxi-nanning", "南宁", "广西壮族自治区南宁市", "load"),
            _point("guangxi-beihai", "北海", "广西壮族自治区北海市", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-46-hainan",
        "海南样本区",
        "海南",
        "46",
        (
            _point("hainan-haikou", "海口", "海南省海口市", "load", "coastal"),
            _point("hainan-sanya", "三亚", "海南省三亚市", "load", "solar", "wind", "coastal"),
        ),
    ),
    MarketZone(
        "cn-50-chongqing",
        "重庆样本区",
        "重庆",
        "50",
        (
            _point("chongqing-main", "重庆主城", "重庆市", "load"),
            _point("chongqing-wanzhou", "万州", "重庆市万州区", "load", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-51-sichuan",
        "四川样本区",
        "四川",
        "51",
        (
            _point("sichuan-chengdu", "成都", "四川省成都市", "load"),
            _point("sichuan-xichang", "西昌", "四川省西昌市", "solar", "wind"),
            _point("sichuan-yibin", "宜宾", "四川省宜宾市", "load", "hydrology"),
        ),
    ),
    MarketZone(
        "cn-52-guizhou",
        "贵州样本区",
        "贵州",
        "52",
        (
            _point("guizhou-guiyang", "贵阳", "贵州省贵阳市", "load"),
            _point("guizhou-bijie", "毕节", "贵州省毕节市", "load", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-53-yunnan",
        "云南样本区",
        "云南",
        "53",
        (
            _point("yunnan-kunming", "昆明", "云南省昆明市", "load"),
            _point("yunnan-dali", "大理", "云南省大理市", "solar", "wind"),
            _point("yunnan-jinghong", "景洪", "云南省景洪市", "load", "hydrology"),
        ),
    ),
    MarketZone(
        "cn-54-tibet",
        "西藏样本区",
        "西藏",
        "54",
        (
            _point("tibet-lhasa", "拉萨", "西藏自治区拉萨市", "load", "solar"),
            _point("tibet-nagqu", "那曲", "西藏自治区那曲市", "solar", "wind", "cold"),
        ),
    ),
    MarketZone(
        "cn-61-shaanxi",
        "陕西样本区",
        "陕西",
        "61",
        (
            _point("shaanxi-xian", "西安", "陕西省西安市", "load"),
            _point("shaanxi-yulin", "榆林", "陕西省榆林市", "load", "solar", "wind", "industrial"),
        ),
    ),
    MarketZone(
        "cn-62-gansu",
        "甘肃样本区",
        "甘肃",
        "62",
        (
            _point("gansu-lanzhou", "兰州", "甘肃省兰州市", "load"),
            _point("gansu-jiuquan", "酒泉", "甘肃省酒泉市", "solar", "wind"),
            _point("gansu-qingyang", "庆阳", "甘肃省庆阳市", "load", "industrial"),
        ),
    ),
    MarketZone(
        "cn-63-qinghai",
        "青海样本区",
        "青海",
        "63",
        (
            _point("qinghai-xining", "西宁", "青海省西宁市", "load", "cold"),
            _point("qinghai-golmud", "格尔木", "青海省格尔木市", "solar", "wind", "cold"),
        ),
    ),
    MarketZone(
        "cn-64-ningxia",
        "宁夏样本区",
        "宁夏",
        "64",
        (
            _point("ningxia-yinchuan", "银川", "宁夏回族自治区银川市", "load"),
            _point("ningxia-zhongwei", "中卫", "宁夏回族自治区中卫市", "solar", "wind"),
        ),
    ),
    MarketZone(
        "cn-65-xinjiang",
        "新疆样本区",
        "新疆",
        "65",
        (
            _point("xinjiang-urumqi", "乌鲁木齐", "新疆维吾尔自治区乌鲁木齐市", "load", "cold"),
            _point("xinjiang-hami", "哈密", "新疆维吾尔自治区哈密市", "solar", "wind"),
            _point("xinjiang-kashgar", "喀什", "新疆维吾尔自治区喀什市", "load", "solar"),
        ),
    ),
)


def representative_points(
    markets: tuple[MarketZone, ...] = NATIONAL_MARKETS,
) -> tuple[tuple[MarketZone, RepresentativePoint], ...]:
    return tuple((market, point) for market in markets for point in market.points)


def validate_analysis_windows(windows: tuple[AnalysisWindow, ...]) -> None:
    ordered = sorted(windows, key=lambda item: (item.start_hour, item.end_hour, item.window_id))
    if not ordered or ordered[0].start_hour != 0 or ordered[-1].end_hour != 24:
        raise ValueError("analysis windows must cover 00:00-24:00 without gaps")
    if len({item.window_id for item in ordered}) != len(ordered):
        raise ValueError("analysis window_id must be unique within an analysis area")
    previous_end = 0
    for window in ordered:
        if not (0 <= window.start_hour < window.end_hour <= 24):
            raise ValueError("analysis window hours must satisfy 0 <= start < end <= 24")
        if window.start_hour != previous_end:
            raise ValueError("analysis windows must cover 00:00-24:00 without gaps")
        previous_end = window.end_hour


def validate_market_config(markets: tuple[MarketZone, ...] = NATIONAL_MARKETS) -> None:
    market_ids = [market.market_id for market in markets]
    point_ids = [point.point_id for market in markets for point in market.points]
    areas = {market.provincial_area for market in markets}
    point_count = sum(len(market.points) for market in markets)
    area_counts: dict[str, int] = {}
    for market in markets:
        area_counts[market.provincial_area] = area_counts.get(market.provincial_area, 0) + 1
    if len(market_ids) != len(set(market_ids)):
        raise ValueError("market_id must be unique")
    if len(point_ids) != len(set(point_ids)):
        raise ValueError("point_id must be unique")
    if len(markets) != 33:
        raise ValueError(f"expected 33 analysis zones, got {len(markets)}")
    if point_count != 75:
        raise ValueError(f"expected 75 representative points, got {point_count}")
    if any(len(market.points) < 2 for market in markets):
        raise ValueError("each analysis zone must have at least two representative points")
    if areas != MAINLAND_PROVINCIAL_AREAS:
        missing = sorted(MAINLAND_PROVINCIAL_AREAS - areas)
        extra = sorted(areas - MAINLAND_PROVINCIAL_AREAS)
        raise ValueError(f"provincial coverage mismatch: missing={missing} extra={extra}")
    expected_area_counts = {
        area: (2 if area in {"河北", "内蒙古"} else 1)
        for area in MAINLAND_PROVINCIAL_AREAS
    }
    if area_counts != expected_area_counts:
        raise ValueError(
            f"analysis-zone split mismatch: expected={expected_area_counts} actual={area_counts}"
        )
    required_split_ids = {
        "cn-13-jibei",
        "cn-13-hebeisouth",
        "cn-15-mengdong",
        "cn-15-mengxi",
    }
    if not required_split_ids.issubset(set(market_ids)):
        raise ValueError(
            f"required grid samples missing: {sorted(required_split_ids - set(market_ids))}"
        )
    for market in markets:
        validate_analysis_windows(market.analysis_windows)
        roles = {role for point in market.points for role in point.roles}
        unknown_roles = roles - ALLOWED_POINT_ROLES
        if unknown_roles:
            raise ValueError(
                f"{market.market_id} has unsupported point roles: {sorted(unknown_roles)}"
            )
        missing_roles = ANALYTIC_ROLES - roles
        if missing_roles:
            raise ValueError(
                f"{market.market_id} lacks analytic representative roles: {sorted(missing_roles)}"
            )


validate_market_config()
