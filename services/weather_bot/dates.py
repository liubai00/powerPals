# -*- coding: utf-8 -*-
"""云云统一日期/时窗解析引擎。

parse_date_span(text, today) -> (start_iso, days, raw_days, status)
  status ∈ {ok, truncated, past, beyond}
  - ok:        目标日在 [today, today+16] 预报窗内, days 未被截断
  - truncated: 起始日在窗内, 但请求跨度超过窗口/16 天上限, 已截断(应提示用户)
  - past:      起始日早于今天(历史, 查不了)
  - beyond:    起始日超出 today+16 预报上限(远期, 查不了)

统一有序解析: 日期区间 > 单绝对日期 > 整月 > N到M天 > 相对区间 > 相对单日 > 星期几 > 窗口 > 兜底明天。
一旦命中绝对日期即早返回, 因此不会再被 "N天" 分支误放大 days。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

FORECAST_HORIZON_DAYS = 16  # 今天起最多 today+16 可服务(与 ForecastRequest.days le=16 对齐)

_REL_DAY_OFFSET = [("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1),
                   ("今天", 0), ("今日", 0), ("今晚", 0), ("今夜", 0)]
_CN_DAY_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18,
    "十九": 19, "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24, "二十五": 25,
    "二十六": 26, "二十七": 27, "二十八": 28, "二十九": 29, "三十": 30, "三十一": 31,
}
_CN_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_DAYNUM = r"(?:3[01]|[12]\d|[1-9]|三十一|三十|二十[一二三四五六七八九]|二十|十[一二三四五六七八九]|十|[一二三四五六七八九])"
_MONNUM = r"(?:1[0-2]|[1-9]|十[一二]|十|[一二三四五六七八九])"


def _num(token: str | None) -> int | None:
    if token is None:
        return None
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _CN_DAY_NUM.get(token)


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _date_with_month(month: int | None, day: int | None, today: date, base_year: int | None = None) -> date | None:
    """M月D日: 缺年补当年; 若结果早于今天则顺延到明年。"""
    if day is None:
        return None
    month = month or today.month
    year = base_year or today.year
    cand = _safe_date(year, month, day)
    if cand is None:
        return None
    if base_year is None and cand < today:
        cand = _safe_date(year + 1, month, day) or cand
    return cand


def _date_bare_day(day: int | None, today: date) -> date | None:
    """裸 N日/号: 当月该日; 若已过则取下月同日。"""
    if day is None:
        return None
    cand = _safe_date(today.year, today.month, day)
    if cand is not None and cand >= today:
        return cand
    m = today.month + 1
    y = today.year + (1 if m > 12 else 0)
    m = 1 if m > 12 else m
    return _safe_date(y, m, day)


def _month_last_day(y: int, m: int) -> int:
    nm = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    return (nm - timedelta(days=1)).day


def _next_weekday(today: date, wd: int, force_next: bool = False) -> date:
    delta = (wd - today.weekday()) % 7
    if delta == 0 and force_next:
        delta = 7
    return today + timedelta(days=delta)


def _month_end_date(text: str, today: date) -> date | None:
    """某月最后一天: 显式'最后一天'或'月底/月末'。归属月: N月 / 默认当月。"""
    mm = re.search(rf"({_MONNUM})\s*月", text)
    if mm:
        mo, y = _num(mm.group(1)), today.year
        last = _safe_date(y, mo, _month_last_day(y, mo))
        if last and last < today:
            y += 1
            last = _safe_date(y, mo, _month_last_day(y, mo))
        return last
    return _safe_date(today.year, today.month, _month_last_day(today.year, today.month))


def _single_absolute_date(text: str, today: date) -> date | None:
    # 节日: 国庆→10/1; "十一"仅在带节日语境(十一天气/假期/长假)时算国庆, "未来十一天"仍走天数
    if "国庆" in text or re.search(r"十一\s*(?:黄金周|长假|假期|天气|出行|期间|放假|档)", text):
        return _date_with_month(10, 1, today)
    if "元旦" in text:
        return _date_with_month(1, 1, today)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(rf"({_MONNUM})\s*月\s*({_DAYNUM})\s*[日号]", text)
    if m:
        return _date_with_month(_num(m.group(1)), _num(m.group(2)), today)
    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
    if m:
        return _date_with_month(int(m.group(1)), int(m.group(2)), today)
    if re.search(r"最后一天|最后1天|月底|月末", text):
        return _month_end_date(text, today)
    if re.search(r"月初", text):
        return _date_bare_day(1, today)
    if re.search(r"月中", text):
        return _date_bare_day(15, today)
    m = re.search(rf"(?<![\d月/-])({_DAYNUM})\s*[日号](?!\s*(?:到|至|-|~))", text)
    if m:
        return _date_bare_day(_num(m.group(1)), today)
    return None


def _xun_span(text: str, today: date) -> tuple[date, int] | None:
    """上旬(1-10) / 中旬(11-20) / 下旬(21-月末)。可带月份"7月下旬", 缺月取当月。"""
    m = re.search(rf"(?:({_MONNUM})\s*月)?\s*(上|中|下)\s*旬", text)
    if not m:
        return None
    month = _num(m.group(1)) if m.group(1) else today.month
    if not month:
        return None
    year = today.year
    xun = m.group(2)
    if xun == "上":
        start_day, end_day = 1, 10
    elif xun == "中":
        start_day, end_day = 11, 20
    else:
        start_day, end_day = 21, _month_last_day(year, month)
    start = _safe_date(year, month, start_day)
    end = _safe_date(year, month, end_day)
    if start and end and end >= start:
        return start, (end - start).days + 1
    return None


def _month_span(text: str, today: date) -> tuple[date, int] | None:
    """整月 → 月首到月末; 裸 N月(无'整月') → 该月1号单日。"""
    explicit_full = bool(re.search(r"整月|一整月|全月|整个月", text))
    m = re.search(rf"(?<!\d)({_MONNUM})\s*月(?!\s*{_DAYNUM}\s*[日号])(?!\d)", text)
    if m:
        mo, y = _num(m.group(1)), today.year
        first = _safe_date(y, mo, 1)
        if first and first < today:
            y += 1
            first = _safe_date(y, mo, 1)
        if first:
            return first, (_month_last_day(y, mo) if explicit_full else 1)
    if re.search(r"整月|一整月|全月|整个月|这个月|本月", text):
        y, mo = today.year, today.month
        return _safe_date(y, mo, 1), _month_last_day(y, mo)
    return None


# ---- 窗口(未来N天/一周/N周/下周) ----
_DAY_COUNT_WORDS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16,
}
_DAY_COUNT_TOKEN_RE = r"(十六|十五|十四|十三|十二|十一|十|[一二两三四五六七八九]|\d+)"
_WEEK_COUNT_RE = re.compile(r"(?<![最上过前去后])([一两二三123])\s*个?\s*(?:周|星期|礼拜)")
_WEEK_BARE_RE = re.compile(r"(?:下|这|本)\s*(?:周|星期|礼拜)(?![末一二三四五六日天年])")
_WEEK_COUNT_WORDS = {"一": 1, "1": 1, "两": 2, "二": 2, "2": 2, "三": 3, "3": 3}


def _clamp_daycount(token: str) -> int:
    value = int(token) if token.isdigit() else _DAY_COUNT_WORDS.get(token, 1)
    return max(1, value)


def _window_days_from_text(text: str) -> int | None:
    wm = _WEEK_COUNT_RE.search(text)
    if wm:
        return _WEEK_COUNT_WORDS.get(wm.group(1), 1) * 7
    if _WEEK_BARE_RE.search(text):
        return 7
    pm = re.search(rf"(?:未来|最近|近|接下来|接着|之后|后面|往后|连续|随后)\s*{_DAY_COUNT_TOKEN_RE}\s*(?:天|日)", text)
    if pm:
        return _clamp_daycount(pm.group(1))
    pl = re.search(rf"{_DAY_COUNT_TOKEN_RE}\s*天", text)
    if pl:
        return _clamp_daycount(pl.group(1))
    return None


def _weekday_from_text(text: str, today: date) -> date | None:
    m = re.search(r"(下\s*)?(?:周|星期|礼拜)\s*([一二三四五六日天])", text)
    if not m:
        return None
    wd = _CN_WEEKDAY.get(m.group(2))
    if wd is None:
        return None
    base = _next_weekday(today, wd, force_next=False)
    if m.group(1):  # 下周X
        base = base + timedelta(days=7)
    return base


def _parse_date_and_span(text: str, today: date) -> tuple[date, int]:
    tomorrow = today + timedelta(days=1)

    # 1) 日期区间 M月D日到[M月]D日
    m = re.search(rf"({_MONNUM})\s*月\s*({_DAYNUM})\s*[日号]\s*(?:到|至|-|~|—|－)\s*(?:({_MONNUM})\s*月)?\s*({_DAYNUM})\s*[日号]", text)
    if m:
        start = _date_with_month(_num(m.group(1)), _num(m.group(2)), today)
        end_m = _num(m.group(3)) if m.group(3) else _num(m.group(1))
        if start:
            end = _date_with_month(end_m, _num(m.group(4)), today, base_year=start.year)
            if end and end < start:
                end = _safe_date(start.year + 1, end.month, end.day)
            if end and end >= start:
                return start, (end - start).days + 1

    # 1b) 纯 D日到D日(同月)
    m = re.search(rf"(?<![\d月])({_DAYNUM})\s*[日号]\s*(?:到|至|-|~|—)\s*({_DAYNUM})\s*[日号]", text)
    if m:
        d1, d2 = _num(m.group(1)), _num(m.group(2))
        start = _date_bare_day(d1, today)
        if start and d1 and d2 and d2 >= d1:
            return start, d2 - d1 + 1

    # 1c) 上/中/下旬(先于整月, 否则"7月下旬"里的"7月"会被当整月)
    xun = _xun_span(text, today)
    if xun and xun[0] is not None:
        return xun

    # 2) 单绝对日期(先于"N到M天", 避免 ISO/斜杠里的 '-' 被当区间连接符)
    d = _single_absolute_date(text, today)
    if d is not None:
        return d, 1

    # 2b) 整月/N月
    span = _month_span(text, today)
    if span and span[0] is not None:
        return span

    # 3) 未来N到M天 / N到M天
    m = re.search(rf"(?:未来|最近|接下来)?\s*({_DAYNUM})\s*(?:到|至|-|~)\s*({_DAYNUM})\s*天", text)
    if m:
        n, mm = _num(m.group(1)), _num(m.group(2))
        if n and mm:
            return today, max(n, mm)

    # 4) 相对区间
    if re.search(r"这周末|本周末|这个周末|周末|双休", text):
        return _next_weekday(today, 5), 2
    if re.search(r"这几天|近几天|最近几天|这两天|这三天", text):
        return today, 3
    m = re.search(r"(?:未来|接下来|最近)?\s*(半|一|二|两|三|四|五|六)?\s*个月", text)
    if m:
        if m.group(1) == "半":
            return today, 15
        n = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}.get(m.group(1), 1)
        return today, n * 30

    # 5) 相对单日
    for word, off in _REL_DAY_OFFSET:
        if word in text:
            return today + timedelta(days=off), 1

    # 6) 星期几
    wd = _weekday_from_text(text, today)
    if wd is not None:
        return wd, 1

    # 7) 窗口(未来N天/一周/下周...)
    dspan = _window_days_from_text(text)
    if dspan is not None:
        start = today
        if re.search(r"下\s*(?:周|星期|礼拜)", text):
            start = _next_weekday(today, 0, force_next=True)  # 下周一
        return start, dspan

    # 8) 兜底: 明天单日
    return tomorrow, 1


def parse_date_span(text: str, today: date | None = None) -> tuple[str, int, int, str]:
    today = today or date.today()
    start, raw_days = _parse_date_and_span(text, today)
    raw_days = max(1, raw_days)
    last = today + timedelta(days=FORECAST_HORIZON_DAYS)
    if start < today:
        return start.isoformat(), min(raw_days, 16), raw_days, "past"
    if start > last:
        return start.isoformat(), min(raw_days, 16), raw_days, "beyond"
    max_from_start = (last - start).days + 1
    days = min(raw_days, max_from_start, 16)
    status = "truncated" if days < raw_days else "ok"
    return start.isoformat(), days, raw_days, status


def target_date_from_text(text: str, today: date | None = None) -> str:
    return parse_date_span(text, today)[0]


def days_from_text(text: str, today: date | None = None) -> int:
    return parse_date_span(text, today)[1]
