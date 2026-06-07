# 深圳气象机器人 V1 实施说明

本文档说明 PowerPals 首版深圳气象共测的两个机器人：

- **深圳气象预测机器人**：生成深圳日前逐小时气象预测、官方提交 JSON、飞书预测卡片和提交记录。
- **气象任务发布机器人**：发布任务、提醒提交、关闭提交窗口，并为后续裁判 Bot 留出任务状态。

首版只支持 **广东省深圳市**，不扩展全国；首版不训练独立气象模型，采用“多源聚合 + 大模型解释或本地解释兜底”。

## 社区目标

```text
共建是宗旨，共测是机制，评分是工具，复盘是方法，成长是结果。
```

V1 的目标不是建立完整榜单，而是跑通一条最小闭环：

```text
任务发布 -> 预测提交 -> 格式校验 -> 飞书展示 -> 记录留痕 -> 等待实况评分
```

## 架构

```text
Feishu group command / scheduler
  -> FastAPI bridge
  -> Task service or forecast service
  -> Weather provider clients
  -> Weighted aggregation
  -> OpenClaw explainer or deterministic fallback
  -> Feishu card
  -> Feishu Bitable
  -> Local JSONL fallback
```

## 任务节奏

| 时间 | 动作 | 接口 |
|---|---|---|
| D-1 09:00 | 发布深圳气象预测任务 | `POST /api/tasks/weather/publish` |
| D-1 16:00 | 数据截止 | 写入任务与提交 JSON |
| D-1 16:30 | 提醒参评 Bot 提交 | `POST /api/tasks/weather/remind` |
| D-1 17:00 | 发布官方深圳预测卡片 | `POST /api/weather/publish` |
| D-1 17:05 | 关闭提交窗口 | `POST /api/tasks/weather/close` |
| D 00:00-23:00 | 预测对象时间窗 | 后续裁判 Bot 使用 |

任务 ID：

```text
WEATHER-SZ-YYYYMMDD-DAYAHEAD-001
```

示例：

```text
WEATHER-SZ-20260610-DAYAHEAD-001
```

## 气象预测接口

请求：

```json
{
  "region": "深圳",
  "target_date": "2026-06-10",
  "granularity": "1h",
  "providers": ["open_meteo", "qweather", "caiyun"]
}
```

响应为 `weather_submission_v1`，必须包含：

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

同时保留兼容字段：

```text
region
target_date
data_cutoff_time
provider_results
aggregated_forecast
key_factors
risk_notes
```

## 任务发布接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/tasks/weather/create` | 创建任务草稿 |
| `POST` | `/api/tasks/weather/publish` | 发布任务卡片并记录 |
| `POST` | `/api/tasks/weather/remind` | 生成提醒卡片并记录 |
| `POST` | `/api/tasks/weather/close` | 关闭任务，等待实况评分 |
| `GET` | `/api/tasks/weather/{task_id}` | 查询任务 |

请求：

```json
{
  "target_date": "2026-06-10"
}
```

任务记录字段：

```text
task_id
track
region
target_date
forecast_start
forecast_end
publish_time
data_cutoff_time
reminder_time
submission_deadline
status
task_card_message_id
submission_format_version
scoring_status
notes
```

## 飞书多维表格

建议建立两张表。

预测提交表字段：

```text
task_id
target_date
region
submit_time
data_cutoff_time
providers_used
max_temp
min_temp
rain_probability
wind_speed
cloud_cover
confidence
risk_summary
json_payload
card_message_id
status
notes
```

任务发布表字段：

```text
task_id
track
region
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

如果飞书多维表格未配置，系统会写入本地：

```text
data/weather_submissions.jsonl
data/weather_tasks.jsonl
```

## 聚合规则

- 温度、风速、云量：QWeather 0.40，Open-Meteo 0.35，Caiyun 0.25。
- 降水概率：Caiyun 0.45，QWeather 0.35，Open-Meteo 0.20。
- 某个数据源失败或未配置时，剩余数据源权重自动归一。
- 所有输出必须标注实际参与聚合的数据源。

## OpenClaw 分工

OpenClaw 只做意图理解、工具调用和中文解释，不直接计算天气结果。天气计算由 FastAPI 后端完成，避免模型编造数据。

推荐调用顺序：

```text
用户问题
  -> OpenClaw skill
  -> /api/weather/forecast 或 /api/tasks/weather/publish
  -> 返回标准 JSON 或飞书可读摘要
```

## Docker

`docker-compose.yml` 包含两个服务：

| 服务 | 说明 |
|---|---|
| `weather-bot` | FastAPI 服务 |
| `weather-scheduler` | 社区节奏调度器 |

调度器执行：

```text
09:00 publish_task
16:30 remind_task
17:00 publish_forecast
17:05 close_task
```

## 验收

- 飞书群内可以查询深圳明日天气。
- 飞书群内可以发布今日气象任务。
- 定时任务可以自动发布任务、提醒、发布预测、关闭任务。
- 每次预测生成标准 `weather_submission_v1` JSON。
- 预测与任务都能写入飞书多维表格；未配置飞书时写入本地 JSONL。
- 输出包含数据来源、数据截止时间、风险说明和免责声明。

## 合规边界

机器人输出仅用于社区共建、评分和复盘，不构成交易建议、报价建议、投资建议、收益承诺或商业认证。
