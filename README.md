# PowerPals Shenzhen Weather Bot

PowerPals 深圳气象预测机器人 V1。首版面向小可爱电力社区的气象预测共测，固定区域为广东省深圳市。

## What It Does

- Exposes a FastAPI service for weather forecasts and Feishu callbacks.
- Aggregates Open-Meteo, QWeather, and Caiyun weather forecasts.
- Uses deterministic aggregation for numbers and OpenClaw-compatible explanation for readable summaries.
- Produces standard `weather_submission_v1` JSON.
- Builds Feishu message cards and can write records to Feishu Bitable.
- Falls back to local JSONL storage when Feishu Bitable is not configured.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
.\.venv\Scripts\uvicorn services.weather_bot.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/health
```

Request a forecast:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/weather/forecast `
  -ContentType "application/json" `
  -Body '{"region":"深圳","target_date":"2026-06-10","granularity":"1h","providers":["open_meteo","qweather","caiyun"]}'
```

## Deployment

Use a cloud server with a public HTTPS domain. Feishu event subscriptions must call:

```text
https://<your-domain>/feishu/events
```

Docker:

```bash
cp .env.example .env
docker compose up -d --build
```

## Feishu Setup

1. Create a Feishu self-built app and enable bot capability.
2. Configure event subscription URL to `/feishu/events`.
3. Set `FEISHU_VERIFICATION_TOKEN`.
4. Add the bot to the target group and set `FEISHU_DEFAULT_CHAT_ID`.
5. Optional: create a Bitable and set `FEISHU_BITABLE_APP_TOKEN` and `FEISHU_BITABLE_TABLE_ID`.

Supported commands:

```text
@机器人 明天深圳天气
@机器人 深圳气象预测 2026-06-10
@机器人 今日气象任务
@机器人 帮助
```

## Provider Keys

Open-Meteo works without a key. QWeather and Caiyun are optional in V1:

```text
QWEATHER_API_KEY=
CAIYUN_API_KEY=
```

If a provider is missing or fails, the aggregator renormalizes weights across remaining usable providers and marks the missing provider as disabled or error.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```
