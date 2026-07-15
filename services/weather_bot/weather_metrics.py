from __future__ import annotations

from collections.abc import Iterable


SUPPORTED_WEATHER_METRIC_ORDER: tuple[str, ...] = ("temperature", "rain", "wind", "cloud")

SUPPORTED_WEATHER_METRICS: dict[str, dict[str, object]] = {
    "temperature": {
        "label": "温度",
        # 体感随温度在卡片展示, 故"体感"归入温度; "更热/热吗/凉快"等口语也触发温度(不用裸"热/冷"避免误匹配)
        "aliases": ("温度", "气温", "最高温", "最低温", "冷热", "热不热", "冷不冷",
                    "更热", "更冷", "凉快", "闷热", "炎热", "多热", "多冷", "热吗", "冷吗", "热一点", "冷一点",
                    "体感", "体感温度"),
    },
    "rain": {
        "label": "降水",
        "aliases": ("降雨", "降水", "下雨", "会不会下雨", "降水概率", "降雨概率", "雨势", "雨天"),
    },
    "wind": {
        "label": "风速",
        # 风向随风速在卡片展示, 故"风向"归入风
        "aliases": ("风速", "风力", "大风", "强风", "风大", "风向"),
    },
    "cloud": {
        "label": "云量",
        "aliases": ("云量", "云层", "多云", "阴天", "晴到多云", "阴晴"),
    },
}

UNSUPPORTED_WEATHER_METRICS: dict[str, dict[str, object]] = {
    "humidity": {"label": "湿度", "aliases": ("湿度", "相对湿度")},
    "pressure": {"label": "气压", "aliases": ("气压", "大气压", "海平面气压")},
    "air_quality": {"label": "空气质量/AQI", "aliases": ("空气质量", "AQI", "aqi", "PM2.5", "pm2.5", "PM10", "臭氧", "O3", "o3")},
    "visibility": {"label": "能见度", "aliases": ("能见度",)},
    # 紫外线(整卡已展示)/体感(→温度)/风向(→风) 已改为受支持, 不再误报"未接入"
    "precipitation_amount": {"label": "降水量/雨量", "aliases": ("雨量", "降雨量", "降水量", "多少毫米", "mm", "毫米")},
    "weather_warning": {"label": "气象预警", "aliases": ("预警", "警报", "气象警报", "气象预警")},
    "solar_radiation": {"label": "日照/辐射", "aliases": ("日照", "辐射", "太阳辐射")},
    "dew_point": {"label": "露点", "aliases": ("露点",)},
    "snow": {"label": "降雪量", "aliases": ("降雪", "雪量", "降雪量")},
}

EXCLUDE_METRIC_PREFIXES = ("不要", "不用", "不看", "去掉", "排除", "别看", "无需", "不用展示", "不要展示")


def normalize_weather_metrics(metrics: Iterable[str] | str | None) -> list[str]:
    if metrics is None:
        return list(SUPPORTED_WEATHER_METRIC_ORDER)
    if isinstance(metrics, str):
        candidates = [item.strip() for item in metrics.split(",")]
    else:
        candidates = [str(item).strip() for item in metrics]
    selected = [key for key in SUPPORTED_WEATHER_METRIC_ORDER if key in candidates]
    return selected or list(SUPPORTED_WEATHER_METRIC_ORDER)


def parse_weather_metrics_query(value: str | None) -> list[str] | None:
    if not value:
        return None
    selected = normalize_weather_metrics(value)
    return None if _is_all_metrics(selected) else selected


def weather_metrics_query_value(metrics: Iterable[str] | str | None) -> str | None:
    if metrics is None:
        return None
    selected = normalize_weather_metrics(metrics)
    if _is_all_metrics(selected):
        return None
    return ",".join(selected)


def weather_metrics_from_text(text: str) -> list[str] | None:
    matched = _matched_supported_metrics(text)
    excluded = _excluded_supported_metrics(text)
    if not matched and not excluded:
        return None

    selected = matched or list(SUPPORTED_WEATHER_METRIC_ORDER)
    selected = [metric for metric in selected if metric not in excluded]
    if not selected:
        return None
    if _is_all_metrics(selected):
        return None
    return selected


def unsupported_weather_metric_labels(text: str) -> list[str]:
    labels = []
    lowered = text.lower()
    for config in UNSUPPORTED_WEATHER_METRICS.values():
        aliases = config["aliases"]
        if any(str(alias).lower() in lowered for alias in aliases):
            label = str(config["label"])
            if label not in labels:
                labels.append(label)
    return labels


def weather_metric_labels(metrics: Iterable[str] | str | None) -> list[str]:
    selected = normalize_weather_metrics(metrics)
    return [str(SUPPORTED_WEATHER_METRICS[key]["label"]) for key in selected]


def weather_metric_phrase(metrics: Iterable[str] | str | None) -> str:
    if metrics is None:
        return ""
    labels = weather_metric_labels(metrics)
    return "、".join(labels)


def has_weather_metric_keyword(text: str) -> bool:
    return bool(_matched_supported_metrics(text) or unsupported_weather_metric_labels(text))


def _matched_supported_metrics(text: str) -> list[str]:
    lowered = text.lower()
    matched = []
    for key in SUPPORTED_WEATHER_METRIC_ORDER:
        aliases = SUPPORTED_WEATHER_METRICS[key]["aliases"]
        if any(str(alias).lower() in lowered for alias in aliases):
            matched.append(key)
    return matched


def _excluded_supported_metrics(text: str) -> list[str]:
    lowered = text.lower()
    excluded = []
    for key in SUPPORTED_WEATHER_METRIC_ORDER:
        aliases = [str(alias).lower() for alias in SUPPORTED_WEATHER_METRICS[key]["aliases"]]
        if any(_has_exclusion(lowered, alias) for alias in aliases):
            excluded.append(key)
    return excluded


def _has_exclusion(text: str, alias: str) -> bool:
    return any(f"{prefix}{alias}" in text or f"{alias}{prefix}" in text for prefix in EXCLUDE_METRIC_PREFIXES)


def _is_all_metrics(metrics: Iterable[str]) -> bool:
    return list(metrics) == list(SUPPORTED_WEATHER_METRIC_ORDER)
