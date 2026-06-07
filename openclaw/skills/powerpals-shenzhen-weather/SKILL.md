---
name: powerpals-shenzhen-weather
description: Use the PowerPals Shenzhen weather bot API to publish weather tasks and generate standard forecast submissions for Feishu and community scoring.
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

Use this skill when the user asks for Shenzhen weather forecasts, PowerPals weather submissions, Feishu-ready summaries, or weather task publishing for the PowerPals community scoring flow.

## Boundaries

- Only handle Guangdong Shenzhen weather forecast tasks.
- Follow the rhythm: task publish, Bot submission, Feishu display, record, later scoring and review.
- Do not provide trading advice, quote advice, investment advice, profit promises, or commercial certification.
- Do not invent weather values. Use the PowerPals Weather Bot API response.
- If the API reports a provider as disabled or error, clearly state which source was unavailable.

## Environment

The service base URL is read from:

```text
POWERPALS_WEATHER_API_BASE
```

## Weather Forecast API

Call:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/weather/forecast" \
  -H "Content-Type: application/json" \
  -d '{"region":"深圳","target_date":"YYYY-MM-DD","granularity":"1h","providers":["open_meteo","qweather","caiyun"]}'
```

The response is the standard `weather_submission_v1` JSON object. Preserve these official fields:

- `submission_type`
- `task_id`
- `track`
- `bot`
- `scope`
- `time_info`
- `data_profile`
- `payload`
- `confidence`
- `explanation`
- `scoring_profile`
- `disclaimer`

For human-readable replies, summarize:

1. task ID, region, target date, and data cutoff time;
2. providers used and unavailable providers;
3. max/min temperature, rain probability, wind speed, cloud cover, main weather, and high-risk period;
4. key factors and risk notes;
5. the disclaimer.

## Weather Task APIs

Publish a task:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/tasks/weather/publish" \
  -H "Content-Type: application/json" \
  -d '{"target_date":"YYYY-MM-DD"}'
```

Send a reminder:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/tasks/weather/remind" \
  -H "Content-Type: application/json" \
  -d '{"target_date":"YYYY-MM-DD"}'
```

Close the submission window:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/tasks/weather/close" \
  -H "Content-Type: application/json" \
  -d '{"target_date":"YYYY-MM-DD"}'
```

Task IDs use:

```text
WEATHER-SZ-YYYYMMDD-DAYAHEAD-001
```

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

User:

```text
发布 2026-06-10 深圳气象任务
```

Action:

1. Call `/api/tasks/weather/publish`.
2. Return the task ID, data cutoff time, submission deadline, status, and Feishu-card summary.
3. Remind the user that outputs are only for community co-building, scoring, and review.
