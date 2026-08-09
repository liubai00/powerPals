from __future__ import annotations

import json
import logging
import re
import time

from services.weather_bot import dates as weather_dates
from services.weather_bot import memory as weather_memory
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from services.weather_bot.cards import build_feishu_card, build_text_reply_card, build_weather_comparison_card, is_rich_reply_text
from services.weather_bot.config import Settings
from services.weather_bot.feishu import FeishuBotAccount, FeishuClient, verify_feishu_token
from services.weather_bot.judge import WeatherJudgeRequest, WeatherJudgeResult, score_weather_submission
from services.weather_bot.llm import LlmClient, answer_role_question, answer_weather_knowledge_question, extract_location_with_llm
from services.weather_bot.location import (
    BUILTIN_LOCATIONS,
    FavoriteLocation,
    LocationBook,
    LocationResolver,
    interpret_region_scope,
    location_slug,
    starts_with_province_scope_modifier,
)
from services.weather_bot.models import ForecastRequest, SubmissionRecord, WeatherSubmission
from services.weather_bot.service import ForecastService
from services.weather_bot.search import TavilySearchClient
from services.weather_bot.storage import JsonlRecorder
from services.weather_bot.typhoon import TyphoonClient, mentions_typhoon
from services.weather_bot.task_cards import build_task_card, build_task_text
from services.weather_bot.tasks import WeatherTask, WeatherTaskRequest, WeatherTaskService
from services.weather_bot.workbench import (
    HydrologyRecord,
    NewsItem,
    WeatherBatchRequest,
    collect_forecasts_with_errors,
    hydrology_csv,
    weather_csv,
    weather_report_html,
)
from services.weather_bot.weather_metrics import (
    has_weather_metric_keyword,
    parse_weather_metrics_query,
    unsupported_weather_metric_labels,
    weather_metric_phrase,
    weather_metrics_from_text,
    weather_metrics_query_value,
)


logger = logging.getLogger(__name__)

FEISHU_LEGACY_BOT = "legacy"
FEISHU_WEATHER_BOT = "weather"
FEISHU_TASK_BOT = "task"
WEATHER_FORECAST_BOT_ROLE = "weather_forecast_bot"
WEATHER_TASK_BOT_ROLE = "weather_task_bot"
WEATHER_BOT_ALIASES = ["云云", "AI气象预测小助手", "气象预测小助手", "气象小助手", "全国气象预测机器人"]
TASK_BOT_ALIASES = ["点点", "AI任务小助手", "任务小助手", "气象任务发布机器人"]
WEATHER_TASK_ID_RE = re.compile(r"WEATHER-CN-(.+)-(\d{4})(\d{2})(\d{2})-DAYAHEAD-001")
DEFAULT_REGION = "广东省深圳市"
LOCATION_ALIASES: tuple[tuple[str, str], ...] = (
    ("黑龙江省", "黑龙江省"),
    ("黑龙江", "黑龙江省"),
    ("内蒙古自治区", "内蒙古自治区"),
    ("内蒙古", "内蒙古自治区"),
    ("广西壮族自治区", "广西壮族自治区"),
    ("广西", "广西壮族自治区"),
    ("宁夏回族自治区", "宁夏回族自治区"),
    ("宁夏", "宁夏回族自治区"),
    ("新疆维吾尔自治区", "新疆维吾尔自治区"),
    ("新疆", "新疆维吾尔自治区"),
    ("西藏自治区", "西藏自治区"),
    ("西藏", "西藏自治区"),
    ("香港特别行政区", "香港特别行政区"),
    ("香港", "香港特别行政区"),
    ("澳门特别行政区", "澳门特别行政区"),
    ("澳门", "澳门特别行政区"),
    ("北京市", "北京市"),
    ("北京", "北京市"),
    ("天津市", "天津市"),
    ("天津", "天津市"),
    ("上海市", "上海市"),
    ("上海", "上海市"),
    ("重庆市", "重庆市"),
    ("重庆", "重庆市"),
    ("河北省", "河北省"),
    ("河北", "河北省"),
    ("山西省", "山西省"),
    ("山西", "山西省"),
    ("辽宁省", "辽宁省"),
    ("辽宁", "辽宁省"),
    ("吉林省", "吉林省"),
    ("吉林", "吉林省"),
    ("江苏省", "江苏省"),
    ("江苏", "江苏省"),
    ("浙江省", "浙江省"),
    ("浙江", "浙江省"),
    ("安徽省", "安徽省"),
    ("安徽", "安徽省"),
    ("福建省", "福建省"),
    ("福建", "福建省"),
    ("江西省", "江西省"),
    ("江西", "江西省"),
    ("山东省", "山东省"),
    ("山东", "山东省"),
    ("河南省", "河南省"),
    ("河南", "河南省"),
    ("湖北省", "湖北省"),
    ("湖北", "湖北省"),
    ("湖南省", "湖南省"),
    ("湖南", "湖南省"),
    ("广东省", "广东省"),
    ("广东", "广东省"),
    ("海南省", "海南省"),
    ("海南", "海南省"),
    ("四川省", "四川省"),
    ("四川", "四川省"),
    ("贵州省", "贵州省"),
    ("贵州", "贵州省"),
    ("云南省", "云南省"),
    ("云南", "云南省"),
    ("陕西省", "陕西省"),
    ("陕西", "陕西省"),
    ("甘肃省", "甘肃省"),
    ("甘肃", "甘肃省"),
    ("青海省", "青海省"),
    ("青海", "青海省"),
    ("台湾省", "台湾省"),
    ("台湾", "台湾省"),
    # 城市雅号/别称 → 标准市名(交给 geocoding 解析), 避免全靠 LLM 抽取、失败静默回退默认城市
    ("魔都", "上海市"),
    ("帝都", "北京市"),
    ("羊城", "广州市"),
    ("花城", "广州市"),
    ("鹏城", "深圳市"),
    ("春城", "昆明市"),
    ("山城", "重庆市"),
    ("冰城", "哈尔滨市"),
    ("蓉城", "成都市"),
    ("锦城", "成都市"),
    ("江城", "武汉市"),
    ("星城", "长沙市"),
    ("泉城", "济南市"),
    ("绿城", "郑州市"),
)

# 常用地级市(非省会)裸名 → "省简称+市名"(坐标交由和风 Geocoding 解析, 省前缀消歧、免市/州后缀出错)。
# 让"盘锦未来7天"这类裸地级市名也能被识别为地区: 免@自动回 + 少依赖 LLM 抽取。省会与已收录市(大连/苏州/青岛…)不重复列。
_PREFECTURE_CITIES: dict[str, tuple[str, ...]] = {
    "河北": ("唐山", "保定", "廊坊", "沧州", "秦皇岛", "邯郸", "邢台", "张家口", "承德", "衡水"),
    "山西": ("大同", "临汾", "运城", "长治", "晋中", "晋城", "阳泉", "忻州", "吕梁", "朔州"),
    "辽宁": ("盘锦", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "铁岭", "朝阳", "葫芦岛"),
    "吉林": ("四平", "通化", "白山", "松原", "白城", "辽源"),
    "黑龙江": ("大庆", "齐齐哈尔", "牡丹江", "佳木斯", "鸡西", "双鸭山", "伊春", "七台河", "鹤岗", "黑河", "绥化"),
    "江苏": ("常州", "南通", "徐州", "盐城", "扬州", "泰州", "镇江", "淮安", "连云港", "宿迁"),
    "浙江": ("嘉兴", "湖州", "绍兴", "金华", "台州", "衢州", "丽水", "舟山"),
    "安徽": ("芜湖", "蚌埠", "阜阳", "安庆", "马鞍山", "滁州", "宿州", "六安", "亳州", "宣城", "淮南", "淮北", "铜陵", "黄山", "池州"),
    "福建": ("泉州", "漳州", "莆田", "三明", "南平", "龙岩", "宁德"),
    "江西": ("赣州", "九江", "上饶", "宜春", "吉安", "抚州", "新余", "鹰潭", "萍乡", "景德镇"),
    "山东": ("烟台", "潍坊", "临沂", "淄博", "济宁", "泰安", "聊城", "威海", "枣庄", "德州", "东营", "菏泽", "滨州", "日照"),
    "河南": ("洛阳", "南阳", "新乡", "许昌", "平顶山", "安阳", "焦作", "商丘", "开封", "濮阳", "周口", "信阳", "驻马店", "三门峡", "鹤壁", "漯河"),
    "湖北": ("宜昌", "襄阳", "荆州", "黄冈", "十堰", "孝感", "荆门", "黄石", "咸宁", "随州", "鄂州"),
    "湖南": ("衡阳", "株洲", "湘潭", "岳阳", "常德", "郴州", "益阳", "永州", "怀化", "娄底", "邵阳", "张家界"),
    "广东": ("汕头", "湛江", "江门", "茂名", "肇庆", "梅州", "清远", "潮州", "揭阳", "汕尾", "河源", "阳江", "云浮", "韶关"),
    "广西": ("柳州", "桂林", "北海", "梧州", "玉林", "钦州", "贵港", "防城港", "百色", "河池", "来宾", "贺州", "崇左"),
    "四川": ("绵阳", "德阳", "南充", "宜宾", "泸州", "乐山", "达州", "自贡", "内江", "遂宁", "广元", "资阳", "眉山", "广安", "巴中", "雅安", "攀枝花"),
    "云南": ("曲靖", "玉溪", "大理", "红河", "文山", "楚雄", "普洱", "保山", "昭通", "丽江", "临沧"),
    "贵州": ("遵义", "六盘水", "安顺", "毕节", "铜仁"),
    "陕西": ("宝鸡", "咸阳", "渭南", "榆林", "汉中", "延安", "安康", "商洛", "铜川"),
    "甘肃": ("天水", "酒泉", "张掖", "武威", "庆阳", "平凉", "白银", "定西", "陇南", "嘉峪关"),
    "内蒙古": ("包头", "鄂尔多斯", "赤峰", "通辽", "呼伦贝尔", "乌海", "巴彦淖尔", "乌兰察布"),
    "新疆": ("喀什", "阿克苏", "昌吉", "哈密", "克拉玛依", "吐鲁番"),
    "海南": ("三亚", "儋州"),
    "宁夏": ("石嘴山", "吴忠", "中卫", "固原"),
}
LOCATION_ALIASES = LOCATION_ALIASES + tuple(
    (_city, f"{_prov}{_city}") for _prov, _cities in _PREFECTURE_CITIES.items() for _city in _cities
)
LOCATION_ALIAS_MAP = dict(LOCATION_ALIASES)
DAY_COUNT_WORDS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
}
DAY_COUNT_TOKEN_RE = r"(十六|十五|十四|十三|十二|十一|十|[一二两三四五六七八九]|\d+)"
REGION_WITH_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fff]{2,12}?(?:特别行政区|自治区|自治州|地区|省|市|盟|州|县|区))(?=(?:最近|未来|接下来|近期|这几天|今天|明天|后天|天气|气象|预测|预报|降雨|降水|信息|任务|的|[一二两三四五六七八九十\d]+[天日]|\s|$))"
)
REGION_QUERY_PREFIXES = (
    "帮我查询一下",
    "帮我查一下",
    "帮我查询",
    "帮我查下",
    "帮我看下",
    "帮我看看",
    "请帮我查",
    "请查询",
    "查一下",
    "查询下",
    "查下",
    "查询",
    "查看",
    "查",
    "发布",
    "领取",
    "我想看",
    "我要看",
    "请",
)
TASK_BARE_REGION_RE = re.compile(
    r"(?:发布|创建|发起|生成|新增)(?:一下|下|一个|一条|一份)?\s*"
    r"([\u4e00-\u9fff]{2,18}?)(?=(?:最近|未来|接下来|今天|明天|后天|"
    r"[一二两三四五六七八九十\d]+[天日]|的?气象任务|气象任务|气象|天气|任务|$))"
)
TASK_BARE_REGION_BLOCKLIST = {
    "今天",
    "今日",
    "明天",
    "明日",
    "后天",
    "最近",
    "未来",
    "接下来",
    "气象",
    "天气",
    "任务",
}
PLACE_SUFFIX_CONTINUATION_CHARS = "省市区县州盟"
FORECAST_REPORT_CACHE_TTL_SECONDS = 3600


def _weather_loading_shell() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>云云 · 正在生成预测…</title>
<style>
  html, body { margin: 0; height: 100%; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: linear-gradient(135deg, #38bdf8 0%, #0ea5e9 45%, #0f766e 100%); color: #fff; }
  #load { position: fixed; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 18px; text-align: center; padding: 24px; }
  .spin { width: 46px; height: 46px; border: 4px solid rgba(255,255,255,.35); border-top-color: #fff; border-radius: 50%; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .t { font-size: 18px; font-weight: 600; }
  .s { font-size: 13px; color: rgba(255,255,255,.85); max-width: 300px; line-height: 1.7; }
  iframe { position: fixed; inset: 0; width: 100%; height: 100%; border: 0; opacity: 0; transition: opacity .35s ease; background: #eef3f9; }
</style>
</head>
<body>
<div id="load">
  <div class="spin"></div>
  <div class="t">⛅ 云云正在生成预测…</div>
  <div class="s">正在汇总多家气象数据源，首次生成约需数秒，请稍候。生成后自动展示，一小时内再次打开即秒开。</div>
</div>
<iframe id="rpt" title="气象预测报告"></iframe>
<script>
  (function () {
    var u = new URL(window.location.href);
    u.searchParams.set("_fragment", "1");
    var f = document.getElementById("rpt");
    f.addEventListener("load", function () {
      var l = document.getElementById("load");
      if (l) { l.style.display = "none"; }
      f.style.opacity = "1";
    });
    f.src = u.toString();
  })();
</script>
</body>
</html>"""
COMPARISON_QUERY_KEYWORDS = ("对比", "比较", "相比", "差异", "哪个更", "哪边更", "谁更", "谁热", "谁冷", "更热", "更冷", "哪个热", "哪个冷", "哪个凉快")
MAX_COMPARISON_REGIONS = 4
WEATHER_KNOWLEDGE_KEYWORDS = (
    "解释",
    "说明",
    "介绍",
    "讲讲",
    "什么是",
    "什么意思",
    "啥意思",
    "为什么",
    "怎么看",
    "含义",
    "来源",
    "数据源",
    "数据来源",
    "哪来",
    "准不准",
    "出力",
    "负荷",
    "现货",
    "电价",
    "风电",
    "光伏",
    "新能源",
    "电网",
    "检修",
    "准确",
    "靠谱",
    "怎么算",
    "怎么预测",
    "原理",
    "机制",
    "更新时间",
    "更新频率",
    "不确定性",
    "置信度",
    "误差",
    "准确性",
    "免责声明",
    "适用边界",
)


# T1/L2 对话记忆: SQLite 落盘(data/memory.db), TTL 7 天, 重启不丢; 失败自动降级为无记忆
def _card_memory_summary(subs) -> str | None:
    """把刚发出的天气卡片压成一行文本存进对话记忆, 供 LLM 回答后续追问时引用真实数据。"""
    if not subs:
        return None
    first = subs[0]
    try:
        s = first.aggregated_forecast.summary
        parts = [
            "[天气卡片]%s %s起%d天" % (first.region, first.target_date, max(1, len(subs))),
            str(getattr(s, "main_weather", "") or ""),
            "%s~%s℃" % (s.min_temperature, s.max_temperature),
            "降水%s%%" % s.rain_probability,
            "风%sm/s" % s.wind_speed,
        ]
        if len(subs) > 1:
            last = subs[-1].aggregated_forecast.summary
            parts.append("末日%s降水%s%%" % (getattr(last, "main_weather", "") or "", last.rain_probability))
        return " ".join(str(p) for p in parts if p)
    except Exception:  # noqa: BLE001
        return None


def _conversation_key(
    bot_role: str,
    chat_id: str | None,
    thread_id: str | None,
    sender_id: str,
    chat_type: str = "",
) -> str:
    return f"{bot_role}|{chat_type or 'unknown'}|{chat_id or ''}|{thread_id or 'main'}|{sender_id}"


def _record_conversation_turn(
    bot_role: str,
    chat_id: str | None,
    thread_id: str | None,
    sender_id: str,
    user_text: str,
    bot_text: str,
    chat_type: str = "",
) -> None:
    if not chat_id or not (user_text or bot_text):
        return
    key = _conversation_key(bot_role, chat_id, thread_id, sender_id, chat_type)
    try:
        if user_text and user_text.strip():
            weather_memory.record_turn(key, "user", user_text.strip())
        if bot_text and bot_text.strip():
            weather_memory.record_turn(key, "assistant", bot_text.strip())
    except Exception:  # noqa: BLE001 - 记忆失败不影响主流程
        pass


def _recent_conversation_turns(
    bot_role: str,
    chat_id: str | None,
    thread_id: str | None,
    sender_id: str,
    chat_type: str = "",
) -> list[dict[str, str]]:
    if not chat_id:
        return []
    try:
        # bot / chat type / chat / thread / sender 五维严格隔离。群内协作不应以串用户上下文为代价。
        key = _conversation_key(bot_role, chat_id, thread_id, sender_id, chat_type)
        return weather_memory.recent_turns(key)
    except Exception:  # noqa: BLE001
        return []


def create_app(
    forecast_service: ForecastService | Any | None = None,
    feishu_verification_token: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings()
    service = forecast_service or ForecastService(settings=settings)
    progress_messages_enabled = settings.feishu_progress_message_enabled and forecast_service is None
    llm_client = LlmClient.from_settings(settings)
    search_client = TavilySearchClient(settings.tavily_api_key)
    typhoon_client = TyphoonClient(settings.qweather_api_key, settings.qweather_api_host)
    task_service = WeatherTaskService()
    location_resolver = LocationResolver(settings)
    location_book = LocationBook(settings)
    recorder = JsonlRecorder(settings.local_jsonl_path)
    task_recorder = JsonlRecorder(settings.local_task_jsonl_path)
    news_recorder = JsonlRecorder(settings.local_news_jsonl_path)
    hydrology_recorder = JsonlRecorder(settings.local_hydrology_jsonl_path)
    legacy_account = _legacy_feishu_account(settings, feishu_verification_token)
    weather_account = _role_feishu_account(settings, FEISHU_WEATHER_BOT, legacy_account)
    task_account = _role_feishu_account(settings, FEISHU_TASK_BOT, legacy_account)
    legacy_feishu = FeishuClient(settings, legacy_account)
    weather_feishu = FeishuClient(settings, weather_account)
    task_feishu = FeishuClient(settings, task_account)
    task_index: dict[str, WeatherTask] = {}
    fallback_processed_message_ids: dict[str, float] = {}
    forecast_report_cache: dict[str, tuple[float, list[WeatherSubmission], list[dict[str, str]]]] = {}
    pending_region_clarifications: dict[str, dict[str, Any]] = {}

    app = FastAPI(title="PowerPals Weather Data Workbench", version="0.7.0")

    def _cache_task(task: WeatherTask) -> WeatherTask:
        task_index[task.task_id] = task
        return task

    def _load_task_from_local_log(task_id: str) -> WeatherTask | None:
        for payload in reversed(task_recorder.read_json_objects()):
            if payload.get("task_id") != task_id:
                continue
            try:
                return _cache_task(WeatherTask.model_validate(payload))
            except ValueError:
                continue
        return None

    def _task_from_task_id(task_id: str) -> WeatherTask | None:
        cached = task_index.get(task_id)
        if cached:
            return cached
        stored = _load_task_from_local_log(task_id)
        if stored:
            return stored

        match = WEATHER_TASK_ID_RE.fullmatch(task_id.strip())
        if not match:
            return None
        location_token = match.group(1)
        target_date = f"{match.group(2)}-{match.group(3)}-{match.group(4)}"
        location = next((item for item in BUILTIN_LOCATIONS.values() if location_slug(item) == location_token), None)
        if not location:
            return None
        return _cache_task(task_service.create_dayahead_task(target_date, location))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _collect_cached_forecasts(request: ForecastRequest) -> tuple[list[WeatherSubmission], list[dict[str, str]]]:
        now = time.monotonic()
        expired = [
            key
            for key, (created_at, _submissions, _errors) in forecast_report_cache.items()
            if now - created_at > FORECAST_REPORT_CACHE_TTL_SECONDS
        ]
        for key in expired:
            forecast_report_cache.pop(key, None)

        key = _forecast_report_cache_key(request)
        cached = forecast_report_cache.get(key)
        if cached:
            return cached[1], cached[2]

        submissions, errors = await collect_forecasts_with_errors(service, request)
        _store_cached_forecasts(request, submissions, errors)
        return submissions, errors

    async def _collect_comparison_forecasts(
        regions: list[str],
        target_date: str,
        days: int,
    ) -> tuple[list[WeatherSubmission], list[dict[str, str]], list[str]]:
        submissions: list[WeatherSubmission] = []
        errors: list[dict[str, str]] = []
        resolved_regions = []
        for region in regions[:MAX_COMPARISON_REGIONS]:
            request = _apply_favorite_alias(
                ForecastRequest(region=region, target_date=target_date, days=days, granularity="1h"),
                location_book,
            )
            collected, region_errors = await _collect_cached_forecasts(request)
            submissions.extend(collected)
            resolved_regions.append(collected[0].region if collected else request.region)
            errors.extend([{"region": request.region, **item} for item in region_errors])
        return submissions, errors, list(dict.fromkeys(resolved_regions))

    def _store_cached_forecasts(
        request: ForecastRequest,
        submissions: list[WeatherSubmission],
        errors: list[dict[str, str]] | None = None,
    ) -> None:
        forecast_report_cache[_forecast_report_cache_key(request)] = (time.monotonic(), submissions, errors or [])

    def _remember_pending_region(event: dict[str, Any], allowed_bot: str, command_type: str, text: str) -> None:
        chat_id = _event_chat_id(event)
        if not chat_id:
            return
        pending_region_clarifications[
            _pending_region_key(
                allowed_bot,
                chat_id,
                _event_sender_id(event),
                _event_thread_id(event) or "",
                _event_chat_type(event),
            )
        ] = {
            "command_type": command_type,
            "target_date": _target_date_from_text(text),
            "days": _days_from_text(text),
            "metrics": weather_metrics_from_text(text),
            "created_at": time.monotonic(),
        }

    def _take_pending_region(event: dict[str, Any], allowed_bot: str) -> dict[str, Any] | None:
        chat_id = _event_chat_id(event)
        if not chat_id:
            return None
        key = _pending_region_key(
            allowed_bot,
            chat_id,
            _event_sender_id(event),
            _event_thread_id(event) or "",
            _event_chat_type(event),
        )
        pending = pending_region_clarifications.get(key)
        if not pending:
            return None
        if time.monotonic() - float(pending.get("created_at", 0)) > 600:
            pending_region_clarifications.pop(key, None)
            return None
        return pending

    def _clear_pending_region(event: dict[str, Any], allowed_bot: str) -> None:
        chat_id = _event_chat_id(event)
        if chat_id:
            pending_region_clarifications.pop(
                _pending_region_key(
                    allowed_bot,
                    chat_id,
                    _event_sender_id(event),
                    _event_thread_id(event) or "",
                    _event_chat_type(event),
                ),
                None,
            )

    @app.post("/api/weather/forecast", response_model=WeatherSubmission)
    async def forecast(request: ForecastRequest) -> WeatherSubmission:
        request = _apply_favorite_alias(request, location_book)
        return await service.forecast(request)

    @app.post("/api/weather/forecast/range")
    async def forecast_range(request: ForecastRequest) -> dict[str, Any]:
        request = _apply_favorite_alias(request, location_book)
        collected, errors = await collect_forecasts_with_errors(service, request)
        submissions = [submission.model_dump(mode="json") for submission in collected]
        return {
            "status": "partial" if errors else "ok",
            "region": submissions[0]["region"] if submissions else request.region,
            "start_date": request.target_date,
            "days": request.days,
            "submissions": submissions,
            "errors": errors,
        }

    @app.post("/api/weather/batch")
    async def forecast_batch(request: WeatherBatchRequest) -> dict[str, Any]:
        submissions = []
        for item in request.requests:
            current = _apply_favorite_alias(item, location_book)
            submissions.append((await service.forecast(current)).model_dump(mode="json"))
        return {"status": "ok", "count": len(submissions), "submissions": submissions}

    @app.post("/api/weather/export")
    async def export_weather(request: ForecastRequest) -> Response:
        request = _apply_favorite_alias(request, location_book)
        submissions, _errors = await _collect_cached_forecasts(request)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        csv_text = weather_csv(submissions)
        filename = f"powerpals-weather-{request.target_date}-{request.days}d.csv"
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/weather/export")
    async def export_weather_get(
        region: str,
        target_date: str,
        days: int = 1,
        latitude: float | None = None,
        longitude: float | None = None,
        location_code: str | None = None,
        location_source: str | None = None,
    ) -> Response:
        return await export_weather(
            ForecastRequest(
                region=region,
                target_date=target_date,
                days=days,
                latitude=latitude,
                longitude=longitude,
                location_code=location_code,
                location_source=location_source,
            )
        )

    @app.post("/api/weather/export/json")
    async def export_weather_json(request: ForecastRequest) -> Response:
        request = _apply_favorite_alias(request, location_book)
        submissions, errors = await _collect_cached_forecasts(request)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        payload = {
            "status": "partial" if errors else "ok",
            "region": submissions[0].region if submissions else request.region,
            "start_date": request.target_date,
            "days": request.days,
            "submissions": [submission.model_dump(mode="json") for submission in submissions],
            "errors": errors,
        }
        filename = f"powerpals-weather-{request.target_date}-{request.days}d.json"
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/weather/export/json")
    async def export_weather_json_get(
        region: str,
        target_date: str,
        days: int = 1,
        latitude: float | None = None,
        longitude: float | None = None,
        location_code: str | None = None,
        location_source: str | None = None,
    ) -> Response:
        return await export_weather_json(
            ForecastRequest(
                region=region,
                target_date=target_date,
                days=days,
                latitude=latitude,
                longitude=longitude,
                location_code=location_code,
                location_source=location_source,
            )
        )

    @app.get("/api/weather/compare/export")
    async def export_weather_compare_get(
        regions: str,
        target_date: str,
        days: int = 1,
    ) -> Response:
        region_list = _comparison_regions_query_to_list(regions)
        if not region_list:
            raise HTTPException(status_code=400, detail="At least one region is required")
        submissions, _errors, _resolved_regions = await _collect_comparison_forecasts(region_list, target_date, days)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        csv_text = weather_csv(submissions)
        filename = f"powerpals-weather-comparison-{target_date}-{days}d.csv"
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/weather/compare/export/json")
    async def export_weather_compare_json_get(
        regions: str,
        target_date: str,
        days: int = 1,
    ) -> Response:
        region_list = _comparison_regions_query_to_list(regions)
        if not region_list:
            raise HTTPException(status_code=400, detail="At least one region is required")
        submissions, errors, resolved_regions = await _collect_comparison_forecasts(region_list, target_date, days)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        payload = {
            "status": "partial" if errors else "ok",
            "regions": resolved_regions,
            "start_date": target_date,
            "days": days,
            "submissions": [submission.model_dump(mode="json") for submission in submissions],
            "errors": errors,
        }
        filename = f"powerpals-weather-comparison-{target_date}-{days}d.json"
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/reports/weather/compare", response_class=HTMLResponse)
    async def weather_compare_report(
        regions: str,
        target_date: str,
        days: int = 1,
        metrics: str | None = None,
        autodownload: str | None = None,
    ) -> HTMLResponse:
        region_list = _comparison_regions_query_to_list(regions)
        if not region_list:
            raise HTTPException(status_code=400, detail="At least one region is required")
        submissions, errors, _resolved_regions = await _collect_comparison_forecasts(region_list, target_date, days)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        report_metrics = parse_weather_metrics_query(metrics)
        download_query: dict[str, Any] = {
            "regions": _comparison_regions_query_value(region_list),
            "target_date": target_date,
            "days": days,
        }
        if metrics:
            download_query["metrics"] = metrics
        html = weather_report_html(
            submissions,
            download_query,
            errors,
            report_metrics,
            autodownload,
            title="多地区气象对比报告",
            download_path="/api/weather/compare/export",
            json_path="/api/weather/compare/export/json",
        )
        return HTMLResponse(content=html)

    @app.get("/reports/weather", response_class=HTMLResponse)
    async def weather_report(
        region: str,
        target_date: str,
        days: int = 1,
        metrics: str | None = None,
        autodownload: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        location_code: str | None = None,
        location_source: str | None = None,
        _fragment: int = 0,
    ) -> HTMLResponse:
        request = _apply_favorite_alias(
            ForecastRequest(
                region=region,
                target_date=target_date,
                days=days,
                latitude=latitude,
                longitude=longitude,
                location_code=location_code,
                location_source=location_source,
            ),
            location_book,
        )
        if not _fragment:
            _cached_entry = forecast_report_cache.get(_forecast_report_cache_key(request))
            if _cached_entry is None or time.monotonic() - _cached_entry[0] > FORECAST_REPORT_CACHE_TTL_SECONDS:
                return HTMLResponse(content=_weather_loading_shell())
        submissions, errors = await _collect_cached_forecasts(request)
        if not submissions:
            raise HTTPException(status_code=502, detail="No usable provider forecasts")
        report_metrics = parse_weather_metrics_query(metrics)
        download_query = _weather_url_query(request)
        if metrics:
            download_query["metrics"] = metrics
        html = weather_report_html(
            submissions,
            download_query,
            errors,
            report_metrics,
            autodownload,
        )
        return HTMLResponse(content=html)

    @app.post("/api/weather/submission")
    async def submission(submission: WeatherSubmission) -> dict[str, str]:
        recorder.append(SubmissionRecord(submission=submission))
        await weather_feishu.write_bitable_record(submission)
        return {"status": "accepted", "task_id": submission.task_id}

    @app.get("/api/locations")
    async def list_locations() -> dict[str, Any]:
        locations = [item.model_dump(mode="json") for item in location_book.list()]
        return {"status": "ok", "count": len(locations), "locations": locations}

    @app.post("/api/locations")
    async def create_location(location: FavoriteLocation) -> dict[str, Any]:
        saved = location_book.upsert(location)
        return {"status": "saved", "location": saved.model_dump(mode="json")}

    @app.delete("/api/locations/{alias}")
    async def delete_location(alias: str) -> dict[str, Any]:
        deleted = location_book.delete(alias)
        return {"status": "deleted" if deleted else "not_found", "alias": alias}

    @app.post("/api/news/items")
    async def create_news_item(item: NewsItem) -> dict[str, Any]:
        news_recorder.append(item)
        return {"status": "accepted", "item": item.model_dump(mode="json")}

    @app.get("/api/news/digest")
    async def news_digest() -> dict[str, Any]:
        items = news_recorder.read_json_objects()
        return {"status": "ok", "count": len(items), "items": list(reversed(items))}

    @app.post("/api/hydrology/records")
    async def create_hydrology_record(record: HydrologyRecord) -> dict[str, Any]:
        hydrology_recorder.append(record)
        return {"status": "accepted", "record": record.model_dump(mode="json")}

    @app.get("/api/hydrology/records")
    async def list_hydrology_records() -> dict[str, Any]:
        records = hydrology_recorder.read_json_objects()
        return {"status": "ok", "count": len(records), "records": list(reversed(records))}

    @app.get("/api/hydrology/export")
    async def export_hydrology_records() -> Response:
        return Response(
            content="\ufeff" + hydrology_csv(hydrology_recorder.read_json_objects()),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="powerpals-hydrology.csv"'},
        )

    @app.get("/api/data/export/catalog")
    async def export_catalog() -> dict[str, Any]:
        return {
            "status": "ok",
            "exports": [
                {"name": "weather_csv", "path": "/api/weather/export", "format": "csv"},
                {"name": "hydrology_csv", "path": "/api/hydrology/export", "format": "csv"},
                {"name": "weather_report", "path": "/reports/weather", "format": "html"},
            ],
        }

    @app.post("/api/judge/weather/score", response_model=WeatherJudgeResult)
    async def judge_weather_score(request: WeatherJudgeRequest) -> WeatherJudgeResult:
        return score_weather_submission(request)

    @app.post("/api/weather/publish")
    async def publish(request: ForecastRequest | None = None) -> dict[str, Any]:
        request = request or _tomorrow_request()
        result = await service.forecast(request)
        card = build_feishu_card(result, show_task_id=True)
        card_message_id = None
        if weather_account.default_chat_id:
            card_message_id = await weather_feishu.send_interactive_card(weather_account.default_chat_id, card)
        recorder.append(SubmissionRecord(submission=result, card_message_id=card_message_id))
        await weather_feishu.write_bitable_record(result, card_message_id)
        return {"status": "published", "submission": result.model_dump(mode="json"), "card": card}

    @app.post("/api/tasks/weather/create")
    async def create_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = _cache_task(task_service.create_dayahead_task(request.target_date, location, request.days))
        return task.model_dump(mode="json")

    @app.post("/api/tasks/weather/publish")
    async def publish_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = task_service.publish(task_service.create_dayahead_task(request.target_date, location, request.days))
        card = build_task_card(task)
        text = build_task_text(task)
        card_message_id = None
        if task_account.default_chat_id:
            card_message_id = await task_feishu.send_interactive_card(task_account.default_chat_id, card)
        task = _cache_task(task.model_copy(update={"task_card_message_id": card_message_id}))
        task_recorder.append(task)
        await task_feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json"), "card": card, "text": text}

    @app.post("/api/tasks/weather/remind")
    async def remind_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = task_service.remind(task_service.publish(task_service.create_dayahead_task(request.target_date, location, request.days)))
        card = build_task_card(task)
        if task_account.default_chat_id:
            card_message_id = await task_feishu.send_interactive_card(task_account.default_chat_id, card)
            task = task.model_copy(update={"task_card_message_id": card_message_id})
        task = _cache_task(task)
        task_recorder.append(task)
        await task_feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json"), "card": card, "text": build_task_text(task)}

    @app.post("/api/tasks/weather/close")
    async def close_weather_task(request: WeatherTaskRequest) -> dict[str, Any]:
        location = await _resolve_task_location(location_resolver, request)
        task = _cache_task(task_service.close(task_service.publish(task_service.create_dayahead_task(request.target_date, location, request.days))))
        task_recorder.append(task)
        await task_feishu.write_task_bitable_record(task)
        return {"task": task.model_dump(mode="json")}

    @app.get("/api/tasks/weather/{task_id}")
    async def get_weather_task(task_id: str) -> dict[str, Any]:
        task = _task_from_task_id(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Unknown weather task")
        return task.model_dump(mode="json")

    @app.get("/api/tasks/weather/{task_id}/submissions")
    async def get_weather_task_submissions(task_id: str) -> dict[str, Any]:
        records = []
        for payload in recorder.read_json_objects():
            try:
                record = SubmissionRecord.model_validate(payload)
            except ValueError:
                continue
            if record.submission.task_id != task_id:
                continue
            records.append(
                {
                    "submission": record.submission.model_dump(mode="json"),
                    "card_message_id": record.card_message_id,
                    "status": record.status,
                    "notes": record.notes,
                }
            )
        return {"status": "ok", "task_id": task_id, "count": len(records), "submissions": records}

    async def _handle_weather_comparison_command(
        text: str,
        regions: list[str],
        display_metrics: list[str] | None,
        unsupported_metrics: list[str],
    ) -> dict[str, Any]:
        target_date = _target_date_from_text(text)
        days = _days_from_text(text)
        requested_regions = regions[:MAX_COMPARISON_REGIONS]
        submissions, errors, resolved_regions = await _collect_comparison_forecasts(requested_regions, target_date, days)
        url_regions = resolved_regions or requested_regions
        report_url = _public_weather_comparison_report_url(settings, url_regions, target_date, days, display_metrics)
        download_url = _public_weather_comparison_download_url(settings, url_regions, target_date, days)
        json_url = _public_weather_comparison_json_url(settings, url_regions, target_date, days)
        card = (
            build_weather_comparison_card(
                submissions,
                metrics=display_metrics,
                report_url=report_url,
                download_url=download_url,
                json_url=json_url,
            )
            if submissions
            else None
        )
        response = {
            "status": "partial" if errors else "handled",
            "bot_role": WEATHER_FORECAST_BOT_ROLE,
            "mode": "weather_comparison",
            "regions": resolved_regions,
            "days": days,
            "report_url": report_url,
            "download_url": download_url,
            "json_url": json_url,
            "card": card,
            "submissions": [submission.model_dump(mode="json") for submission in submissions],
            "errors": errors,
        }
        if display_metrics:
            response["metrics"] = display_metrics
        if unsupported_metrics:
            response["unsupported_metrics"] = unsupported_metrics
        if not submissions:
            response["text"] = "这次对比查询没有拿到可用气象数据，请换一个地区或稍后重试。"
        return response

    async def _handle_weather_knowledge_command(text: str) -> dict[str, Any]:
        # 用户提到具体台风时, 拉和风实时路径+预报做权威 grounding(以最新数据为准, 不用训练记忆里的旧台风)
        live_context = None
        if typhoon_client.enabled:
            try:
                live_context = await typhoon_client.brief_for_text(text)
            except Exception:  # noqa: BLE001 - 台风数据失败不影响回答
                live_context = None
        # 有权威实时台风数据时以它为准, 跳过较慢且噪声大的通用搜索; 否则退回联网搜索
        search_results = []
        if not live_context and search_client.enabled:
            search_results = await search_client.search(text)
        compact_results = [
            {"title": item.title, "url": item.url, "content": item.content[:500]}
            for item in search_results
        ]
        return {
            "status": "handled",
            "bot_role": WEATHER_FORECAST_BOT_ROLE,
            "mode": "knowledge_answer",
            "search_result_count": len(search_results),
            "typhoon_grounded": bool(live_context),
            "text": await answer_weather_knowledge_question(
                llm_client,
                user_text=text,
                fallback=_weather_knowledge_fallback(text),
                search_results=compact_results,
                live_context=live_context,
            ),
        }

    async def _handle_power_briefing_command(
        text: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        from services.weather_bot.briefing_cache import BriefingCache
        from services.weather_bot.power_briefing import (
            SHANGHAI_TZ,
            get_or_generate_briefing,
        )

        cache = BriefingCache(
            settings.power_briefing_cache_db,
            ttl_seconds=settings.power_briefing_cache_ttl_seconds,
        )
        if _is_power_briefing_expand_command(text):
            state = _load_conversation_state(event, WEATHER_FORECAST_BOT_ROLE) or {}
            if not state.get("last_power_briefing_cache_key"):
                state = (
                    _load_shared_briefing_thread_state(
                        event,
                        WEATHER_FORECAST_BOT_ROLE,
                    )
                    or state
                )
            cache_key = state.get("last_power_briefing_cache_key")
            snapshot = cache.load_fresh(str(cache_key)) if cache_key else None
            if snapshot is None:
                return {
                    "status": "needs_briefing_context",
                    "bot_role": WEATHER_FORECAST_BOT_ROLE,
                    "mode": "power_briefing_expand",
                    "text": "当前会话没有可展开的有效晨报快照。请先发送“生成今天的电力气象决策晨报 2.0”。",
                }
            return {
                "status": "handled",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "mode": "power_briefing_expand",
                "coverage": snapshot["coverage"],
                "cache_hit": True,
                "briefing_cache_key": snapshot["cache_key"],
                "generated_at": snapshot["generated_at"],
                "expires_at": snapshot["expires_at"],
                "card": snapshot["detail_card"],
            }

        start_date = datetime.now(SHANGHAI_TZ).date().isoformat()
        tomorrow_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
        try:
            snapshot, cache_hit = await get_or_generate_briefing(
                service,
                typhoon_client,
                start_date,
                cache=cache,
            )
        except Exception:  # noqa: BLE001 - 手动晨报失败返回可重试提示
            logger.exception("manual_power_briefing_failed")
            return {
                "status": "briefing_unavailable",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "mode": "power_briefing",
                "text": "本次没有取得足够的全国代表点气象数据，晨报未生成。请稍后重试。",
            }
        return {
            "status": "handled",
            "bot_role": WEATHER_FORECAST_BOT_ROLE,
            "mode": "power_briefing",
            "coverage": snapshot["coverage"],
            "statistics": snapshot["statistics"],
            "cache_hit": cache_hit,
            "briefing_cache_key": snapshot["cache_key"],
            "generated_at": snapshot["generated_at"],
            "expires_at": snapshot["expires_at"],
            "date_range": [start_date, tomorrow_date],
            "card": snapshot["summary_card"],
        }

    async def _handle_weather_command(text: str) -> dict[str, Any]:
        if _is_past_weather_query(text) and not _is_weather_knowledge_question(text):
            return {
                "status": "handled",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "text": _past_weather_text(),
            }
        # 台风问句直接走知识处理器: 拉和风实时路径做权威 grounding, 不当成城市卡片查询
        if typhoon_client.enabled and mentions_typhoon(text):
            return await _handle_weather_knowledge_command(text)
        task_id = _task_id_from_text(text)
        task = _task_from_task_id(task_id) if task_id else None
        if task_id and not task:
            return {
                "status": "task_not_found",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "text": f"没有找到任务 ID：{task_id}。请确认任务助手已经发布过该任务，或直接问我城市天气。",
            }

        task_submission_mode = task is not None
        display_metrics = None if task_submission_mode else weather_metrics_from_text(text)
        unsupported_metrics = [] if task_submission_mode else unsupported_weather_metric_labels(text)
        if not task_submission_mode and _is_weather_knowledge_question(text) and (
            _needs_region_clarification(text) or len(text) >= 30
        ):
            # 咨询词 + (无地区 或 长文本) = 分析类问题(如台风情景推演), 走知识 LLM 而非数据卡片
            return await _handle_weather_knowledge_command(text)
        if not task_submission_mode and _date_span_status(text) == "beyond":
            # 目标日超出"今天起未来16天"预报窗: 明确提示, 不用近端数据冒充远期
            return {
                "status": "handled",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "mode": "beyond_horizon",
                "text": _beyond_horizon_text(),
            }
        if not task_submission_mode and unsupported_metrics and not display_metrics:
            return {
                "status": "unsupported_metric",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "mode": "unsupported_metric",
                "unsupported_metrics": unsupported_metrics,
                "text": _unsupported_weather_metric_text(unsupported_metrics),
            }
        comparison_regions = [] if task_submission_mode else _comparison_regions_from_text(text)
        if not task_submission_mode and len(comparison_regions) >= 2:
            if len(comparison_regions) > MAX_COMPARISON_REGIONS:
                return {
                    "status": "too_many_regions",
                    "bot_role": WEATHER_FORECAST_BOT_ROLE,
                    "mode": "weather_comparison",
                    "regions": comparison_regions,
                    "max_regions": MAX_COMPARISON_REGIONS,
                    "text": _too_many_comparison_regions_text(comparison_regions),
                }
            return await _handle_weather_comparison_command(text, comparison_regions, display_metrics, unsupported_metrics)
        llm_region_override = None
        if not task_submission_mode and _needs_region_clarification(text):
            llm_region_override = await extract_location_with_llm(llm_client, text)
            if llm_region_override and not _location_candidate_supported_by_text(llm_region_override, text):
                # 未在原文中找到依据的模型地点不得直接进入天气接口。
                llm_region_override = None
            if not llm_region_override:
                days = _days_from_text(text)
                return {
                    "status": "needs_region",
                    "bot_role": WEATHER_FORECAST_BOT_ROLE,
                    "mode": "clarification",
                    "days": days,
                    "text": _region_clarification_text(days, "forecast"),
                }
        elif not task_submission_mode:
            _regex_region = _explicit_region_from_text(text)
            if _is_province_only_region(_regex_region) and _has_extra_place_after_province(text, str(_regex_region)):
                # regex 只抓到省名且其后疑似还有更具体地名(如"辽宁盘锦"), 用 LLM 抽市/县
                candidate = await extract_location_with_llm(llm_client, text)
                if candidate and not _location_candidate_supported_by_text(candidate, text):
                    candidate = None
                if candidate and candidate.startswith(str(_regex_region)):
                    trimmed = candidate[len(str(_regex_region)):].lstrip("省市 ")
                    if trimmed:
                        candidate = trimmed
                if candidate and candidate != _regex_region and not _is_province_only_region(candidate):
                    llm_region_override = candidate
                else:
                    # LLM 未能定位到具体市/县(超时/禁用/只回省名): 不要静默退成省会单点数据, 追问具体城市
                    days = _days_from_text(text)
                    return {
                        "status": "needs_region",
                        "bot_role": WEATHER_FORECAST_BOT_ROLE,
                        "mode": "clarification",
                        "days": days,
                        "text": _region_clarification_text(days, "forecast"),
                    }
        request = _request_from_task(task) if task_submission_mode else _request_from_text(text)
        if llm_region_override:
            request = request.model_copy(update={"region": llm_region_override})
        request = _apply_favorite_alias(request, location_book)
        report_url = _public_weather_report_url(settings, request, display_metrics)
        download_url = _public_weather_download_url(settings, request)
        json_url = _public_weather_json_url(settings, request)
        if request.days > 1:
            submissions, errors = await _collect_cached_forecasts(request)
            if task_submission_mode:
                submissions = [submission.model_copy(update={"task_id": task.task_id}) for submission in submissions]
            _card_notice = None
            if not task_submission_mode:
                _start, _days, _raw_days, _status = weather_dates.parse_date_span(text)
                if _status == "truncated":
                    _card_notice = f"⚠️ 云云最多预报未来 16 天，你问的 {_raw_days} 天已为你取前 {request.days} 天。"
            card = (
                build_feishu_card(
                    submissions[0],
                    report_url=report_url,
                    download_url=download_url,
                    json_url=json_url,
                    chart_submissions=submissions,
                    show_task_id=task_submission_mode,
                    metrics=display_metrics,
                    notice=_card_notice,
                )
                if submissions
                else None
            )
            response = {
                "status": "partial" if errors else "handled",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "mode": "task_submission" if task_submission_mode else "instant_query",
                "region": submissions[0].region if submissions else request.region,
                "days": request.days,
                "report_url": report_url,
                "download_url": download_url,
                "json_url": json_url,
                "card": card,
                "submissions": [submission.model_dump(mode="json") for submission in submissions],
                "errors": errors,
            }
            if display_metrics:
                response["metrics"] = display_metrics
            if unsupported_metrics:
                response["unsupported_metrics"] = unsupported_metrics
            if task_submission_mode:
                response["task_id"] = task.task_id
                response["_record_submissions"] = submissions
            elif submissions:
                # 即时查询也把真实预报挂上供记忆摘要(温度/降水/风), 让"那适合晾衣服吗"能引用真实数值
                response["_memory_submissions"] = submissions
            return response
        result = await service.forecast(request)
        _store_cached_forecasts(request, [result], [])
        if task_submission_mode:
            result = result.model_copy(update={"task_id": task.task_id})
        _single_notice = None
        if not task_submission_mode and unsupported_metrics and display_metrics:
            # 混合指标: 展示了支持的, 但别静默吞掉不支持的, 明确告知
            _single_notice = f"ℹ️ {'、'.join(unsupported_metrics)} 云云暂未接入，本卡片未包含。"
        card = build_feishu_card(
            result,
            report_url=report_url,
            download_url=download_url,
            json_url=json_url,
            show_task_id=task_submission_mode,
            metrics=display_metrics,
            notice=_single_notice,
        )
        response = {
            "status": "handled",
            "bot_role": WEATHER_FORECAST_BOT_ROLE,
            "mode": "task_submission" if task_submission_mode else "instant_query",
            "report_url": report_url,
            "download_url": download_url,
            "json_url": json_url,
            "card": card,
        }
        if display_metrics:
            response["metrics"] = display_metrics
        if unsupported_metrics:
            response["unsupported_metrics"] = unsupported_metrics
        if task_submission_mode:
            response["task_id"] = task.task_id
            response["_record_submission"] = result
        else:
            response["_memory_submissions"] = [result]
        return {
            **response,
        }

    async def _handle_task_command(
        text: str,
        feishu_client: FeishuClient,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = _task_action_from_text(text)
        if action in {"query", "remind", "close"}:
            task_id = _task_id_from_text(text)
            if not task_id and isinstance(event, dict):
                state = _load_conversation_state(event, WEATHER_TASK_BOT_ROLE) or {}
                task_id = str(state.get("last_task_id") or "") or None
            if not task_id:
                return {
                    "status": "needs_task_id",
                    "bot_role": WEATHER_TASK_BOT_ROLE,
                    "mode": f"task_{action}",
                    "text": "请提供气象任务 ID；如果是刚发布的任务，也可以直接回复“查询刚才的任务”。",
                }
            task = _task_from_task_id(task_id)
            if not task:
                return {
                    "status": "task_not_found",
                    "bot_role": WEATHER_TASK_BOT_ROLE,
                    "mode": f"task_{action}",
                    "text": f"没有找到任务 ID：{task_id}。请确认 ID 是否正确。",
                }
            if action == "remind":
                task = _cache_task(task_service.remind(task))
            elif action == "close":
                task = _cache_task(task_service.close(task))
            if action in {"remind", "close"}:
                task_recorder.append(task)
                await feishu_client.write_task_bitable_record(task)
            return {
                "status": "handled",
                "bot_role": WEATHER_TASK_BOT_ROLE,
                "mode": f"task_{action}",
                "task": task.model_dump(mode="json"),
                "card": build_task_card(task),
                "text": build_task_text(task),
            }

        if _needs_task_region_clarification(text):
            days = _days_from_text(text)
            return {
                "status": "needs_region",
                "bot_role": WEATHER_TASK_BOT_ROLE,
                "mode": "clarification",
                "days": days,
                "text": _region_clarification_text(days, "task"),
            }
        task_request = _task_request_from_text(text)
        location = await _resolve_task_location(location_resolver, task_request)
        task = _cache_task(
            task_service.publish(
                task_service.create_dayahead_task(task_request.target_date, location, task_request.days)
            )
        )
        card = build_task_card(task)
        task_recorder.append(task)
        await feishu_client.write_task_bitable_record(task)
        return {
            "status": "handled",
            "bot_role": WEATHER_TASK_BOT_ROLE,
            "task": task.model_dump(mode="json"),
            "card": card,
            "text": build_task_text(task),
        }

    async def _handle_general_command(text: str, allowed_bot: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
        bot_role = _bot_role_for_allowed_bot(allowed_bot)
        history = (
            _recent_conversation_turns(
                bot_role,
                _event_chat_id(event),
                _event_thread_id(event),
                _event_sender_id(event),
                _event_chat_type(event),
            )
            if isinstance(event, dict)
            else []
        )
        return {
            "status": "handled",
            "bot_role": bot_role,
            "text": await answer_role_question(
                llm_client,
                bot_role=bot_role,
                user_text=text,
                fallback=_help_text(allowed_bot),
                history=history,
            ),
        }

    async def _handle_pending_region_reply(
        text: str,
        event: dict[str, Any],
        allowed_bot: str,
        feishu_client: FeishuClient,
    ) -> dict[str, Any] | None:
        if _is_weather_command(text) or _is_task_command(text) or _is_task_submission_command(text):
            return None
        pending = _take_pending_region(event, allowed_bot)
        if not pending:
            return None
        pending_command_type = str(pending.get("command_type") or "")
        needs_region = (
            _needs_task_region_clarification(text)
            if pending_command_type == "task"
            else _needs_region_clarification(text)
        )
        if needs_region:
            return None

        _clear_pending_region(event, allowed_bot)
        command_text = _merge_pending_region_text(text, pending)
        if pending.get("command_type") == "task":
            await _send_progress_message(feishu_client, event, WEATHER_TASK_BOT_ROLE)
            return await _handle_task_command(command_text, feishu_client, event)
        await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
        return await _handle_weather_command(command_text)

    async def _send_progress_message(feishu_client: FeishuClient, event: dict[str, Any], bot_role: str) -> None:
        if not progress_messages_enabled:
            return
        chat_id = _event_chat_id(event)
        if not chat_id:
            return
        try:
            await feishu_client.send_text_message(chat_id, _progress_text(bot_role))
        except Exception as exc:  # noqa: BLE001 - progress messages are best effort only
            logger.warning("feishu_progress_message_failed bot_role=%s error=%s", bot_role, exc)

    async def _record_task_submission(submission: WeatherSubmission, card_message_id: str | None = None) -> None:
        recorder.append(
            SubmissionRecord(
                submission=submission,
                card_message_id=card_message_id,
                status="submitted_to_task",
                notes="task_submission",
            )
        )
        await weather_feishu.write_bitable_record(submission, card_message_id)

    async def _handle_feishu_event(
        payload: dict[str, Any],
        account: FeishuBotAccount,
        feishu_client: FeishuClient,
        allowed_bot: str,
    ) -> dict[str, Any]:
        logger.warning(
            "feishu_event_received allowed_bot=%s schema=%s event_type=%s has_encrypt=%s payload_keys=%s",
            allowed_bot,
            payload.get("schema", ""),
            _feishu_event_type(payload),
            "encrypt" in payload,
            sorted(payload.keys()),
        )
        if not verify_feishu_token(payload, account.verification_token):
            raise HTTPException(status_code=403, detail="Invalid Feishu verification token")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        event_type = _feishu_event_type(payload)
        if event_type and event_type != "im.message.receive_v1":
            return {"status": "ignored", "reason": "unsupported_event_type", "event_type": event_type}

        event = payload.get("event", {})
        if _is_group_chat(event) and not _is_supported_group_message_type(event):
            return {
                "status": "ignored",
                "bot_role": _bot_role_for_allowed_bot(allowed_bot),
                "reason": "unsupported_group_message_type",
            }

        raw_text = _event_text(event)
        if (
            _is_group_chat(event)
            and not _is_addressed_to_bot(raw_text, event, allowed_bot)
            and not _is_reply_to_recorded_bot_message(event, allowed_bot)
        ):
            return {
                "status": "ignored",
                "bot_role": _bot_role_for_allowed_bot(allowed_bot),
                "reason": "group_message_not_addressed",
            }
        if (
            allowed_bot in {FEISHU_WEATHER_BOT, FEISHU_TASK_BOT}
            and not _is_group_chat(event)
            and not _is_direct_chat(event)
            and not _is_legacy_specialized_event_addressed(raw_text, event, allowed_bot)
        ):
            return {
                "status": "ignored",
                "bot_role": _bot_role_for_allowed_bot(allowed_bot),
            }

        logger.warning(
            "feishu_event_text allowed_bot=%s event_keys=%s message_keys=%s text=%r",
            allowed_bot,
            sorted(event.keys()) if isinstance(event, dict) else [],
            sorted(event.get("message", {}).keys()) if isinstance(event, dict) and isinstance(event.get("message"), dict) else [],
            raw_text,
        )

        text = _normalize_event_text(raw_text, event, allowed_bot)
        event["_normalized_text"] = text
        contextual_text, context_action = _contextual_weather_text(text, event)
        if context_action == "reset":
            _clear_pending_region(event, allowed_bot)
            result = {
                "status": "handled",
                "bot_role": WEATHER_FORECAST_BOT_ROLE,
                "mode": "context_reset",
                "text": "已清除刚才的天气查询上下文。请重新告诉我地点、日期和想看的指标。",
            }
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
        text = contextual_text

        if _is_help_command(text):
            _clear_pending_region(event, allowed_bot)
            result = {"status": "handled", "bot_role": _bot_role_for_allowed_bot(allowed_bot), "text": _help_text(allowed_bot)}
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)

        if _is_power_briefing_command(text):
            _clear_pending_region(event, allowed_bot)
            if allowed_bot == FEISHU_TASK_BOT:
                result = _redirect_to_bot_command(WEATHER_TASK_BOT_ROLE, WEATHER_FORECAST_BOT_ROLE)
            else:
                await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
                result = await _handle_power_briefing_command(text, event)
            return await _send_feishu_event_response(
                feishu_client,
                event,
                result,
                _record_task_submission,
            )

        pending_result = await _handle_pending_region_reply(text, event, allowed_bot, feishu_client)
        if pending_result:
            return await _send_feishu_event_response(feishu_client, event, pending_result, _record_task_submission)

        if allowed_bot == FEISHU_WEATHER_BOT:
            if _is_task_submission_command(text):
                await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
                result = await _handle_weather_command(text)
                return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
            if _is_task_command(text):
                result = _redirect_to_bot_command(WEATHER_FORECAST_BOT_ROLE, WEATHER_TASK_BOT_ROLE)
                return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
            if _is_weather_command(text):
                if not _needs_region_clarification(text):
                    _clear_pending_region(event, allowed_bot)
                    await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
                else:
                    _remember_pending_region(event, allowed_bot, "forecast", text)
                result = await _handle_weather_command(text)
                return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
            await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
            result = await _handle_general_command(text, allowed_bot, event)
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)

        if allowed_bot == FEISHU_TASK_BOT:
            if _is_task_submission_command(text):
                result = _redirect_to_bot_command(WEATHER_TASK_BOT_ROLE, WEATHER_FORECAST_BOT_ROLE)
                return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
            if _is_task_command(text):
                if _task_action_from_text(text) in {"query", "remind", "close"} or not _needs_task_region_clarification(text):
                    _clear_pending_region(event, allowed_bot)
                    await _send_progress_message(feishu_client, event, WEATHER_TASK_BOT_ROLE)
                else:
                    _remember_pending_region(event, allowed_bot, "task", text)
                result = await _handle_task_command(text, feishu_client, event)
                return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
            if _is_weather_command(text):
                result = _redirect_to_bot_command(WEATHER_TASK_BOT_ROLE, WEATHER_FORECAST_BOT_ROLE)
                return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
            await _send_progress_message(feishu_client, event, WEATHER_TASK_BOT_ROLE)
            result = await _handle_general_command(text, allowed_bot, event)
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)

        if _is_task_submission_command(text):
            await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
            result = await _handle_weather_command(text)
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
        if _is_task_command(text):
            if _task_action_from_text(text) in {"query", "remind", "close"} or not _needs_task_region_clarification(text):
                _clear_pending_region(event, allowed_bot)
                await _send_progress_message(feishu_client, event, WEATHER_TASK_BOT_ROLE)
            else:
                _remember_pending_region(event, allowed_bot, "task", text)
            result = await _handle_task_command(text, feishu_client, event)
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
        if _is_weather_command(text):
            if not _needs_region_clarification(text):
                _clear_pending_region(event, allowed_bot)
                await _send_progress_message(feishu_client, event, WEATHER_FORECAST_BOT_ROLE)
            else:
                _remember_pending_region(event, allowed_bot, "forecast", text)
            result = await _handle_weather_command(text)
            return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)
        await _send_progress_message(feishu_client, event, _bot_role_for_allowed_bot(allowed_bot))
        result = await _handle_general_command(text, allowed_bot, event)
        return await _send_feishu_event_response(feishu_client, event, result, _record_task_submission)

    async def _safe_handle_feishu_event(
        payload: dict[str, Any],
        account: FeishuBotAccount,
        feishu_client: FeishuClient,
        allowed_bot: str,
    ) -> dict[str, Any]:
        event = payload.get("event", {}) if isinstance(payload, dict) else {}
        header = payload.get("header", {}) if isinstance(payload, dict) else {}
        event_id = str(header.get("event_id")) if isinstance(header, dict) and header.get("event_id") else None
        if not event_id:
            message_id = _event_message_id(event) if isinstance(event, dict) else None
            if message_id and _seen_fallback_message(
                fallback_processed_message_ids,
                allowed_bot,
                message_id,
            ):
                return {
                    "status": "ignored",
                    "reason": "duplicate_message",
                    "bot_role": _bot_role_for_allowed_bot(allowed_bot),
                }
        if event_id:
            try:
                if not weather_memory.claim_event(allowed_bot, event_id):
                    logger.warning("feishu_event_duplicate allowed_bot=%s event_id=%s", allowed_bot, event_id)
                    return {
                        "status": "ignored",
                        "reason": "duplicate_event",
                        "bot_role": _bot_role_for_allowed_bot(allowed_bot),
                    }
            except Exception:  # noqa: BLE001 - idempotency storage failure degrades to normal handling
                logger.exception("feishu_event_claim_failed allowed_bot=%s", allowed_bot)
        try:
            result = await _handle_feishu_event(payload, account, feishu_client, allowed_bot)
            if event_id:
                try:
                    if result.get("event_reply_error"):
                        weather_memory.fail_event(allowed_bot, event_id)
                    else:
                        weather_memory.complete_event(allowed_bot, event_id, result)
                except Exception:  # noqa: BLE001 - ledger failure must not turn a successful reply into fallback
                    logger.exception("feishu_event_finalize_failed allowed_bot=%s event_id=%s", allowed_bot, event_id)
            return result
        except HTTPException:
            if event_id:
                try:
                    weather_memory.fail_event(allowed_bot, event_id)
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception:  # noqa: BLE001 - 永不沉默: 任何未预料异常都回复用户并 200
            logger.exception("feishu_event_unhandled_error allowed_bot=%s", allowed_bot)
            if event_id:
                try:
                    weather_memory.fail_event(allowed_bot, event_id)
                except Exception:  # noqa: BLE001
                    pass
            try:
                event = payload.get("event", {}) if isinstance(payload, dict) else {}
                chat_id = _event_chat_id(event)
                if chat_id:
                    fallback_text = (
                        "云云处理这条消息时出了点小状况😥 已经记下来修啦~\n"
                        "可以换个问法再试试：查天气用「城市 + 未来3天」；分析类问题稍后再问我一次。"
                    )
                    incoming_message_id = _event_message_id(event)
                    thread_id = _event_thread_id(event)
                    if incoming_message_id and thread_id:
                        await feishu_client.reply_text_message(incoming_message_id, fallback_text, in_thread=True)
                    else:
                        await feishu_client.send_text_message(chat_id, fallback_text)
            except Exception:  # noqa: BLE001
                pass
            return {"status": "error_fallback", "bot_role": _bot_role_for_allowed_bot(allowed_bot)}

    @app.post("/feishu/events")
    async def feishu_events(payload: dict[str, Any]) -> dict[str, Any]:
        return await _safe_handle_feishu_event(payload, legacy_account, legacy_feishu, FEISHU_LEGACY_BOT)

    @app.post("/feishu/events/weather")
    async def feishu_weather_events(payload: dict[str, Any]) -> dict[str, Any]:
        return await _safe_handle_feishu_event(payload, weather_account, weather_feishu, FEISHU_WEATHER_BOT)

    @app.post("/feishu/events/task")
    async def feishu_task_events(payload: dict[str, Any]) -> dict[str, Any]:
        return await _safe_handle_feishu_event(payload, task_account, task_feishu, FEISHU_TASK_BOT)

    return app


def _legacy_feishu_account(settings: Settings, verification_token_override: str | None = None) -> FeishuBotAccount:
    return FeishuBotAccount(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        verification_token=verification_token_override
        if verification_token_override is not None
        else settings.feishu_verification_token,
        encrypt_key=settings.feishu_encrypt_key,
        default_chat_id=settings.feishu_default_chat_id,
        name=FEISHU_LEGACY_BOT,
    )


def _role_feishu_account(settings: Settings, role: str, legacy: FeishuBotAccount) -> FeishuBotAccount:
    if role == FEISHU_WEATHER_BOT:
        return FeishuBotAccount(
            app_id=settings.feishu_weather_app_id or legacy.app_id,
            app_secret=settings.feishu_weather_app_secret or legacy.app_secret,
            verification_token=settings.feishu_weather_verification_token
            if settings.feishu_weather_verification_token is not None
            else legacy.verification_token,
            encrypt_key=settings.feishu_weather_encrypt_key or legacy.encrypt_key,
            default_chat_id=settings.feishu_weather_default_chat_id or legacy.default_chat_id,
            name=FEISHU_WEATHER_BOT,
        )
    if role == FEISHU_TASK_BOT:
        return FeishuBotAccount(
            app_id=settings.feishu_task_app_id or legacy.app_id,
            app_secret=settings.feishu_task_app_secret or legacy.app_secret,
            verification_token=settings.feishu_task_verification_token
            if settings.feishu_task_verification_token is not None
            else legacy.verification_token,
            encrypt_key=settings.feishu_task_encrypt_key or legacy.encrypt_key,
            default_chat_id=settings.feishu_task_default_chat_id or legacy.default_chat_id,
            name=FEISHU_TASK_BOT,
        )
    raise ValueError(f"Unknown Feishu bot role: {role}")


def _bot_role_for_allowed_bot(allowed_bot: str) -> str:
    if allowed_bot == FEISHU_WEATHER_BOT:
        return WEATHER_FORECAST_BOT_ROLE
    if allowed_bot == FEISHU_TASK_BOT:
        return WEATHER_TASK_BOT_ROLE
    return "legacy_combined_bot"


def _progress_text(bot_role: str) -> str:
    if bot_role == WEATHER_FORECAST_BOT_ROLE:
        return "⛅ 收到~ 云云正在汇总多家气象数据，马上给你生成预测卡片，请稍候。"
    if bot_role == WEATHER_TASK_BOT_ROLE:
        return "📋 收到~ 点点正在处理这条气象共测任务，请稍候。"
    return "收到~ 正在处理，请稍候。"


def _feishu_event_type(payload: dict[str, Any]) -> str:
    header = payload.get("header", {})
    if isinstance(header, dict):
        event_type = header.get("event_type")
        if isinstance(event_type, str):
            return event_type
    event_type = payload.get("type")
    return event_type if isinstance(event_type, str) else ""


def _event_message_id(event: dict[str, Any]) -> str | None:
    message = event.get("message", {})
    message_id = message.get("message_id") if isinstance(message, dict) else None
    if not message_id:
        message_id = event.get("message_id")
    return str(message_id) if message_id else None


def _payload_event_id(payload: dict[str, Any]) -> str | None:
    header = payload.get("header", {}) if isinstance(payload, dict) else {}
    event_id = header.get("event_id") if isinstance(header, dict) else None
    if event_id:
        return str(event_id)
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    return _event_message_id(event) if isinstance(event, dict) else None


def _event_thread_id(event: dict[str, Any]) -> str | None:
    message = event.get("message", {})
    if not isinstance(message, dict):
        return None
    # 飞书回复线程有时只给 root_id / parent_id，三者应归入同一隔离维度。
    thread_id = message.get("thread_id") or message.get("root_id") or message.get("parent_id")
    return str(thread_id) if thread_id else None


def _seen_fallback_message(
    seen: dict[str, float],
    allowed_bot: str,
    message_id: str,
    ttl_seconds: int = 600,
) -> bool:
    """Compatibility dedup for synthetic/legacy events that have no event_id."""
    now = time.monotonic()
    for key, created_at in list(seen.items()):
        if now - created_at > ttl_seconds:
            seen.pop(key, None)
    key = f"{allowed_bot}:{message_id}"
    if key in seen:
        return True
    seen[key] = now
    return False


def _event_sender_id(event: dict[str, Any]) -> str:
    sender = event.get("sender", {}) if isinstance(event, dict) else {}
    if not isinstance(sender, dict):
        return ""
    sid = sender.get("sender_id", {})
    if isinstance(sid, dict):
        return str(sid.get("open_id") or sid.get("user_id") or sid.get("union_id") or "")
    return str(sid or "")


def _event_chat_type(event: dict[str, Any]) -> str:
    message = event.get("message", {}) if isinstance(event, dict) else {}
    chat_type = message.get("chat_type") if isinstance(message, dict) else None
    if not chat_type:
        chat_type = event.get("chat_type") if isinstance(event, dict) else None
    return str(chat_type or "unknown").lower()


def _conversation_state_key(event: dict[str, Any], bot_role: str) -> str | None:
    chat_id = _event_chat_id(event)
    sender_id = _event_sender_id(event)
    if not chat_id or not sender_id:
        return None
    return _conversation_key(
        bot_role,
        chat_id,
        _event_thread_id(event),
        sender_id,
        _event_chat_type(event),
    )


def _load_conversation_state(event: dict[str, Any], bot_role: str) -> dict[str, Any] | None:
    key = _conversation_state_key(event, bot_role)
    if not key:
        return None
    try:
        return weather_memory.load_conversation_state(key)
    except Exception:  # noqa: BLE001 - memory is best effort
        return None


def _load_shared_briefing_thread_state(
    event: dict[str, Any],
    bot_role: str,
) -> dict[str, Any] | None:
    chat_id = _event_chat_id(event)
    thread_id = _event_thread_id(event)
    if not chat_id or not thread_id:
        return None
    key = _conversation_key(
        bot_role,
        chat_id,
        thread_id,
        "*",
        _event_chat_type(event),
    )
    try:
        return weather_memory.load_conversation_state(key)
    except Exception:  # noqa: BLE001 - memory is best effort
        return None


def _clear_conversation_state(event: dict[str, Any], bot_role: str) -> None:
    key = _conversation_state_key(event, bot_role)
    if not key:
        return
    try:
        weather_memory.clear_conversation_state(key)
    except Exception:  # noqa: BLE001 - memory is best effort
        pass


def _pending_region_key(
    allowed_bot: str,
    chat_id: str,
    sender_id: str = "",
    thread_id: str = "",
    chat_type: str = "",
) -> str:
    return f"{allowed_bot}:{chat_type or 'unknown'}:{chat_id}:{sender_id}:{thread_id}"


def _merge_pending_region_text(region_text: str, pending: dict[str, Any]) -> str:
    target_date = str(pending.get("target_date") or _target_date_from_text(region_text))
    days = int(pending.get("days") or 1)
    metric_phrase = weather_metric_phrase(pending.get("metrics"))
    suffix = f" {metric_phrase}" if metric_phrase else ""
    if pending.get("command_type") == "task":
        return f"发布{region_text} {target_date} 未来{days}天气象任务"
    return f"{region_text} {target_date} 未来{days}天{suffix}"


def _is_addressed_to_bot(_text: str, event: dict[str, Any], allowed_bot: str) -> bool:
    aliases = _bot_aliases(allowed_bot)
    if not aliases:
        return False
    if _is_direct_chat(event):
        return True

    mention_values = _event_mention_texts(event)
    return any(alias in item for alias in aliases for item in mention_values)


def _is_legacy_specialized_event_addressed(
    text: str,
    event: dict[str, Any],
    allowed_bot: str,
) -> bool:
    """Compatibility for old webhook fixtures that omit chat_type and structured mentions."""
    aliases = _bot_aliases(allowed_bot)
    searchable = [text, *_event_mention_texts(event)]
    return any(alias in item for alias in aliases for item in searchable)


def _is_group_chat(event: dict[str, Any]) -> bool:
    return _event_chat_type(event) in {"group", "group_chat"}


def _event_message_type(event: dict[str, Any]) -> str:
    message = event.get("message", {}) if isinstance(event, dict) else {}
    message_type = message.get("message_type") if isinstance(message, dict) else None
    return str(message_type or "").strip().lower()


def _is_supported_group_message_type(event: dict[str, Any]) -> bool:
    # Fail closed for cards, files, images, audio and other machine payloads.
    # Feishu's normal @ conversation messages are message_type=text.
    return _event_message_type(event) == "text"


def _bot_reply_marker_key(chat_id: str, message_id: str) -> str:
    return f"bot-reply|{chat_id}|{message_id}"


def _remember_bot_reply_message(
    event: dict[str, Any],
    message_id: str,
    bot_role: str,
) -> None:
    if not _is_group_chat(event) or not message_id:
        return
    chat_id = _event_chat_id(event)
    if not chat_id:
        return
    try:
        weather_memory.save_conversation_state(
            _bot_reply_marker_key(chat_id, message_id),
            {
                "state_version": 1,
                "source": "recorded_bot_reply",
                "chat_id": chat_id,
                "message_id": message_id,
                "bot_role": bot_role,
            },
        )
    except Exception:  # noqa: BLE001 - a delivered reply must not be retried or duplicated
        logger.exception("feishu_bot_reply_marker_save_failed chat_id=%s", chat_id)


def _event_reply_target_ids(event: dict[str, Any]) -> list[str]:
    message = event.get("message", {}) if isinstance(event, dict) else {}
    if not isinstance(message, dict):
        return []
    target_ids: list[str] = []
    for key in ("root_id", "parent_id"):
        value = message.get(key)
        if value and str(value) not in target_ids:
            target_ids.append(str(value))
    return target_ids


def _is_reply_to_recorded_bot_message(event: dict[str, Any], allowed_bot: str) -> bool:
    if not _is_group_chat(event):
        return False
    chat_id = _event_chat_id(event)
    if not chat_id:
        return False
    allowed_marker_roles = {
        FEISHU_WEATHER_BOT: {WEATHER_FORECAST_BOT_ROLE},
        FEISHU_TASK_BOT: {WEATHER_TASK_BOT_ROLE},
        FEISHU_LEGACY_BOT: {
            WEATHER_FORECAST_BOT_ROLE,
            WEATHER_TASK_BOT_ROLE,
            "legacy_combined_bot",
        },
    }.get(allowed_bot, set())
    for target_id in _event_reply_target_ids(event):
        try:
            marker = weather_memory.load_conversation_state(
                _bot_reply_marker_key(chat_id, target_id)
            )
            marker_role = str(marker.get("bot_role") or "") if marker else ""
            if (
                marker
                and marker.get("source") == "recorded_bot_reply"
                and marker_role in allowed_marker_roles
            ):
                return True

            # Backward compatibility for scheduled briefing cards sent before the strict gate.
            scheduled_key = _conversation_key(
                WEATHER_FORECAST_BOT_ROLE,
                chat_id,
                target_id,
                "*",
                _event_chat_type(event),
            )
            scheduled_state = weather_memory.load_conversation_state(scheduled_key)
            if (
                allowed_bot in {FEISHU_WEATHER_BOT, FEISHU_LEGACY_BOT}
                and scheduled_state
                and scheduled_state.get("source") == "scheduled_briefing"
            ):
                return True
        except Exception:  # noqa: BLE001 - fail closed when reply ownership cannot be proven
            logger.exception("feishu_bot_reply_marker_load_failed chat_id=%s", chat_id)
            return False
    return False


def _is_direct_chat(event: dict[str, Any]) -> bool:
    message = event.get("message", {})
    chat_type = message.get("chat_type") if isinstance(message, dict) else None
    if not chat_type:
        chat_type = event.get("chat_type")
    return str(chat_type).lower() in {"p2p", "private", "single", "direct"}


def _bot_aliases(allowed_bot: str) -> list[str]:
    if allowed_bot == FEISHU_WEATHER_BOT:
        return WEATHER_BOT_ALIASES
    if allowed_bot == FEISHU_TASK_BOT:
        return TASK_BOT_ALIASES
    if allowed_bot == FEISHU_LEGACY_BOT:
        return WEATHER_BOT_ALIASES + TASK_BOT_ALIASES
    return []


def _event_mention_texts(event: dict[str, Any]) -> list[str]:
    message = event.get("message", {})
    mentions = message.get("mentions") if isinstance(message, dict) else None
    if not isinstance(mentions, list):
        return []

    values = []
    for mention in mentions:
        values.extend(_string_values(mention))
    return values


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(_string_values(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_string_values(item))
        return values
    return []


def _redirect_to_bot_command(current_bot_role: str, suggested_bot_role: str) -> dict[str, str]:
    if current_bot_role == WEATHER_TASK_BOT_ROLE:
        suggested_bot_name = "气象小助手云云"
        suggested_event_path = "/feishu/events/weather"
        text = "这个问题交给气象小助手「云云」更合适哦~ 我是点点，负责气象任务的发布、提醒、关闭和记录。"
    else:
        suggested_bot_name = "任务小助手点点"
        suggested_event_path = "/feishu/events/task"
        text = "这个任务交给任务小助手「点点」就好~ 我是云云，负责天气预测、报告和数据导出。"
    return {
        "status": "redirect",
        "bot_role": current_bot_role,
        "suggested_bot_role": suggested_bot_role,
        "suggested_bot_name": suggested_bot_name,
        "suggested_event_path": suggested_event_path,
        "text": text,
    }


async def _send_feishu_event_response(
    feishu_client: FeishuClient,
    event: dict[str, Any],
    result: dict[str, Any],
    record_submission: Callable[[WeatherSubmission, str | None], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    submission_to_record = result.pop("_record_submission", None)
    submissions_to_record = result.pop("_record_submissions", None)
    memory_submissions = result.pop("_memory_submissions", None)  # 即时查询: 仅供记忆, 不写 Bitable
    chat_id = _event_chat_id(event)
    if not chat_id or result.get("status") == "ignored":
        if isinstance(submission_to_record, WeatherSubmission) and record_submission:
            try:
                await record_submission(submission_to_record, None)
                result["submission_record_status"] = "accepted"
            except Exception as exc:  # noqa: BLE001 - event ack should not depend on Bitable writes
                result["submission_record_error"] = str(exc)
        if isinstance(submissions_to_record, list) and record_submission:
            try:
                recorded = 0
                for submission in submissions_to_record:
                    if isinstance(submission, WeatherSubmission):
                        await record_submission(submission, None)
                        recorded += 1
                result["submission_record_status"] = "accepted"
                result["submission_record_count"] = recorded
            except Exception as exc:  # noqa: BLE001 - event ack should not depend on Bitable writes
                result["submission_record_error"] = str(exc)
        logger.warning(
            "feishu_event_result status=%s bot_role=%s has_chat_id=%s",
            result.get("status"),
            result.get("bot_role"),
            bool(chat_id),
        )
        return result

    try:
        message_id = ""
        card = result.get("card")
        text = result.get("text")
        incoming_message_id = _event_message_id(event)
        thread_id = _event_thread_id(event)
        if isinstance(card, dict):
            if incoming_message_id and thread_id:
                message_id = await feishu_client.reply_interactive_card(incoming_message_id, card, in_thread=True)
            else:
                message_id = await feishu_client.send_interactive_card(chat_id, card)
        elif isinstance(text, str) and text:
            # 含 Markdown 的富文本回答(知识/角色分析)走 lark_md 卡片渲染, 避免 ## / ** 原样露出;
            # 短系统消息(澄清/降级)仍走纯文本
            if is_rich_reply_text(text):
                reply_card = build_text_reply_card(text)
                if incoming_message_id and thread_id:
                    message_id = await feishu_client.reply_interactive_card(incoming_message_id, reply_card, in_thread=True)
                else:
                    message_id = await feishu_client.send_interactive_card(chat_id, reply_card)
            elif incoming_message_id and thread_id:
                message_id = await feishu_client.reply_text_message(incoming_message_id, text, in_thread=True)
            else:
                message_id = await feishu_client.send_text_message(chat_id, text)
        if message_id:
            result["event_reply_message_id"] = message_id
            _remember_bot_reply_message(
                event,
                message_id,
                str(result.get("bot_role") or ""),
            )
    except Exception as exc:  # noqa: BLE001 - ack the event even when message delivery fails
        result["event_reply_error"] = str(exc)
    if isinstance(submission_to_record, WeatherSubmission) and record_submission:
        try:
            await record_submission(submission_to_record, message_id or None)
            result["submission_record_status"] = "accepted"
        except Exception as exc:  # noqa: BLE001 - event ack should not depend on Bitable writes
            result["submission_record_error"] = str(exc)
    if isinstance(submissions_to_record, list) and record_submission:
        try:
            recorded = 0
            for submission in submissions_to_record:
                if isinstance(submission, WeatherSubmission):
                    await record_submission(submission, message_id or None)
                    recorded += 1
            result["submission_record_status"] = "accepted"
            result["submission_record_count"] = recorded
        except Exception as exc:  # noqa: BLE001 - event ack should not depend on Bitable writes
            result["submission_record_error"] = str(exc)
    logger.warning(
        "feishu_event_result status=%s bot_role=%s has_chat_id=%s reply_message_id=%s reply_error=%s",
        result.get("status"),
        result.get("bot_role"),
        bool(chat_id),
        result.get("event_reply_message_id", ""),
        result.get("event_reply_error", ""),
    )
    if result.get("event_reply_error"):
        return result
    try:
        _pref_subs = (
            submissions_to_record
            if isinstance(submissions_to_record, list)
            else memory_submissions
            if isinstance(memory_submissions, list)
            else ([submission_to_record] if isinstance(submission_to_record, WeatherSubmission) else [])
        )
        _pref_subs = [s for s in _pref_subs if isinstance(s, WeatherSubmission)]
        _bot_text = result.get("text") if isinstance(result.get("text"), str) else ""
        if not _bot_text and result.get("card"):
            _bot_text = _card_memory_summary(_pref_subs) or "[已发送天气卡片]"
        _record_conversation_turn(
            result.get("bot_role") or "",
            chat_id,
            _event_thread_id(event),
            _event_sender_id(event),
            str(event.get("_normalized_text") or _event_text(event)),
            _bot_text,
            _event_chat_type(event),
        )
        briefing_cache_key = result.get("briefing_cache_key")
        if (
            isinstance(briefing_cache_key, str)
            and briefing_cache_key
            and result.get("status") == "handled"
        ):
            state_key = _conversation_state_key(event, WEATHER_FORECAST_BOT_ROLE)
            if state_key:
                state = weather_memory.load_conversation_state(state_key) or {"state_version": 1}
                state["last_power_briefing_cache_key"] = briefing_cache_key
                state["last_power_briefing_generated_at"] = result.get("generated_at")
                weather_memory.save_conversation_state(state_key, state)
                if message_id and not _event_thread_id(event):
                    reply_thread_key = _conversation_key(
                        WEATHER_FORECAST_BOT_ROLE,
                        chat_id,
                        message_id,
                        _event_sender_id(event),
                        _event_chat_type(event),
                    )
                    weather_memory.save_conversation_state(reply_thread_key, state)
        if _pref_subs:
            state_key = _conversation_state_key(event, WEATHER_FORECAST_BOT_ROLE)
            if state_key:
                state = weather_memory.load_conversation_state(state_key) or {"state_version": 1}
                first = _pref_subs[0]
                state["last_successful_request"] = {
                    "region": getattr(first, "region", None),
                    "target_date": getattr(first, "target_date", None),
                    "days": max(1, int(result.get("days") or len(_pref_subs))),
                    "metrics": result.get("metrics") or [],
                }
                weather_memory.save_conversation_state(state_key, state)
                if message_id and not _event_thread_id(event):
                    reply_thread_key = _conversation_key(
                        WEATHER_FORECAST_BOT_ROLE,
                        chat_id,
                        message_id,
                        _event_sender_id(event),
                        _event_chat_type(event),
                    )
                    weather_memory.save_conversation_state(reply_thread_key, state)
        task_payload = result.get("task")
        if isinstance(task_payload, dict) and task_payload.get("task_id"):
            state_key = _conversation_state_key(event, WEATHER_TASK_BOT_ROLE)
            if state_key:
                state = weather_memory.load_conversation_state(state_key) or {"state_version": 1}
                state["last_task_id"] = str(task_payload["task_id"])
                weather_memory.save_conversation_state(state_key, state)
                if message_id and not _event_thread_id(event):
                    reply_thread_key = _conversation_key(
                        WEATHER_TASK_BOT_ROLE,
                        chat_id,
                        message_id,
                        _event_sender_id(event),
                        _event_chat_type(event),
                    )
                    weather_memory.save_conversation_state(reply_thread_key, state)
    except Exception:  # noqa: BLE001
        pass
    return result


def _tomorrow_request() -> ForecastRequest:
    target = date.today() + timedelta(days=1)
    return ForecastRequest(target_date=target.isoformat(), granularity="1h")


def _apply_favorite_alias(request: ForecastRequest, location_book: LocationBook) -> ForecastRequest:
    if request.latitude is not None or request.longitude is not None:
        return request
    favorite = location_book.resolve(request.region)
    if not favorite:
        return request
    return request.model_copy(
        update={
            "region": favorite.name,
            "latitude": favorite.latitude,
            "longitude": favorite.longitude,
            "location_code": favorite.code,
            "location_source": favorite.source,
        }
    )


def _event_text(event: dict[str, Any]) -> str:
    message = event.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, dict):
            text = parsed.get("text")
            if isinstance(text, str):
                return text
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return str(content)


def _normalize_event_text(text: str, event: dict[str, Any], allowed_bot: str) -> str:
    normalized = (text or "").replace("\u200b", " ")
    normalized = re.sub(r"<at\b[^>]*>.*?</at>", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"@_user_\d+\s*", " ", normalized)
    for alias in sorted(_bot_aliases(allowed_bot), key=len, reverse=True):
        normalized = re.sub(rf"@?\s*{re.escape(alias)}(?:机器人)?\s*", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip(" ，,。；;：:")


_CONTEXT_RESET_RE = re.compile(r"重新查|重新查询|不要沿用|别沿用|清除(?:上下文|记录)|忘掉(?:刚才|上次)")
_CONTEXT_FOLLOWUP_RE = re.compile(
    r"那明天|明天呢|后天呢|刚才那个|上次那个|换成|改成|不是.+是|只看|只要|重试一下|再试一次"
)
_DATE_SIGNAL_RE = re.compile(
    r"今天|今日|明天|明日|后天|大后天|未来|接下来|最近|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}月\d{1,2}[日号]|[一二三四五六七八九十\d]+[天日]"
)
_WINDOW_SIGNAL_RE = re.compile(r"(?:未来|接下来|最近|改成)\s*[一二两三四五六七八九十\d]+\s*[天日]")


def _region_from_followup_fragment(fragment: str) -> str | None:
    candidate = re.split(
        r"未来|接下来|最近|今天|今日|明天|明日|后天|天气|气象|预报|降雨|降水|温度|风速|云量",
        fragment,
        maxsplit=1,
    )[0].strip(" ，,。；;：:的是")
    if not candidate or re.fullmatch(r"[一二两三四五六七八九十\d]+[天日]?", candidate):
        return None
    cleaned = _clean_region_candidate(candidate)
    if cleaned and (cleaned in BUILTIN_LOCATIONS or cleaned in LOCATION_ALIAS_MAP):
        # 保留用户原文中的具体市/区县名，避免“辽宁盘锦”再次被省级规则截成辽宁。
        return cleaned
    explicit = _explicit_region_from_text(fragment)
    return explicit or cleaned


def _region_expression_for_followup(region: str) -> str:
    aliases = [
        alias
        for alias, normalized in LOCATION_ALIASES
        if normalized == region and not _is_province_only_region(alias) and len(alias) >= 2
    ]
    if aliases:
        return min(aliases, key=len)
    return region


def _contextual_weather_text(
    text: str,
    event: dict[str, Any],
) -> tuple[str, str | None]:
    """Merge a short weather follow-up with the last successful request.

    Returns ``(text, action)`` where action is ``reset`` when the caller should
    acknowledge a context reset without issuing a weather request.
    """
    if _CONTEXT_RESET_RE.search(text):
        _clear_conversation_state(event, WEATHER_FORECAST_BOT_ROLE)
        return text, "reset"

    state = _load_conversation_state(event, WEATHER_FORECAST_BOT_ROLE) or {}
    last = state.get("last_successful_request")
    if not isinstance(last, dict) or not last.get("region"):
        return text, None

    current_metrics = weather_metrics_from_text(text)
    explicit_region = _explicit_region_from_text(text)

    # 否定纠正必须只取肯定部分，不能把“广州、深圳”误判成两地对比。
    correction = re.search(r"(?:不是|不要)\s*[^，,。；;]+[，,。；;]\s*(?:是|改成|换成)?\s*(.+)", text)
    if correction:
        explicit_region = _region_from_followup_fragment(correction.group(1))
    else:
        replacement = re.search(r"(?:换成|改成)\s*(.+)", text)
        if replacement and not _WINDOW_SIGNAL_RE.search(text):
            explicit_region = _region_from_followup_fragment(replacement.group(1))

    is_followup = bool(_CONTEXT_FOLLOWUP_RE.search(text))
    is_followup = is_followup or bool(current_metrics and not explicit_region)
    is_followup = is_followup or bool(_DATE_SIGNAL_RE.search(text) and not explicit_region and len(text) <= 24)
    if not is_followup:
        return text, None

    region = explicit_region or _region_expression_for_followup(str(last.get("region")))
    target_date = str(last.get("target_date") or _target_date_from_text(text))
    days = max(1, int(last.get("days") or 1))
    metrics = current_metrics or last.get("metrics") or None

    if _DATE_SIGNAL_RE.search(text):
        target_date = _target_date_from_text(text)
        if re.search(r"明天|明日|后天|大后天|今天|今日", text) and not _WINDOW_SIGNAL_RE.search(text):
            days = 1
    if _WINDOW_SIGNAL_RE.search(text):
        days = _days_from_text(text)

    metric_phrase = weather_metric_phrase(metrics)
    metric_suffix = f" {metric_phrase}" if metric_phrase else ""
    return f"{region} {target_date} 未来{days}天{metric_suffix}", None


def _event_chat_id(event: dict[str, Any]) -> str | None:
    message = event.get("message", {})
    chat_id = message.get("chat_id") or event.get("chat_id")
    return str(chat_id) if chat_id else None


def _task_id_from_text(text: str) -> str | None:
    match = WEATHER_TASK_ID_RE.search(text)
    return match.group(0) if match else None


def _is_task_submission_command(text: str) -> bool:
    return _task_id_from_text(text) is not None and _task_action_from_text(text) not in {
        "query",
        "remind",
        "close",
    }


PAST_WEATHER_RE = re.compile(
    r"历史|回顾|实况|复盘|(昨|前)天|上上?\s*(周|星期|礼拜|个?月)|过去\s*[一两二三四五六七八九十0-9]*\s*(天|日|周|星期|礼拜|个?月)|去年"
)
_CN_MONTH = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _mentions_past_month(text: str) -> bool:
    from datetime import datetime as _dt

    current_month = _dt.now().month
    for token in re.findall(r"([0-9]{1,2}|十[一二]?|[一二三四五六七八九])\s*月", text):
        value = int(token) if token.isdigit() else _CN_MONTH.get(token, 0)
        if 1 <= value < current_month:
            return True
    return False


def _is_past_weather_query(text: str) -> bool:
    # 显式历史词(上周/昨天/去年/过去N天) 或 统一日期引擎解析出的目标日早于今天
    return bool(PAST_WEATHER_RE.search(text)) or _date_span_status(text) == "past"


def _past_weather_text() -> str:
    return (
        "云云这边只有**预报**数据（今天起未来 1-16 天），历史 / 实况天气暂时查不了🙏\n"
        "想看历史实况建议查中国气象局或当地气象台官网。\n"
        "我可以帮你看未来的，比如：\n"
        "• 盘锦未来7天\n"
        "• 盘锦明天天气"
    )


def _beyond_horizon_text() -> str:
    return (
        "云云的预报只到**今天起未来 16 天**，你问的日期超出了这个范围，暂时算不了🙏\n"
        "换到 16 天以内的日期就行，比如：\n"
        "• 盘锦未来7天\n"
        "• 盘锦这周末天气\n"
        "• 盘锦7月20日天气"
    )


FORECAST_WINDOW_HINT_RE = re.compile(r"今天|今日|明天|明日|后天|大后天|周末|下周|未来|最近|近期|接下来|这几天")


def _has_forecast_window_hint(text: str) -> bool:
    if FORECAST_WINDOW_HINT_RE.search(text):
        return True
    return _days_from_text(text) > 1


def _is_weather_command(text: str) -> bool:
    if _is_task_submission_command(text):
        return True
    if has_weather_metric_keyword(text):
        return True
    # 台风问句(无天气词也算): 走天气命令→知识处理器做实时 grounding, 不落闲聊 LLM
    if mentions_typhoon(text):
        return True
    # 多地对比意图(如"对比广州和深圳"/"广州和深圳哪个更热"): 无天气词也识别为天气查询
    if len(_comparison_regions_from_text(text)) >= 2:
        return True
    # 意图增强: 「时间窗口 + (已知地区 或 强预报窗)」视为天气查询;
    # 地名不在词表时(如"盘锦未来7天")留给 LLM 兜底抽取
    if _has_forecast_window_hint(text):
        if _explicit_region_from_text(text):
            return True
        if re.search(r"未来|最近|接下来|这几天", text) or _days_from_text(text) > 1:
            return True
    return any(
        keyword in text
        for keyword in [
            "天气",
            "气象",
            "预测",
            "预报",
            "降雨",
            "降水",
            "下雨",
            "雨",
            "气温",
            "温度",
            "风速",
            "风力",
            "云量",
            "湿度",
            "气压",
            "空气质量",
            "AQI",
            "能见度",
            "紫外线",
            "体感",
            "雨量",
            "降水量",
            "风向",
            "趋势",
        ]
    )


def _is_task_command(text: str) -> bool:
    return (
        "气象任务" in text
        or ("气象" in text and "任务" in text)
        or ("任务" in text and _task_action_from_text(text) is not None)
    )


def _is_power_briefing_command(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    if _is_power_briefing_expand_command(compact):
        return True
    has_briefing_name = (
        "电力气象决策晨报" in compact
        or "电力气象晨报" in compact
        or "晨报2.0" in compact
    )
    has_generate_action = any(
        keyword in compact
        for keyword in ("生成", "出一份", "出个", "预览", "查看", "给我", "测试")
    )
    concise_daily_request = bool(re.fullmatch(r"(?:今天|今日|明天)?(?:的)?(?:电力气象)?晨报(?:2\.0)?", compact))
    return has_briefing_name and (has_generate_action or concise_daily_request)


def _is_power_briefing_expand_command(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    return any(
        keyword in compact
        for keyword in (
            "展开全部分析区",
            "查看全部分析区",
            "展开全部市场",
            "查看全部市场",
            "晨报全部详情",
            "晨报完整明细",
        )
    )


def _task_action_from_text(text: str) -> str | None:
    if any(keyword in text for keyword in ("关闭", "结束", "终止", "截止任务")):
        return "close"
    if any(keyword in text for keyword in ("提醒", "催办", "催一下", "通知提交")):
        return "remind"
    if any(keyword in text for keyword in ("查询", "查看", "状态", "进度", "刚才的任务")):
        return "query"
    if any(keyword in text for keyword in ("发布", "创建", "新建", "发起", "生成")):
        return "create"
    return None


def _is_help_command(text: str) -> bool:
    return any(keyword in text for keyword in ["帮助", "你有什么作用", "你能做什么", "能做什么", "使用说明", "介绍一下"])


def _is_weather_knowledge_question(text: str) -> bool:
    return any(keyword in text for keyword in WEATHER_KNOWLEDGE_KEYWORDS)


def _request_from_text(text: str) -> ForecastRequest:
    target_date = _target_date_from_text(text)
    coordinates = _coordinates_from_text(text)
    if coordinates:
        latitude, longitude = coordinates
        return ForecastRequest(
            region=_coordinate_region(latitude, longitude),
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            days=_days_from_text(text),
            granularity="1h",
        )
    return ForecastRequest(
        region=_region_from_text(text),
        target_date=target_date,
        days=_days_from_text(text),
        granularity="1h",
    )


def _task_request_from_text(text: str) -> WeatherTaskRequest:
    coordinates = _coordinates_from_text(text)
    if coordinates:
        latitude, longitude = coordinates
        return WeatherTaskRequest(
            region=_coordinate_region(latitude, longitude),
            latitude=latitude,
            longitude=longitude,
            target_date=_target_date_from_text(text),
            days=_days_from_text(text),
        )
    return WeatherTaskRequest(
        region=_task_region_from_text(text) or _region_from_text(text),
        target_date=_target_date_from_text(text),
        days=_days_from_text(text),
    )


def _request_from_task(task: WeatherTask) -> ForecastRequest:
    return ForecastRequest(
        region=task.region,
        latitude=task.latitude,
        longitude=task.longitude,
        location_code=task.location_code,
        location_source=task.location_source,
        target_date=task.target_date,
        days=task.forecast_days,
        granularity="1h",
    )


def _target_date_from_text(text: str) -> str:
    return weather_dates.target_date_from_text(text)


def _date_span_status(text: str) -> str:
    """返回统一日期引擎的窗口状态: ok / truncated / past / beyond。"""
    return weather_dates.parse_date_span(text)[3]


# 有数量词的「一周/两周」不可能是"周四"这类星期几, 不做排除; 裸"周"(下周/本周)才需防"周X"
WEEK_COUNT_RE = re.compile(r"(?<![最上过前去后])([一两二三123])\s*个?\s*(?:周|星期|礼拜)")
WEEK_BARE_RE = re.compile(r"(?:下|这|本)\s*(?:周|星期|礼拜)(?![末一二三四五六日天年])")
_WEEK_COUNT_WORDS = {"一": 1, "1": 1, "两": 2, "二": 2, "2": 2, "三": 3, "3": 3}


def _days_from_text(text: str) -> int:
    return weather_dates.days_from_text(text)


def _normalize_day_count(token: str) -> int:
    if token.isdigit():
        value = int(token)
    else:
        value = DAY_COUNT_WORDS[token]
    return min(16, max(1, value))


def _needs_region_clarification(text: str) -> bool:
    return _coordinates_from_text(text) is None and _explicit_region_from_text(text) is None


def _needs_task_region_clarification(text: str) -> bool:
    return _coordinates_from_text(text) is None and _task_region_from_text(text) is None


def _region_clarification_text(days: int, command_type: str) -> str:
    day_text = f"{days}天" if days > 1 else "明天/指定日期"
    if command_type == "task":
        return (
            f"📋 点点收到啦~ 你想发布{day_text}的气象共测任务，不过还差一个地点。\n"
            "告诉我城市 / 区县 / 经纬度就行，例如：发布广州未来四天气象任务。"
        )
    return (
        f"⛅ 云云收到啦~ 你想看{day_text}的天气，不过还差一个地点。\n"
        "告诉我城市 / 区县 / 经纬度就行，例如：武汉未来三天天气，或 22.80,113.52 明天天气。"
    )


def _unsupported_weather_metric_text(metrics: list[str]) -> str:
    metric_text = "、".join(metrics)
    return (
        f"抱歉，你问的「{metric_text}」云云暂时还没接入哦。\n"
        "目前能查、能出图的气象要素有：🌡️ 温度、🌧️ 降水概率、💨 风速、☁️ 云量。\n"
        "你可以换这些要素再问我一次~"
    )


def _too_many_comparison_regions_text(regions: list[str]) -> str:
    return (
        f"你一次问了 {len(regions)} 个地区：{'、'.join(regions)}~\n"
        f"为了卡片清晰好读，云云一次最多对比 {MAX_COMPARISON_REGIONS} 个地区。\n"
        "先挑其中 2-4 个，或者分几次对比吧。"
    )


def _weather_knowledge_fallback(text: str) -> str:
    return (
        "可以，我直接解释，不需要城市模板卡片。\n"
        "- 数据来源：当前预测主要来自公开气象接口，并在结果中标注实际使用的数据源。\n"
        "- 更新时间：不同数据源更新时间不同，卡片里的“数据截止”表示本次预测使用数据的截止时间。\n"
        "- 预测不确定性：多日预报、局地短时降水、云量和风速变化通常不确定性更高；所以结果会给出风险提示和置信度说明。\n"
        "- 适用边界：这些结果适合社区共建、评分和复盘参考，不等同于官方预警或交易建议。"
    )


def _region_from_text(text: str) -> str:
    return _explicit_region_from_text(text) or DEFAULT_REGION


def _task_region_from_text(text: str) -> str | None:
    candidate = _explicit_region_from_text(text) or _task_bare_region_from_text(text)
    return _clean_task_region_candidate(candidate) if candidate else None


def _task_bare_region_from_text(text: str) -> str | None:
    search_text = _location_search_text(text)
    for match in TASK_BARE_REGION_RE.finditer(search_text):
        candidate = _clean_task_region_candidate(match.group(1))
        if candidate:
            return LOCATION_ALIAS_MAP.get(candidate, candidate)
    return None


def _clean_task_region_candidate(candidate: str) -> str | None:
    region = _clean_region_candidate(candidate)
    if not region:
        return None
    # “发布今日广州气象任务”中的“今日”是日期，不是地名的一部分。
    region = re.sub(r"^(?:今天|今日|明天|明日|后天|大后天)\s*", "", region)
    region = region.strip(" 的")
    if not region or region in TASK_BARE_REGION_BLOCKLIST:
        return None
    if any(keyword in region for keyword in ("气象", "天气", "任务")):
        return None
    return region


def _comparison_regions_from_text(text: str) -> list[str]:
    regions = _regions_from_text(text)
    if len(regions) < 2:
        return []
    if any(keyword in text for keyword in COMPARISON_QUERY_KEYWORDS):
        return regions
    if any(separator in text for separator in ("和", "与", "跟", "及", "、", "，", ",", "或", "还是", "vs", "VS", "对比")):
        return regions
    return []


def _regions_from_text(text: str) -> list[str]:
    search_text = _location_search_text(text)
    matches: list[tuple[int, int, str]] = []
    for alias in sorted(BUILTIN_LOCATIONS.keys(), key=len, reverse=True):
        for match in re.finditer(re.escape(alias), search_text):
            next_index = match.end()
            if next_index < len(search_text) and search_text[next_index] in PLACE_SUFFIX_CONTINUATION_CHARS:
                continue
            location = BUILTIN_LOCATIONS.get(alias)
            region = location.name if location else LOCATION_ALIAS_MAP.get(alias, alias)
            matches.append((match.start(), match.end(), region))

    for match in REGION_WITH_SUFFIX_RE.finditer(search_text):
        candidate = _clean_region_candidate(match.group(1))
        if candidate:
            matches.append((match.start(1), match.end(1), LOCATION_ALIAS_MAP.get(candidate, candidate)))

    selected: list[tuple[int, int, str]] = []
    for start, end, region in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0])):
        if any(not (end <= used_start or start >= used_end) for used_start, used_end, _region in selected):
            continue
        selected.append((start, end, region))

    regions = []
    seen = set()
    for _start, _end, region in sorted(selected, key=lambda item: item[0]):
        if region in seen:
            continue
        seen.add(region)
        regions.append(region)
    return regions


_PROVINCE_ONLY_RE = re.compile(
    r"^(河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|广西|海南|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|内蒙古|台湾)(省|自治区|壮族自治区|回族自治区|维吾尔自治区)?$"
)
_PROVINCE_STOP_AFTER = set("的省市天气未来最近近期接下这几周月日晴雨雪风云温度气象预报预测报怎么样如何啥吗呢和与跟今明后大过去上历史")


def _is_province_only_region(region) -> bool:
    return bool(region and _PROVINCE_ONLY_RE.match(str(region)))


def _has_extra_place_after_province(text: str, province: str) -> bool:
    idx = text.find(province)
    if idx < 0:
        return False
    after = text[idx + len(province):]
    for prefix in ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省"):
        if after.startswith(prefix):
            after = after[len(prefix):]
            break
    if not after:
        return False
    if starts_with_province_scope_modifier(after):
        return False
    ch = after[0]
    return bool(re.match(r"[一-鿿]", ch)) and ch not in _PROVINCE_STOP_AFTER


def _explicit_region_from_text(text: str) -> str | None:
    search_text = _location_search_text(text)
    # 带行政后缀(区/县/市/新区)的候选, 比裸 builtin 键更具体
    suffixed_region = _suffixed_region_from_text(search_text)
    builtin_region = None
    for region in sorted(BUILTIN_LOCATIONS.keys(), key=len, reverse=True):
        if _region_matches_text(search_text, region):
            builtin_region = region
            break

    # 后缀候选更长(更具体)时优先, 修复"上海浦东新区"命中 builtin 键"上海"就整市返回、区县被吞
    if suffixed_region and (builtin_region is None or len(suffixed_region) > len(builtin_region)):
        return LOCATION_ALIAS_MAP.get(suffixed_region, suffixed_region)
    if builtin_region:
        # “辽宁盘锦”既包含省名也包含已知地级市。省名先命中时继续检查更具体的
        # 城市别名，避免退回省级代表点或依赖 LLM 才能纠正。
        if _is_province_only_region(builtin_region) and _has_extra_place_after_province(search_text, builtin_region):
            province_canonical = LOCATION_ALIAS_MAP.get(builtin_region, builtin_region)
            province_prefix = re.sub(r"(?:维吾尔自治区|壮族自治区|回族自治区|自治区|省)$", "", province_canonical)
            for alias, normalized in sorted(LOCATION_ALIASES, key=lambda item: len(item[0]), reverse=True):
                if _is_province_only_region(alias) or alias not in search_text:
                    continue
                if str(normalized).startswith(province_prefix):
                    return normalized
        return builtin_region
    if suffixed_region:
        return LOCATION_ALIAS_MAP.get(suffixed_region, suffixed_region)

    for alias, normalized in sorted(LOCATION_ALIASES, key=lambda item: len(item[0]), reverse=True):
        if alias in search_text:
            return normalized
    return None


def _location_search_text(text: str) -> str:
    search_text = text.replace("@", " ")
    for alias in WEATHER_BOT_ALIASES + TASK_BOT_ALIASES:
        search_text = search_text.replace(alias, " ")
    return search_text


def _region_matches_text(text: str, region: str) -> bool:
    for match in re.finditer(re.escape(region), text):
        next_index = match.end()
        if next_index < len(text) and text[next_index] in PLACE_SUFFIX_CONTINUATION_CHARS:
            continue
        return True
    return False


def _suffixed_region_from_text(text: str) -> str | None:
    for match in REGION_WITH_SUFFIX_RE.finditer(text):
        candidate = _clean_region_candidate(match.group(1))
        if candidate:
            return candidate
    return None


# 宏观大区不是可解析城市, 命中改走澄清而非硬 geocode(否则错点或抛错→通用报错)
_MACRO_REGIONS = frozenset({
    "华南", "华北", "华东", "华中", "西南", "西北", "东北", "华南地区", "华北地区", "华东地区",
    "华中地区", "西南地区", "西北地区", "东北地区", "江浙沪", "长三角", "珠三角", "京津冀", "东部",
    "西部", "南方", "北方", "全国",
})
# 指代短语不是地名(如"刚才那个城市"), 别当城市 geocode; 让其落到带 history 的对话 LLM 去解析指代
_REFERENCE_WORDS = ("那个", "这个", "刚才", "刚说", "上面", "前面", "同一个", "上述", "那边", "那里", "这里")


def _clean_region_candidate(candidate: str) -> str | None:
    region = candidate.strip(" ，,。；;：:")
    changed = True
    while changed:
        changed = False
        for prefix in sorted(REGION_QUERY_PREFIXES, key=len, reverse=True):
            if region.startswith(prefix):
                region = region[len(prefix) :].strip(" ，,。；;：:")
                changed = True
    if not region:
        return None
    if region in _MACRO_REGIONS:
        return None
    if any(word in region for word in _REFERENCE_WORDS):
        return None
    # 日期碎片/"各地区"等泛指被"地区"后缀误抓成地名(如"月下旬各地区"), 剔除
    if any(frag in region for frag in ("上旬", "中旬", "下旬", "各地", "各区", "各市", "各县", "各省")):
        return None
    interpreted = interpret_region_scope(region)
    if interpreted.scope is not None:
        return interpreted.entity
    return region


def _location_candidate_supported_by_text(candidate: str, text: str) -> bool:
    """Reject LLM-only locations that have no specific place token in the user text."""

    compact_candidate = re.sub(r"\s+", "", candidate)
    compact_text = re.sub(r"\s+", "", text)
    if compact_candidate and compact_candidate in compact_text:
        return True
    province_removed = False
    for alias, normalized in sorted(LOCATION_ALIASES, key=lambda item: len(item[0]), reverse=True):
        if not _is_province_only_region(normalized):
            continue
        for prefix in (normalized, alias):
            if compact_candidate.startswith(prefix):
                compact_candidate = compact_candidate[len(prefix) :]
                province_removed = True
                break
        if province_removed:
            break
    core = re.sub(r"(特别行政区|自治区|自治州|地区|新区|省|市|县|区|盟|州)+$", "", compact_candidate)
    return len(core) >= 2 and core in compact_text


def _coordinates_from_text(text: str) -> tuple[float, float] | None:
    import re

    pair_match = re.search(r"(-?\d{1,2}(?:\.\d+)?)\s*[,，]\s*(-?\d{1,3}(?:\.\d+)?)", text)
    if pair_match:
        return _validated_coordinates(float(pair_match.group(1)), float(pair_match.group(2)))

    named_match = re.search(r"北纬\s*(\d{1,2}(?:\.\d+)?)\D+东经\s*(\d{1,3}(?:\.\d+)?)", text)
    if named_match:
        return _validated_coordinates(float(named_match.group(1)), float(named_match.group(2)))

    return None


def _validated_coordinates(latitude: float, longitude: float) -> tuple[float, float] | None:
    if -90 <= latitude <= 90 and -180 <= longitude <= 180:
        return latitude, longitude
    return None


def _coordinate_region(latitude: float, longitude: float) -> str:
    return f"经纬度 {latitude:.4f},{longitude:.4f}"


def _public_weather_report_url(settings: Settings, request: ForecastRequest, metrics: list[str] | None = None) -> str | None:
    if not settings.public_base_url:
        return None
    query_params = _weather_url_query(request)
    metrics_value = weather_metrics_query_value(metrics)
    if metrics_value:
        query_params["metrics"] = metrics_value
    query = urlencode(query_params)
    return f"{settings.public_base_url.rstrip('/')}/reports/weather?{query}"


def _public_weather_download_url(settings: Settings, request: ForecastRequest) -> str | None:
    if not settings.public_base_url:
        return None
    query = urlencode(_weather_url_query(request))
    return f"{settings.public_base_url.rstrip('/')}/api/weather/export?{query}"


def _public_weather_json_url(settings: Settings, request: ForecastRequest) -> str | None:
    if not settings.public_base_url:
        return None
    query = urlencode(_weather_url_query(request))
    return f"{settings.public_base_url.rstrip('/')}/api/weather/export/json?{query}"


def _public_weather_comparison_report_url(
    settings: Settings,
    regions: list[str],
    target_date: str,
    days: int,
    metrics: list[str] | None = None,
) -> str | None:
    if not settings.public_base_url:
        return None
    query_params: dict[str, Any] = {
        "regions": _comparison_regions_query_value(regions),
        "target_date": target_date,
        "days": days,
    }
    metrics_value = weather_metrics_query_value(metrics)
    if metrics_value:
        query_params["metrics"] = metrics_value
    query = urlencode(query_params)
    return f"{settings.public_base_url.rstrip('/')}/reports/weather/compare?{query}"


def _public_weather_comparison_download_url(settings: Settings, regions: list[str], target_date: str, days: int) -> str | None:
    if not settings.public_base_url:
        return None
    query = urlencode({"regions": _comparison_regions_query_value(regions), "target_date": target_date, "days": days})
    return f"{settings.public_base_url.rstrip('/')}/api/weather/compare/export?{query}"


def _public_weather_comparison_json_url(settings: Settings, regions: list[str], target_date: str, days: int) -> str | None:
    if not settings.public_base_url:
        return None
    query = urlencode({"regions": _comparison_regions_query_value(regions), "target_date": target_date, "days": days})
    return f"{settings.public_base_url.rstrip('/')}/api/weather/compare/export/json?{query}"


def _comparison_regions_query_value(regions: list[str]) -> str:
    return ",".join(region.strip() for region in regions if region.strip())


def _comparison_regions_query_to_list(regions: str) -> list[str]:
    normalized_regions = []
    for region in re.split(r"[,，、/]+", regions):
        region = region.strip()
        if not region:
            continue
        parsed_regions = _regions_from_text(region)
        normalized_regions.append(parsed_regions[0] if parsed_regions else LOCATION_ALIAS_MAP.get(region, region))
    return list(dict.fromkeys(normalized_regions))[:MAX_COMPARISON_REGIONS]


def _weather_url_query(request: ForecastRequest) -> dict[str, Any]:
    query: dict[str, Any] = {"region": request.region, "target_date": request.target_date, "days": request.days}
    if request.latitude is not None:
        query["latitude"] = request.latitude
    if request.longitude is not None:
        query["longitude"] = request.longitude
    if request.location_code:
        query["location_code"] = request.location_code
    if request.location_source:
        query["location_source"] = request.location_source
    return query


def _forecast_report_cache_key(request: ForecastRequest) -> str:
    return json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


async def _resolve_task_location(location_resolver: LocationResolver, request: WeatherTaskRequest):
    return await location_resolver.resolve(
        ForecastRequest(
            region=request.region,
            latitude=request.latitude,
            longitude=request.longitude,
            location_code=request.location_code,
            location_source=request.location_source,
            target_date=request.target_date,
            granularity="1h",
        )
    )


def _help_text(allowed_bot: str = FEISHU_LEGACY_BOT) -> str:
    weather_help = [
        "⛅ 大家好，我是云云，PowerPals 的气象预测小助手~",
        "负责全国城市 / 区县 / 经纬度的天气预测，发布共测任务请找「点点」。",
        "",
        "🌦️ **我能帮你**",
        "1. **查天气** — 城市、区县、经纬度，今天或指定某天都行",
        "2. **看趋势** — 未来 3 天 / 7 天 温度、降水、风力变化",
        "3. **逐小时** — 小时级变化，适合排出行、看电力负荷",
        "4. **风险解读** — 降雨 / 降温 / 大风的时段和影响范围",
        "5. **数据源** — 预报来源、口径和不确定性说明",
        "6. **生成晨报** — 全国 31 个省级地区、33 个分析区、75 个代表点的今日 + 明日扫描",
        "",
        "💬 **你可以这样问我**",
        "• @云云 广州明天天气",
        "• @云云 武汉未来三天天气",
        "• @云云 北京气象预测 2026-06-10",
        "• @云云 22.8016,113.5252 明天天气",
        "• @云云 生成今天的电力气象决策晨报 2.0",
        "• @云云 展开全部分析区",
    ]
    task_help = [
        "📋 大家好，我是点点，PowerPals 的气象任务小助手~",
        "负责气象共测任务的发布、提醒、关闭和记录，天气预测请找「云云」。",
        "",
        "🗂️ **我能帮你**",
        "1. **发布任务** — 指定城市 / 坐标，建立气象共测任务",
        "2. **提醒 / 关闭** — 任务进度提醒、到点自动关闭",
        "3. **记录归档** — 任务结果写入多维表格留痕",
        "",
        "💬 **你可以这样用我**",
        "• @点点 今日广州气象任务",
        "• @点点 22.8016,113.5252 今日气象任务",
        "• @点点 帮助",
    ]
    if allowed_bot == FEISHU_WEATHER_BOT:
        return "\n".join(weather_help)
    if allowed_bot == FEISHU_TASK_BOT:
        return "\n".join(task_help)
    return "\n".join([*weather_help, "", *task_help])


app = create_app()
