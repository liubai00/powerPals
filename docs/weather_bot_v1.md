# 全国气象机器人 V1 实施说明

本文档说明 PowerPals 气象共测的两个主机器人和一个最小裁判评分工具：

- **全国气象预测机器人**：根据城市、地区或经纬度生成逐小时气象预测、官方提交 JSON、飞书预测卡片和提交记录，响应标记 `bot_role=weather_forecast_bot`。
- **气象任务发布机器人**：发布任务、提醒提交、关闭提交窗口、记录任务状态，为后续裁判 Bot 评分和复盘留痕，响应标记 `bot_role=weather_task_bot`。
- **最小裁判评分工具**：输入标准预测 JSON 和实况摘要，输出基础误差、命中情况和综合分。它暂时不是完整榜单平台。

深圳仍是默认地区，但不再是限制。当前版本支持全国城市/地区/经纬度输入。

## 任务机器人具体能干什么

任务机器人解决的是“共测组织问题”，不是“天气计算问题”。

它负责：

- 发布任务：明确测哪个地区、哪一天、什么时间窗口。
- 统一规则：写清数据截止时间、提交截止时间、提交格式和免责声明。
- 提醒提交：在 D-1 16:30 提醒参评 Bot。
- 关闭窗口：在 D-1 17:05 关闭提交，进入等待实况评分状态。
- 记录留痕：把任务状态写入飞书多维表格或本地 JSONL。
- 衔接裁判：为后续裁判 Bot 提供 `task_id`、地区、经纬度、预测窗口、提交格式。

它不负责：

- 不编造天气数据。
- 不替代气象预测机器人。
- 不直接打分，打分由裁判工具或后续裁判 Bot 完成。
- 不给交易、报价、投资或收益建议。

## 位置输入

预测和任务接口都支持同一套位置输入。

城市/地区：

```json
{
  "region": "广州",
  "target_date": "2026-06-10"
}
```

经纬度：

```json
{
  "region": "广州南沙",
  "latitude": 22.8016,
  "longitude": 113.5252,
  "target_date": "2026-06-10"
}
```

默认：

```json
{
  "target_date": "2026-06-10"
}
```

默认解析为 `广东省深圳市`。

## 位置解析流程

```text
请求
  -> 显式经纬度
  -> 内置常用城市表
  -> QWeather GeoAPI
  -> Open-Meteo Geocoding
  -> 解析失败则返回错误
```

首版内置深圳、广州、北京、上海等常用城市，便于没有 API Key 时本地演示。配置和风天气 Key 后，全国城市解析会更完整。

## 架构

```text
Feishu command / scheduled task / HTTP API
  -> intent and location parsing
  -> task service or forecast service
  -> weather provider clients
  -> weighted aggregation
  -> OpenClaw explainer or deterministic fallback
  -> Feishu card
  -> Feishu Bitable
  -> local JSONL fallback
```

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/weather/forecast` | 单日气象预测 |
| `POST` | `/api/weather/forecast/range` | 多日气象预测 |
| `POST` | `/api/tasks/weather/create` | 创建任务草稿 |
| `POST` | `/api/tasks/weather/publish` | 发布任务 |
| `POST` | `/api/tasks/weather/remind` | 提醒提交 |
| `POST` | `/api/tasks/weather/close` | 关闭任务 |
| `GET` | `/api/tasks/weather/{task_id}` | 按任务 ID 查询任务，优先读内存，再读本地 JSONL |
| `POST` | `/api/judge/weather/score` | 对单条标准预测做基础评分 |
| `POST` | `/feishu/events` | 飞书回调 |

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

## 飞书命令

支持：

```text
@机器人 明天深圳天气
@机器人 广州明天天气
@机器人 广州未来三天天气
@机器人 22.8016,113.5252 明天天气
@机器人 今日广州气象任务
@机器人 22.8016,113.5252 今日气象任务
@机器人 发布北京气象任务 2026-06-10
@机器人 帮助
```

当前规则：如果一句话同时包含“气象任务”和“天气预测”，优先按任务处理。响应里的 `bot_role` 会明确说明由预测机器人还是任务机器人处理。后续可以引入更完整的 Intent Router，在混合意图时主动澄清或拆成两步。

## 标准提交

`weather_submission_v1` 现在在 `scope.location` 中记录标准位置：

```json
{
  "name": "广东省广州市",
  "code": "440100",
  "latitude": 23.1291,
  "longitude": 113.2644,
  "source": "builtin"
}
```

这让预测、任务、裁判、复盘都能使用同一个位置口径。

## 多日预测

`/api/weather/forecast/range` 会连续生成多个单日标准提交。它不会改变单日评分格式，而是返回：

```text
submissions[0] -> 第一天 weather_submission_v1
submissions[1] -> 第二天 weather_submission_v1
submissions[2] -> 第三天 weather_submission_v1
```

这样既支持“未来三天查询”，又不破坏后续裁判 Bot 的单日评分逻辑。

## 最小裁判评分

`/api/judge/weather/score` 先服务于“能不能复盘”的问题，不追求完整榜单。输入包含：

```text
submission: 标准 weather_submission_v1
truth.max_temperature
truth.min_temperature
truth.rain_observed
truth.wind_speed
```

输出包含：

```text
metrics: 温度误差、降水预测/实况/命中、风速误差
component_scores: temperature, rain, wind
total_score: 综合分
summary: 中文评分摘要
```

默认权重为温度 45%、降水 35%、风速 20%。降水以 `rain_probability >= 50%` 作为有雨预测阈值。后续裁判 Bot 可以在这个接口之上增加实况数据拉取、多 Bot 汇总、排行榜和复盘报告。

## 定时节奏

默认节奏仍为：

| 时间 | 动作 |
|---|---|
| D-1 09:00 | 发布任务 |
| D-1 16:30 | 提醒提交 |
| D-1 17:00 | 发布预测 |
| D-1 17:05 | 关闭任务 |

当前 scheduler 会读取 `.env` 中的 `DEFAULT_WEATHER_REGION`、`DEFAULT_WEATHER_LATITUDE`、`DEFAULT_WEATHER_LONGITUDE`。不配置时默认发布深圳任务，配置城市名或经纬度后，可自动发布对应地区的任务和官方预测。后续如果要“每天多个城市自动任务”，建议增加任务配置表，由 scheduler 读取城市列表和日期策略。

## 验收

- 可以按城市查询：广州、北京、上海、深圳。
- 可以按经纬度查询。
- 可以发布全国城市任务。
- 可以发布经纬度任务。
- 可以查询未来三天。
- 任务记录包含地区、经纬度、位置来源。
- 服务重启后，可以从本地 JSONL 回查已发布任务。
- 可以对单条标准预测做最小裁判评分。
- 标准提交 JSON 通过 schema 校验。
- 不配置飞书时，本地 JSONL 可留痕。

## 合规边界

机器人输出仅用于社区共建、评分和复盘，不构成交易建议、报价建议、投资建议、收益承诺或商业认证。
