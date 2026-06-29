# PowerPals 全国气象机器人与任务发布机器人

PowerPals 是小可爱电力社区面向电力行业 AI Bot 共建、共测、评分、复盘的开源示范项目。本仓库当前提供一套可运行的 **全国气象预测机器人 + 气象任务发布机器人 + 气象数据工作台**，并提供一个最小可用的气象裁判评分接口。

项目目标不是给出交易或报价建议，而是跑通社区可复用的共测闭环：

```text
任务发布 -> Bot 预测 -> 标准 JSON 提交 -> 飞书卡片展示 -> 多维表格留痕 -> 后续评分与复盘
```

社区口径：

```text
共建是宗旨，共测是机制，评分是工具，复盘是方法，成长是结果。
```

## 两个主机器人

| 机器人 | 作用 | 典型输入 | 典型输出 |
|---|---|---|---|
| 全国气象预测机器人 | 查询城市、地区或经纬度对应的逐小时气象预测，只负责预测和解释 | `广州明天天气`、`22.8016,113.5252 明天天气` | `weather_submission_v1` JSON、飞书预测卡片，响应中标记 `bot_role=weather_forecast_bot` |
| 气象任务发布机器人 | 发布共测任务、统一提交口径、提醒提交、关闭窗口、记录状态，不计算天气 | `今日广州气象任务`、`22.8016,113.5252 今日气象任务` | 任务卡片、任务记录、后续评分输入，响应中标记 `bot_role=weather_task_bot` |

生产部署建议使用两个独立飞书机器人 App，并把回调入口分开：

| 飞书机器人 | 回调入口 | 允许能力 |
|---|---|---|
| 全国气象预测机器人 | `/feishu/events/weather` | 天气预测、多日预测、网页报告和导出链接 |
| 气象任务发布机器人 | `/feishu/events/task` | 任务发布、提醒、关闭和任务留痕 |

旧入口 `/feishu/events` 仍保留兼容单机器人模式，会继续按“任务优先、预测其次”的规则处理，但不建议用于多机器人隔离部署。

任务机器人本身不计算天气。它负责“组织比赛/共测流程”：告诉大家测哪里、测哪天、什么时候截止、用什么格式提交，并把任务状态写入飞书多维表格或本地 JSONL。

裁判目前不是第三个完整业务机器人，而是一个最小评分工具：输入标准预测 JSON 和实况摘要，输出温度误差、降水命中、风速误差和综合分。后续可以扩展为独立裁判 Bot、榜单和复盘报告。

## 全国位置支持

当前支持三类位置输入：

```json
{ "region": "广州", "target_date": "2026-06-10" }
```

```json
{ "region": "北京市", "target_date": "2026-06-10" }
```

```json
{
  "region": "广州南沙",
  "latitude": 22.8016,
  "longitude": 113.5252,
  "target_date": "2026-06-10"
}
```

如果不传位置，默认使用 `广东省深圳市`，兼容旧命令。

位置解析顺序：

1. 显式经纬度优先。
2. 内置常用城市表优先解析深圳、广州、北京、上海等城市。
3. 配置 `QWEATHER_API_KEY` 后，可用和风天气 GeoAPI 做全国城市解析。
4. 无和风 Key 时，尝试 Open-Meteo Geocoding 兜底。

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务健康检查 |
| `POST` | `/api/weather/forecast` | 生成单日标准气象预测提交 |
| `POST` | `/api/weather/forecast/range` | 生成最多 16 天标准气象预测提交 |
| `POST` | `/api/weather/batch` | 批量生成多个城市/经纬度预测 |
| `POST`/`GET` | `/api/weather/export` | 导出 Excel 可直接打开的 CSV |
| `POST`/`GET` | `/api/weather/export/json` | 导出标准 `weather_submission_v1` JSON |
| `GET` | `/reports/weather` | 飞书群可打开的网页报告 |
| `POST` | `/api/weather/submission` | 记录外部 Bot 的标准提交 |
| `POST` | `/api/weather/publish` | 生成预测、发布预测卡片并记录 |
| `POST` | `/api/judge/weather/score` | 用实况摘要对单条气象预测做基础评分 |
| `GET`/`POST`/`DELETE` | `/api/locations` | 地址收藏，支持别名和经纬度 |
| `GET`/`POST` | `/api/news/*` | 电力资讯摘要的本地聚合入口 |
| `GET`/`POST` | `/api/hydrology/*` | 水情记录和导出入口 |
| `GET` | `/api/data/export/catalog` | 数据导出中心目录 |
| `POST` | `/api/tasks/weather/create` | 生成任务草稿 |
| `POST` | `/api/tasks/weather/publish` | 发布任务卡片并记录任务 |
| `POST` | `/api/tasks/weather/remind` | 发布提交提醒并记录任务状态 |
| `POST` | `/api/tasks/weather/close` | 关闭提交窗口，进入等待实况评分状态 |
| `GET` | `/api/tasks/weather/{task_id}` | 按任务 ID 查询任务 |
| `POST` | `/feishu/events` | 旧版单机器人飞书事件回调入口 |
| `POST` | `/feishu/events/weather` | 气象预测机器人飞书事件回调入口，只处理预测能力 |
| `POST` | `/feishu/events/task` | 气象任务发布机器人飞书事件回调入口，只处理任务能力 |

## 快速启动

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
.\.venv\Scripts\uvicorn services.weather_bot.main:app --reload
```

健康检查：

```text
http://127.0.0.1:8000/health
```

查询广州单日预测：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/weather/forecast `
  -ContentType "application/json" `
  -Body '{"region":"广州","target_date":"2026-06-10","providers":["open_meteo","qweather","caiyun"]}'
```

查询广州未来三天：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/weather/forecast/range `
  -ContentType "application/json" `
  -Body '{"region":"广州","target_date":"2026-06-10","days":3,"providers":["open_meteo"]}'
```

查询广州未来 16 天：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/weather/forecast/range `
  -ContentType "application/json" `
  -Body '{"region":"广州","target_date":"2026-06-10","days":16,"providers":["open_meteo"]}'
```

如果部分日期超出数据源可用窗口，接口会返回 `status=partial`，已成功日期仍在 `submissions`，失败日期会进入 `errors`。

按经纬度发布任务：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/tasks/weather/publish `
  -ContentType "application/json" `
  -Body '{"region":"广州南沙","latitude":22.8016,"longitude":113.5252,"target_date":"2026-06-10"}'
```

导出气象 CSV：

```powershell
Invoke-WebRequest -Method Post http://127.0.0.1:8000/api/weather/export `
  -ContentType "application/json" `
  -Body '{"region":"广州","target_date":"2026-06-10","days":7}' `
  -OutFile weather.csv
```

导出标准 JSON：

```text
http://127.0.0.1:8000/api/weather/export/json?region=广州&target_date=2026-06-10&days=7
```

打开网页报告：

```text
http://127.0.0.1:8000/reports/weather?region=广州&target_date=2026-06-10&days=7
```

收藏地址：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/locations `
  -ContentType "application/json" `
  -Body '{"alias":"南沙基地","name":"广州南沙","latitude":22.8016,"longitude":113.5252}'
```

最小裁判评分：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/judge/weather/score `
  -ContentType "application/json" `
  -Body '{"submission":{...},"truth":{"max_temperature":31.0,"min_temperature":26.0,"rain_observed":false,"wind_speed":3.0}}'
```

## 任务 ID

全国通用格式：

```text
WEATHER-CN-<location-token>-YYYYMMDD-DAYAHEAD-001
```

示例：

```text
WEATHER-CN-440300-20260610-DAYAHEAD-001
WEATHER-CN-440100-20260610-DAYAHEAD-001
WEATHER-CN-COORD-22_8016-113_5252-20260610-DAYAHEAD-001
```

城市优先使用行政区划/位置编码；经纬度任务使用坐标 token。

## 飞书命令

支持示例：

```text
@机器人 明天深圳天气
@机器人 广州明天天气
@机器人 北京气象预测 2026-06-10
@机器人 广州未来三天天气
@机器人 22.8016,113.5252 明天天气
@机器人 今日广州气象任务
@机器人 22.8016,113.5252 今日气象任务
@机器人 发布北京气象任务 2026-06-10
@机器人 帮助
```

如果使用旧入口 `/feishu/events`，一句话同时包含“任务”和“天气预测”时，系统仍优先按任务命令处理。使用新入口时，`/feishu/events/weather` 只处理预测类命令，遇到任务命令会返回 `status=redirect` 并提示找气象任务发布机器人；`/feishu/events/task` 只处理任务类命令，遇到天气预测命令同样返回 `status=redirect` 并提示找全国气象预测机器人。当前响应会通过 `bot_role` 明确返回是预测机器人还是任务机器人处理。配置 `PUBLIC_BASE_URL` 后，预测卡片会带 **卡片内趋势图表**、**打开网页报告**、**下载CSV** 和 **下载JSON** 按钮；网页报告按钮使用飞书 AppLink 在飞书端内打开，适合直接在群里转发。

## 气象数据工作台

工作台吸收了电力资讯插件类工具里最适合 PowerPals 的能力，但保持开源和可审计：

- 气象预测最多 16 天。
- 支持城市、地区、经纬度、收藏地址别名。
- 支持批量预测。
- 支持 CSV 下载，Excel 可直接打开。
- 支持飞书卡片内展示温度趋势和降水概率图表。
- 支持飞书群点击打开的网页报告，报告页内含 SVG 曲线和 CSV/JSON 下载按钮。
- 支持本地电力资讯摘要记录，不抓取未授权公众号正文。
- 支持水情记录和 CSV 导出。
- 所有本地留痕默认写入 `data/`，密钥不进入仓库。

## 标准提交格式

`/api/weather/forecast` 返回 `weather_submission_v1`。关键字段：

```text
submission_type
task_id
track
bot
scope
scope.location
time_info
data_profile
payload
confidence
explanation
scoring_profile
disclaimer
```

示例与 Schema：

```text
examples/weather_submission_shenzhen.json
schemas/weather_submission_v1.schema.json
```

## 最小裁判评分

`/api/judge/weather/score` 用于后续裁判 Bot 的第一步，不依赖大模型，不生成榜单。输入：

```text
submission: weather_submission_v1
truth: max_temperature, min_temperature, rain_observed, wind_speed
```

输出包含：

```text
judge_bot_id
scoring_version
metrics
component_scores
total_score
summary
```

当前规则：温度分占 45%，降水命中占 35%，风速误差占 20%。降水以 `rain_probability >= 50%` 作为是否预测有雨的阈值。该接口只用于共测评分和复盘，不构成气象业务认证。

## 定时节奏

默认节奏：

| 时间 | 动作 |
|---|---|
| D-1 09:00 | 发布任务 |
| D-1 16:00 | 数据截止 |
| D-1 16:30 | 提交提醒 |
| D-1 17:00 | 发布官方预测 |
| D-1 17:05 | 关闭任务 |
| D 00:00-23:00 | 预测窗口 |

当前 scheduler 会读取 `.env` 中的 `DEFAULT_WEATHER_REGION`、`DEFAULT_WEATHER_LATITUDE`、`DEFAULT_WEATHER_LONGITUDE`。不配置时默认使用深圳，配置城市名或经纬度后，可自动发布对应地区的任务和官方预测。后续如果要做“每天多个城市自动任务”，建议增加任务配置表，由 scheduler 读取城市列表和日期策略。

## 飞书多维表格

建议建立两张表：

| 表 | 用途 | 环境变量 |
|---|---|---|
| 预测提交表 | 记录每次气象预测 JSON 和卡片消息 ID | `FEISHU_BITABLE_TABLE_ID` |
| 任务发布表 | 记录任务发布、提醒、关闭状态 | `FEISHU_TASK_BITABLE_TABLE_ID` |

任务表建议字段包含：

```text
task_id
track
region
location_code
latitude
longitude
location_source
target_date
forecast_start
forecast_end
publish_time
data_cutoff_time
submission_deadline
status
task_card_message_id
submission_format_version
scoring_status
notes
```

## 环境变量

复制 `.env.example` 为 `.env` 后按需填写：

```text
QWEATHER_API_KEY=
CAIYUN_API_KEY=
OPENCLAW_API_URL=
OPENCLAW_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
FEISHU_DEFAULT_CHAT_ID=
FEISHU_WEATHER_APP_ID=
FEISHU_WEATHER_APP_SECRET=
FEISHU_WEATHER_VERIFICATION_TOKEN=
FEISHU_WEATHER_DEFAULT_CHAT_ID=
FEISHU_TASK_APP_ID=
FEISHU_TASK_APP_SECRET=
FEISHU_TASK_VERIFICATION_TOKEN=
FEISHU_TASK_DEFAULT_CHAT_ID=
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
FEISHU_TASK_BITABLE_TABLE_ID=
LOCAL_JSONL_PATH=data/weather_submissions.jsonl
LOCAL_TASK_JSONL_PATH=data/weather_tasks.jsonl
LOCAL_LOCATIONS_PATH=data/locations.json
LOCAL_NEWS_JSONL_PATH=data/news_items.jsonl
LOCAL_HYDROLOGY_JSONL_PATH=data/hydrology_records.jsonl
PUBLIC_BASE_URL=
```

## Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Compose 包含两个服务：

| 服务 | 说明 |
|---|---|
| `weather-bot` | FastAPI、飞书事件回调、手动接口 |
| `weather-scheduler` | 按社区节奏自动发布任务、提醒、发布预测、关闭窗口 |

## 测试

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m compileall services tests
docker compose config
docker compose build
```

仓库包含 GitHub Actions CI，push 或 PR 时会自动运行测试、编译检查、Schema 示例校验、`docker compose config` 和 Docker 构建。

## 合规边界

本项目输出仅用于小可爱电力社区共建、评分和复盘，不构成交易建议、报价建议、投资建议、收益承诺或商业认证。
