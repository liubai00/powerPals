# PowerPals 深圳气象机器人

PowerPals 深圳气象机器人是小可爱电力社区第一阶段气象预测共测的开源示范项目。首版固定服务 **广东省深圳市**，通过多家气象数据源聚合生成标准化预测结果，并输出飞书消息卡片、飞书多维表格记录和 `weather_submission_v1` JSON。

这个项目的目标不是直接给出交易建议，而是跑通社区的最小共测闭环：

```text
任务发布 -> 多源气象预测 -> 标准格式提交 -> 飞书展示 -> 记录留痕 -> 后续评分与复盘
```

## 核心能力

- 提供 FastAPI 服务，支持天气预测、飞书事件回调、发布和提交记录接口。
- 聚合 Open-Meteo、和风天气 QWeather、彩云天气三类气象数据源。
- 对温度、降水概率、风速、云量做统一单位转换和加权融合。
- 生成 PowerPals 标准 `weather_submission_v1` JSON。
- 生成飞书可读消息卡片。
- 支持写入飞书多维表格，便于运营和管理者查看结果。
- 在飞书多维表格未配置时，自动写入本地 JSONL 作为备份记录。
- 预留 OpenClaw 解释层：可接 OpenClaw API，也可使用本地确定性解释兜底。
- 提供 OpenClaw skill 模板，便于在 OpenClaw 中复用本项目 API。

## 首版范围

| 项目 | 说明 |
|---|---|
| 地区 | 广东省深圳市 |
| 时间粒度 | 逐小时，默认 `1h` |
| 数据源 | Open-Meteo、QWeather、Caiyun |
| 输出字段 | 温度、降水概率、风速、云量、风险说明、数据来源、免责声明 |
| 飞书能力 | 事件回调、消息卡片、可选多维表格写入 |
| 裁判能力 | 首版只做提交记录和格式标准化，不做完整自动榜单 |

## 目录结构

```text
powerPals/
  services/weather_bot/              # FastAPI 服务与核心业务代码
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
  -Body '{"region":"深圳","target_date":"2026-06-10","granularity":"1h","providers":["open_meteo","qweather","caiyun"]}'
```

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务健康检查 |
| `POST` | `/api/weather/forecast` | 生成标准气象预测提交 |
| `POST` | `/api/weather/submission` | 记录外部提交 |
| `POST` | `/api/weather/publish` | 生成预测、发布卡片并记录 |
| `POST` | `/feishu/events` | 飞书事件回调入口 |

`/api/weather/forecast` 请求示例：

```json
{
  "region": "深圳",
  "target_date": "2026-06-10",
  "granularity": "1h",
  "providers": ["open_meteo", "qweather", "caiyun"]
}
```

## 多源聚合规则

默认权重：

| 指标 | QWeather | Open-Meteo | Caiyun |
|---|---:|---:|---:|
| 温度 | 0.40 | 0.35 | 0.25 |
| 风速 | 0.40 | 0.35 | 0.25 |
| 云量 | 0.40 | 0.35 | 0.25 |
| 降水概率 | 0.35 | 0.20 | 0.45 |

如果某个数据源未配置或请求失败，系统会自动排除该数据源，并对剩余数据源重新归一化权重。

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
LOCAL_JSONL_PATH=data/weather_submissions.jsonl
```

说明：

- Open-Meteo 不需要 API Key。
- QWeather 和 Caiyun 没有 Key 时会被标记为 `disabled`。
- OpenClaw 未配置时，会使用本地确定性解释，不影响核心预测流程。
- 飞书多维表格未配置时，会写入本地 JSONL 备份。

## 飞书接入

1. 在飞书开放平台创建企业自建应用。
2. 开启机器人能力。
3. 配置事件订阅地址：

```text
https://<your-domain>/feishu/events
```

4. 将飞书事件订阅里的 Verification Token 写入 `.env`：

```text
FEISHU_VERIFICATION_TOKEN=
```

5. 将机器人加入目标群，并配置：

```text
FEISHU_DEFAULT_CHAT_ID=
```

6. 如需写入多维表格，配置：

```text
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
```

支持的群内命令：

```text
@机器人 明天深圳天气
@机器人 深圳气象预测 2026-06-10
@机器人 今日气象任务
@机器人 帮助
```

## OpenClaw Skill

项目内置了自用 OpenClaw skill：

```text
openclaw/skills/powerpals-shenzhen-weather/
```

使用方式：

```bash
export POWERPALS_WEATHER_API_BASE="https://your-domain.example.com"
```

这个 skill 不直接抓天气，而是调用本项目的 FastAPI 服务，保证输出仍然遵守 PowerPals 标准数据结构。

## Docker 部署

```bash
cp .env.example .env
docker compose up -d --build
```

如果只是检查配置：

```bash
docker compose config
```

正式接飞书时，服务器必须有公网 HTTPS 域名。

## 测试

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m compileall services tests
```

当前测试覆盖：

- 多源权重聚合与缺失数据源重归一。
- 标准气象提交对象生成。
- FastAPI 核心接口。
- 飞书 URL verification 与 token 校验。
- 飞书卡片结构。
- 示例 JSON 与 schema 校验。

## 合规边界

本项目输出仅用于小可爱电力社区共建、评分和复盘，不构成：

- 交易建议；
- 报价建议；
- 投资建议；
- 收益承诺；
- 商业认证。

任何业务使用都应结合实际市场规则、数据口径和合规要求独立判断。
