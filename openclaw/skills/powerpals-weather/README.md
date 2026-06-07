# PowerPals Weather Skill

This OpenClaw skill calls the PowerPals Weather Bot API. It can:

- generate standard `weather_submission_v1` forecast JSON;
- query nationwide weather by city, region, or coordinates;
- return Feishu-readable weather summaries;
- publish, remind, and close weather forecast tasks.

## Setup

```bash
export POWERPALS_WEATHER_API_BASE="https://your-domain.example.com"
```

For local development:

```bash
export POWERPALS_WEATHER_API_BASE="http://127.0.0.1:8000"
```

## Forecast Examples

```text
广州明天天气，按 PowerPals 格式输出
广州未来三天天气
22.8016,113.5252 明天天气
```

The skill calls:

```text
POST /api/weather/forecast
POST /api/weather/forecast/range
```

## Task Examples

```text
发布 2026-06-10 北京气象任务
今日广州气象任务
```

The skill calls:

```text
POST /api/tasks/weather/publish
POST /api/tasks/weather/remind
POST /api/tasks/weather/close
```

All outputs are only for community co-building, scoring, and review.
