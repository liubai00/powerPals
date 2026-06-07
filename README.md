# PowerPals 深圳气象机器人与任务发布机器人

PowerPals 是小可爱电力社区面向电力行业 AI Bot 共建、共测、评分、复盘的开源示范项目。本仓库首版固定服务 **广东省深圳市**，目标不是给出交易或报价建议，而是跑通社区第一条可复用的共测闭环：

```text
任务发布 -> Bot 预测 -> 标准 JSON 提交 -> 飞书卡片展示 -> 多维表格留痕 -> 后续评分与复盘
```

当前包含两个机器人：

- **深圳气象预测机器人**：聚合 Open-Meteo、和风天气 QWeather、彩云天气，生成 `weather_submission_v1` 官方提交 JSON、飞书卡片和提交记录。
- **气象任务发布机器人**：按社区节奏发布深圳气象预测任务，提醒参评 Bot 提交，关闭提交窗口，并记录任务状态。

社区口径：

```text
共建是宗旨，共测是机制，评分是工具，复盘是方法，成长是结果。
```

## 首版范围

| 项目 | 说明 |
|---|---|
| 地区 | 广东省深圳市 |
| 赛道 | `weather_forecast` |
| 任务类型 | 深圳日前逐小时气象预测 |
| 时间粒度 | 默认 `1h` |
| 数据源 | Open-Meteo、QWeather、Caiyun |
| Agent 层 | OpenClaw 可选；未配置时使用本地确定性解释 |
| 飞书能力 | 群内命令、消息卡片、多维表格留痕 |
| 裁判能力 | 首版只做格式校验、记录留痕、基础评分字段预留 |

## 目录结构

```text
powerPals/
  services/weather_bot/              # FastAPI 服务、天气聚合、飞书和任务发布逻辑
  schemas/weather_submission_v1.schema.json
  examples/weather_submission_shenzhen.json
  openclaw/skills/powerpals-shenzhen-weather/
  docs/shenzhen_weather_bot_v1.md
  docs/clawhub_weather_skill_review.md
  tests/
  Dockerfile
  docker-compose.yml
  .env.example
```

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

请求一次深圳气象预测：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/weather/forecast `
  -ContentType "application/json" `
  -Body '{"target_date":"2026-06-10","granularity":"1h","providers":["open_meteo","qweather","caiyun"]}'
```

发布一个深圳气象任务：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/tasks/weather/publish `
  -ContentType "application/json" `
  -Body '{"target_date":"2026-06-10"}'
```

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务健康检查 |
| `POST` | `/api/weather/forecast` | 生成标准气象预测提交 |
| `POST` | `/api/weather/submission` | 记录外部 Bot 的标准提交 |
| `POST` | `/api/weather/publish` | 生成预测、发布预测卡片并记录 |
| `POST` | `/api/tasks/weather/create` | 生成任务草稿 |
| `POST` | `/api/tasks/weather/publish` | 发布任务卡片并记录任务 |
| `POST` | `/api/tasks/weather/remind` | 发布提交提醒并记录任务状态 |
| `POST` | `/api/tasks/weather/close` | 关闭提交窗口，进入等待实况评分状态 |
| `GET` | `/api/tasks/weather/{task_id}` | 按任务 ID 查询任务 |
| `POST` | `/feishu/events` | 飞书事件回调入口 |

## 社区任务节奏

任务 ID 格式：

```text
WEATHER-SZ-YYYYMMDD-DAYAHEAD-001
```

默认节奏：

| 时间 | 动作 | 说明 |
|---|---|---|
| D-1 09:00 | 发布任务 | 任务发布机器人发送任务卡片 |
| D-1 16:00 | 数据截止 | 参评 Bot 只能使用该时间前可获得的数据 |
| D-1 16:30 | 提交提醒 | 提醒参评 Bot 准备提交 |
| D-1 17:00 | 发布官方预测 | 深圳气象预测机器人发布基线预测卡片 |
| D-1 17:05 | 关闭任务 | 任务状态变为 `closed`，评分状态变为 `waiting_truth` |
| D 00:00-23:00 | 预测窗口 | 后续裁判 Bot 可用实况数据评分 |

Docker 中的 `weather-scheduler` 服务会按上表自动执行。

## 标准提交格式

`/api/weather/forecast` 返回 `weather_submission_v1`，同时保留兼容字段和社区官方字段。

关键字段：

```text
submission_type
task_id
track
bot
scope
time_info
data_profile
payload
confidence
explanation
scoring_profile
disclaimer
```

示例见：

```text
examples/weather_submission_shenzhen.json
schemas/weather_submission_v1.schema.json
```

## 多源聚合规则

默认权重：

| 指标 | QWeather | Open-Meteo | Caiyun |
|---|---:|---:|---:|
| 温度 | 0.40 | 0.35 | 0.25 |
| 风速 | 0.40 | 0.35 | 0.25 |
| 云量 | 0.40 | 0.35 | 0.25 |
| 降水概率 | 0.35 | 0.20 | 0.45 |

如果某个数据源未配置或请求失败，系统会自动排除该数据源，并对剩余数据源重新归一化权重。所有输出都会标注实际参与聚合的数据源、数据截止时间、风险提示和免责声明。

## 飞书接入

1. 在飞书开放平台创建企业自建应用。
2. 开启机器人能力。
3. 配置事件订阅地址：

```text
https://<your-domain>/feishu/events
```

4. 将飞书事件订阅里的 Verification Token 写入 `.env`。
5. 将机器人加入目标群，并配置 `FEISHU_DEFAULT_CHAT_ID`。
6. 如需写入多维表格，准备两个表：

| 表 | 用途 | 环境变量 |
|---|---|---|
| 预测提交表 | 记录每次气象预测 JSON 和卡片消息 ID | `FEISHU_BITABLE_TABLE_ID` |
| 任务发布表 | 记录任务发布、提醒、关闭状态 | `FEISHU_TASK_BITABLE_TABLE_ID` |

支持的群内命令：

```text
@机器人 明天深圳天气
@机器人 深圳气象预测 2026-06-10
@机器人 今日气象任务
@机器人 帮助
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
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
FEISHU_TASK_BITABLE_TABLE_ID=
LOCAL_JSONL_PATH=data/weather_submissions.jsonl
LOCAL_TASK_JSONL_PATH=data/weather_tasks.jsonl
```

说明：

- Open-Meteo 不需要 API Key。
- QWeather 和 Caiyun 没有 Key 时会被标记为 `disabled`。
- OpenClaw 未配置时，会使用本地确定性解释，不影响核心预测流程。
- 飞书多维表格未配置时，预测和任务都会写入本地 JSONL 备份。

## OpenClaw Skill

项目内置了自用 OpenClaw skill：

```text
openclaw/skills/powerpals-shenzhen-weather/
```

使用方式：

```bash
export POWERPALS_WEATHER_API_BASE="https://your-domain.example.com"
```

这个 skill 不直接抓天气，而是调用本项目的 FastAPI 服务，保证输出仍然遵守 PowerPals 标准数据结构和任务节奏。

## Docker 部署

```bash
cp .env.example .env
docker compose up -d --build
```

Compose 包含两个服务：

| 服务 | 说明 |
|---|---|
| `weather-bot` | FastAPI、飞书事件回调、手动接口 |
| `weather-scheduler` | 按社区节奏自动发布任务、提醒、发布预测、关闭窗口 |

如果只是检查配置：

```bash
docker compose config
```

正式接飞书时，服务器必须有公网 HTTPS 域名。

## 测试

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m compileall services tests
docker compose config
docker compose build
```

当前测试覆盖：

- 多源权重聚合与缺失数据源重归一。
- 官方 `weather_submission_v1` 提交结构。
- FastAPI 气象预测、任务发布、飞书事件接口。
- 飞书 URL verification 与 token 校验。
- 飞书预测卡片和任务卡片结构。
- 示例 JSON 与 schema 校验。
- 社区节奏 scheduler 计划。
- 任务发布本地 JSONL 留痕和任务表字段映射。

## 合规边界

本项目输出仅用于小可爱电力社区共建、评分和复盘，不构成：

- 交易建议；
- 报价建议；
- 投资建议；
- 收益承诺；
- 商业认证。

任何业务使用都应结合实际市场规则、数据口径和合规要求独立判断。
