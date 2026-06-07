# PowerPals Shenzhen Weather Skill

This OpenClaw skill calls the PowerPals Shenzhen weather bot API. It can:

- generate the standard `weather_submission_v1` forecast JSON;
- return a Feishu-readable weather summary;
- publish, remind, and close Shenzhen weather forecast tasks.

## Setup

Set:

```bash
export POWERPALS_WEATHER_API_BASE="https://your-domain.example.com"
```

For local development:

```bash
export POWERPALS_WEATHER_API_BASE="http://127.0.0.1:8000"
```

## Forecast Example

```text
明天深圳天气，按 PowerPals 格式输出
```

The skill calls:

```text
POST /api/weather/forecast
```

and preserves the community-standard schema.

## Task Example

```text
发布 2026-06-10 深圳气象任务
```

The skill calls:

```text
POST /api/tasks/weather/publish
```

The task rhythm is:

```text
D-1 09:00 publish task
D-1 16:30 remind submitters
D-1 17:00 publish official forecast
D-1 17:05 close submission window
```

All outputs are only for community co-building, scoring, and review.
