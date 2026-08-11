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
3. 只有和风 Geo、Nominatim 或 Open-Meteo Geocoding 的精确 endpoint 与生产 `SourcePolicy` 同时通过许可、用途、署名和留存审查时，才按配置顺序调用外部解析。
4. 仅有 API Key、服务名称或可访问 URL 不会触发外部请求；所有外部定位来源都未获准时返回澄清/不可用，不静默联网或猜坐标。

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

### 管理写接口安全

所有会写入本地业务数据或主动发布内容的 `/api/*` 管理接口都必须携带
`Authorization: Bearer <ADMIN_API_TOKEN>`。未配置 Token、未携带凭证或凭证错误时均默认拒绝，
且不会调用预测、飞书或本地写入逻辑。天气查询、报告读取和飞书事件回调不使用这个管理凭证。
生产和预发布环境还必须把凭证绑定到明确的 `ADMIN_API_ACTOR_ID`，并在
`ADMIN_API_ROLES_JSON` 中授予 `administrator`；只有 Token 而没有身份和角色时仍会拒绝。

即使鉴权成功，`/api/weather/publish`、`/api/tasks/weather/publish` 和
`/api/tasks/weather/remind` 也只有在 `GLOBAL_FEISHU_SEND_ENABLED=true`、
`ADMIN_API_SEND_ENABLED=true`、目标 `chat_id` 位于 `ADMIN_API_SEND_TARGETS_JSON`
且 `DRY_RUN=false` 时才允许发送飞书卡片。管理 API 的开关和白名单独立于 09:00
晨报，避免开启晨报时连带放开其他发布入口；任一条件不满足时只生成预览并在响应的
`delivery.reason` 中给出拒绝原因。允许产生外部副作用时还必须携带 `Idempotency-Key`；
相同键和相同请求只复用首次结果，不会重复发送或写入，相同键配不同请求则拒绝。
审计库仅保存键/请求哈希、actor、role、动作、状态和时间，不保存 Token 或原始请求正文，
并按 90 天窗口清理。生产环境不应把 Token 或目标 ID 写入日志或提交到仓库。

飞书用户主动发起的合法问答回复与主动群发使用不同开关：
`FEISHU_PASSIVE_REPLY_ENABLED=false` 可用于“只计算、不回复”的影子阶段；`DRY_RUN=true`
拥有更高优先级，会同时阻止被动回复、进度消息和任务 Bitable/JSONL 写入。正常内部问答阶段
可只开启被动回复而继续保持 `GLOBAL_FEISHU_SEND_ENABLED=false`，因此不会连带开启 09:00 晨报、
告警或管理 API 群发。

### 外部数据事实边界

本项目不拥有、也不在本地提供气象原始数据，以及负荷、出力、机组、联络线、价格、持仓或用户资产事实。业务事实只能来自许可和用途已经人工审核的第三方 API、官方公开数据或可回溯到原始发布方的公开材料；本地数据库仅保存必要的有限期缓存、不可变版本与来源元数据、派生结果和审计/回放记录，不是自有业务数据仓库。

- `SourceRegistry` 按运行环境、provider 和 URL 前缀匹配人工审核的来源策略。`WEATHER_SOURCE_POLICIES_JSON=[]` 是生产默认值，表示所有第三方事实均拒绝进入计算；存在 API Key 不等于取得生产使用许可。
- `DataAvailabilityGate` 继续检查来源、真实抓取时间、有效时间、单位、粒度、覆盖范围、时区、新鲜度、完整率、质量和内容哈希。缺失、过期、来源不匹配或许可不允许时必须返回“数据不可用”，不得用另一个来源、大模型记忆或历史缓存静默补值。
- 搜索服务只用于发现官方原文入口。搜索摘要、转载内容和模型总结不能成为结构化数值、预警、负荷、出力、机组或价格真值；无法定位原始来源时只可作为待核查线索。
- 和风天气当前官方预警适配器使用经纬度接口 `/weatheralert/v1/current/{latitude}/{longitude}`。只有 endpoint、许可、文字引用用途、署名和元数据策略全部匹配时才返回最小化的预警标题、原始发布机构、发布时间、有效/失效时间、来源标记和署名；不持久化第三方预警正文或处置说明。
- 第三方原始响应不支持长期落库。版本与回放只保存许可允许的最小元数据和派生结果，并遵守保留期。

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
  -Headers @{ Authorization = "Bearer $env:ADMIN_API_TOKEN" } `
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

## 受控持续学习 2.0

云云支持离线、可审计的“受控持续学习”，但不会自行修改代码、配置或数据源权重，学习任务本身也没有飞书发送能力。学习输入来自自动生成的解析案例、管理员补充的结构化案例，以及在独立来源策略获准后执行的到期参考天气评分。当前实现可适配 Open-Meteo 历史格点/再分析数据（不等同于官方气象站实况），但生产默认不启用；缺少 `open_meteo_archive_truth` 的审核策略时为 0 次外部请求，也不宣称已完成真实评分。

本地运行完整周期（回放 + 到期实况评分 + 候选报告）：

```powershell
python -m services.weather_bot.controlled_learning_cli run
```

不访问外部实况、只验证本地解析链路：

```powershell
python -m services.weather_bot.controlled_learning_cli run --skip-truth --strict
```

管理员案例使用 `ReplayCase` JSON，通过 CLI 加入；案例不会从普通飞书聊天自动收集：

```powershell
python -m services.weather_bot.controlled_learning_cli add-case --file .\my-replay-case.json
```

查看候选及记录人工决定：

```powershell
python -m services.weather_bot.controlled_learning_cli candidates --status pending
python -m services.weather_bot.controlled_learning_cli decide cand-xxxxxxxxxxxxxxxx --status approved --actor admin --reason "已完成人工复核"
```

`approved` 只表示审计状态，不会自动应用。真正变更仍需独立修改、全量测试、提交和部署。周期报告默认写入 `data/controlled_learning/reports/`，生产定时任务模板见 `deploy/controlled_learning.cron`。

如需把运行进度同步到专用测试群，使用独立发布器；它只读取最新报告并发送计数摘要，不包含原始消息、失败案例正文、候选载荷或群 ID。群名必须精确且唯一匹配，绝不回退到默认群；同一学习 run 只允许发送一次。发布仍同时受 `DRY_RUN`、`GLOBAL_FEISHU_SEND_ENABLED` 和 `CONTROLLED_LEARNING_REPORT_SEND_ENABLED` 控制，默认零发送：

```powershell
# 只读核验目标；不要求发送开关开启，也不会发送
python -m services.weather_bot.controlled_learning_report_cli --check-target

# 发布两小时内生成的 latest.json；所有发送开关需显式开启
python -m services.weather_bot.controlled_learning_report_cli
```

独立定时模板见 `deploy/controlled_learning_report.cron`。部署前必须先执行 `--check-target`，确认 `test` 返回 `unique_exact_target`；不存在或同名群超过一个时保持阻断。

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

旧版社区节奏 scheduler 只有在 `LEGACY_WEATHER_SCHEDULER_ENABLED=true` 时才会运行，并读取 `.env` 中的 `DEFAULT_WEATHER_REGION`、`DEFAULT_WEATHER_LATITUDE`、`DEFAULT_WEATHER_LONGITUDE`；该开关默认关闭，不能因开启晨报的全局发送开关而被连带启用。不配置地点时默认使用深圳。后续如果要做“每天多个城市自动任务”，建议增加独立审核的任务配置表、发送开关和目标白名单。

电力气象交易晨报 3.1 使用独立入口 `scripts/daily_power_briefing.py`，生产定时配置保存在 `deploy/power_briefing.cron`。全国版本覆盖 31 个省级地区、33 个电力气象分析区和 75 个代表城市。09:00 完整晨报的范围固定为“今日 09:00–24:00 + 明日 00:00–24:00”，逐一检查夜间、早峰、午间光伏、下午过渡、晚峰和深夜窗口；已过去的时段不会写成未来风险。今日变化只能与昨日 09:00 对今日同地区、同窗口、同代理指标和同方法版本的快照比较，明日首次出现则明确写“首次观察”，不再使用含糊的“较上一版”。

定时链保留四个北京时间节点：08:50 `precompute` 生成 09:00 不可变快照，09:00 `send` 只发送该快照；14:50 `afternoon_precompute` 必须先读到同日 09:00 快照，15:00 `afternoon_send` 只在风险新增、升级、减弱、解除、同一事件时段移动或可信度改变时发送差异卡，无实质变化则静默。上午与下午分别由 `POWER_BRIEFING_ALLOW_SEND` 和 `POWER_BRIEFING_AFTERNOON_ALLOW_SEND` 授权；每次发送还必须满足 `GLOBAL_FEISHU_SEND_ENABLED=true`、目标 `chat_id` 位于人工审核的 `POWER_BRIEFING_TARGETS_JSON`，且 `DRY_RUN=false`。任一条件不满足都不发送，也不会占用发送幂等键。cron 只选择运行模式，不覆盖计划开关。

09:00/15:00 发送均只读取对应时次、带 `forecast_run_id` 的新鲜快照；缺失、过期、时次错误或身份不一致时以 `precompute_snapshot_missing` 失败关闭，不现场抓取和生成。每个“日期化发布时次 + 目标 + 快照 + run_id”使用 SQLite 持久化发送账本和稳定飞书 UUID；重复或并发运行最多产生一次外部消息，明确失败或过期租约使用同一 UUID 安全重试，账本默认清理 90 天前记录。代码中不保存群 ID。快照默认保留 24 小时；用户在群内真实 @云云手动生成晨报后，可回复该机器人消息并发送“展开全部分析区”读取同一快照，不会重复抓取。正常数据只显示一行更新时间、覆盖和可信度；缺失、降级、低可信或主动展开时才显示运行 ID、起报/有效时间、来源链接和内容指纹。代表城市不足时明确写“只按已有城市展示，不外推整个分析区”，单城市分析区不参与全区排行。

订阅本身不是发送授权：私聊必须明确回复“确认订阅”，群聊还必须由配置的审核管理员在同一群、同一线程二次明确确认，“可以”“好的”等模糊回复不会激活。订阅状态即使成为 `ACTIVE` 也不能绕过全局发送开关、告警发送开关、目标范围和 `DRY_RUN`；当前生产应保持 `ALERT_SEND_ENABLED=false`，直至主动通知发送链、来源许可和目标白名单全部独立验收。

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
APP_ENV=production
ADMIN_API_TOKEN=
ADMIN_API_ACTOR_ID=
ADMIN_API_ROLES_JSON=[]
ADMIN_API_SEND_ENABLED=false
ADMIN_API_SEND_TARGETS_JSON=[]
ADMIN_API_AUDIT_DB=data/admin_api_audit.db
ADMIN_API_IDEMPOTENCY_REQUIRED=true
GLOBAL_FEISHU_SEND_ENABLED=false
DRY_RUN=true
QWEATHER_API_KEY=
QWEATHER_API_HOST=
CAIYUN_API_KEY=
OPENCLAW_API_URL=
OPENCLAW_API_KEY=
OPENCLAW_EGRESS_ENABLED=false
OPENCLAW_ALLOWED_HTTPS_PREFIXES_JSON=[]
LLM_API_BASE_URL=
LLM_API_KEY=
LLM_EGRESS_ENABLED=false
LLM_ALLOWED_HTTPS_PREFIXES_JSON=[]
LLM_MODEL=gpt-5.6-sol
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
FEISHU_BOT_OPEN_ID=
FEISHU_WEATHER_BOT_OPEN_ID=
FEISHU_TASK_BOT_OPEN_ID=
FEISHU_ALLOW_LEGACY_NAME_MENTIONS=false
FEISHU_ALLOW_UNSIGNED_EVENTS=false
FEISHU_PASSIVE_REPLY_ENABLED=false
ELECTRICITY_WEATHER_ANALYSIS_ENABLED=false
MANUAL_POWER_BRIEFING_ENABLED=false
SUBSCRIPTIONS_ENABLED=false
ALERT_EVALUATION_ENABLED=false
EXTERNAL_DATA_WORKBENCH_ENABLED=false
CONVERSATION_HISTORY_ENABLED=false
CONVERSATION_HISTORY_TTL_SECONDS=1800
CONVERSATION_HISTORY_MAX_TURNS=6
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
FEISHU_TASK_BITABLE_TABLE_ID=
LOCAL_JSONL_PATH=data/weather_submissions.jsonl
LOCAL_TASK_JSONL_PATH=data/weather_tasks.jsonl
LOCAL_LOCATIONS_PATH=data/locations.json
LOCAL_NEWS_JSONL_PATH=data/news_items.jsonl
LOCAL_HYDROLOGY_JSONL_PATH=data/hydrology_records.jsonl
POWER_BRIEFING_ALLOW_SEND=false
POWER_BRIEFING_AFTERNOON_ALLOW_SEND=false
POWER_BRIEFING_TARGETS_JSON=[]
LEGACY_WEATHER_SCHEDULER_ENABLED=false
WEATHER_SOURCE_POLICIES_JSON=[]
ALERT_SEND_ENABLED=false
SUBSCRIPTION_ADMIN_OPEN_IDS_JSON=[]
PUBLIC_BASE_URL=
```

电力气象分析、手动晨报、订阅、告警评估和旧版外部数据工作台使用五个互不替代的功能开关；订阅激活不等于允许评估，允许评估也不等于允许发送。生产模板全部默认关闭。旧版新闻/水文手工录入尚未接入来源、许可和保留期适配器，因此在所有发布阶段都必须保持 `EXTERNAL_DATA_WORKBENCH_ENABLED=false`。基础城市天气查询不依赖这些开关。自由文本对话历史也默认关闭；地点、日期、指标、任务和晨报指针使用独立的结构化状态、五维隔离和分类 TTL，不需要保存聊天全文。

群聊采用严格触发策略：只处理飞书事件中带有机器人结构化 `mentions`、且 mention `open_id` 与当前机器人配置完全一致的文本消息，或对机器人已发送消息的精确回复；普通群消息、同名普通用户、手打的“@云云”文本以及卡片、文件、图片等非文本消息一律静默忽略。生产必须配置 `FEISHU_WEATHER_BOT_OPEN_ID`、`FEISHU_TASK_BOT_OPEN_ID`（单机器人兼容入口使用 `FEISHU_BOT_OPEN_ID`），并保持 `FEISHU_ALLOW_LEGACY_NAME_MENTIONS=false`。私聊无需 `@`。

### 生产发布硬阻断

当前默认配置是有意设计的 fail-closed 状态：`WEATHER_SOURCE_POLICIES_JSON=[]`、`POWER_BRIEFING_TARGETS_JSON=[]` 且发送开关关闭。在以下人工清单完成之前，不得部署为会抓取真实业务事实或向飞书主动发送的生产版本，也不得为了“先跑起来”而填入猜测的许可或目标：

1. 每个外部来源逐项记录 provider、精确 endpoint 前缀、合同/条款版本、商业使用范围、允许用途、署名要求、缓存与保留期、配额/成本、责任人和复核日期。
2. 将审核结论转换为与生产环境精确匹配的 `SourcePolicy`，核对 required metrics、单位、覆盖模型、时区、最大数据年龄、最低完整率和 `derived_only`/`metadata_only` 保留策略；禁止 `raw_storage`。
3. 对每个飞书目标记录真实 `chat_id`、授权人、用途、频率、静默时段、紧急停发责任人和复核日期，再生成最小 `POWER_BRIEFING_TARGETS_JSON`。群管理员 open_id 和机器人 open_id 也必须独立复核。
4. 密钥、Token、chat_id 和内部许可材料只能通过生产密钥/配置系统注入，不写入仓库、镜像、日志、报告或测试 fixture。
5. 先在 `DRY_RUN=true`、全局发送关闭时通过 96 条事件回放、全量测试、配置解析和同一快照晨报校验；保存上一稳定镜像/提交、配置哈希和数据库备份。
6. 灰度先只计算不回复/不发送，再开放内部私聊，最后仅对一个已审核晨报目标观察一个完整发布周期；不得补发灰度前积压消息。异常时立即关闭 `ALERT_SEND_ENABLED` 和 `GLOBAL_FEISHU_SEND_ENABLED`，冻结 outbox，恢复上一稳定镜像/配置，并保留审计数据排查。

上述硬门禁可用离线 preflight 转成机器可读结果（不会联网、抓天气或发送飞书）：

```powershell
.\.venv\Scripts\python.exe -m services.weather_bot.release_preflight `
  --phase shadow `
  --evidence C:\secure\weather-release-evidence.json
```

`--phase` 依次支持 `shadow`、`passive`、`scheduled`。`shadow` 要求五项受控能力和上午/下午主动发送全部关闭；初始 `passive` 只允许电力气象分析和鉴权后的被动回复；`scheduled` 只允许已审核的同快照晨报链，可分别审核 09:00 和 15:00 开关，手动晨报、订阅、告警评估和旧版外部数据工作台仍关闭。命令只输出检查代码和通用说明，
不会回显 Token、app secret、open_id 或 chat_id；通过返回退出码 0，任一项缺失返回 2 和
`BLOCKED`。外部 evidence 文件至少要引用待发布/上一稳定提交、配置 SHA-256、可读备份、
监控与回滚责任人、逐来源审批单及有效期；首次计划发送还必须恰好对应一个目标审批引用。
preflight 的 `READY` 只是必要条件，不能替代真实许可、目标授权和变更审批。

现有 08:50/09:00 与 14:50/15:00 任务定义在灰度和回滚中都保留；只有重新核对来源、目标、幂等和快照后才可恢复对应时次发送。晨报 3.1 的信息结构、演示卡和 22 项验收矩阵见 `docs/power_briefing_3_1_design.md`，完整发布检查表见 `docs/weather_power_trading_assistant_upgrade_plan.md`。

## Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Compose 包含两个服务：

| 服务 | 说明 |
|---|---|
| `weather-bot` | FastAPI、飞书事件回调、手动接口 |
| `weather-scheduler` | 旧版社区节奏；默认保持空闲，仅在 `LEGACY_WEATHER_SCHEDULER_ENABLED=true` 时发布任务、提醒、预测和关闭窗口 |

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
