---
name: powerpals-shenzhen-weather
description: Use the PowerPals Shenzhen weather bot API to generate standard weather forecast submissions for Feishu and community scoring.
metadata:
  openclaw:
    requires:
      env:
        - POWERPALS_WEATHER_API_BASE
      bins:
        - curl
    primaryEnv: POWERPALS_WEATHER_API_BASE
---

# PowerPals Shenzhen Weather

Use this skill when the user asks for Shenzhen weather forecasts, PowerPals weather submissions, Feishu-ready weather summaries, or weather data for the PowerPals community scoring flow.

## Boundaries

- Only handle Guangdong Shenzhen weather forecast tasks.
- Do not provide trading advice, quote advice, investment advice, profit promises, or commercial certification.
- Do not invent weather values. Use the PowerPals Weather Bot API response.
- If the API reports a provider as disabled or error, clearly state which source was unavailable.

## API

The service base URL is read from:

```text
POWERPALS_WEATHER_API_BASE
```

Call:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/weather/forecast" \
  -H "Content-Type: application/json" \
  -d '{"region":"深圳","target_date":"YYYY-MM-DD","granularity":"1h","providers":["open_meteo","qweather","caiyun"]}'
```

## Response Handling

The API response is already the standard `weather_submission_v1` JSON object. Preserve these fields:

- `task_id`
- `region`
- `target_date`
- `data_cutoff_time`
- `provider_results`
- `aggregated_forecast`
- `confidence`
- `key_factors`
- `risk_notes`
- `disclaimer`

For human-readable replies, summarize:

1. task ID, region, target date, and data cutoff time;
2. providers used and unavailable providers;
3. max/min temperature, rain probability, wind speed, cloud cover, main weather, and high-risk period;
4. key factors and risk notes;
5. the disclaimer.

## Example Prompt Handling

User:

```text
明天深圳天气，按 PowerPals 格式输出
```

Action:

1. Resolve tomorrow's date in Asia/Shanghai.
2. Call `/api/weather/forecast`.
3. Return the standard JSON if the user asks for structured output.
4. Return a concise Feishu-style summary if the user asks for a readable forecast.
