# PowerPals Shenzhen Weather Skill

This OpenClaw skill calls the PowerPals Shenzhen Weather Bot API and returns either the standard `weather_submission_v1` JSON or a Feishu-readable weather summary.

## Setup

Set:

```bash
export POWERPALS_WEATHER_API_BASE="https://your-domain.example.com"
```

For local development:

```bash
export POWERPALS_WEATHER_API_BASE="http://127.0.0.1:8000"
```

## Example

```text
明天深圳天气，按 PowerPals 格式输出
```

The skill calls:

```text
POST /api/weather/forecast
```

and preserves the community-standard schema.
