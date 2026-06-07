---
name: powerpals-weather
description: Use the PowerPals weather bot API to query nationwide weather, publish weather tasks, and generate standard forecast submissions for Feishu and community scoring.
metadata:
  openclaw:
    requires:
      env:
        - POWERPALS_WEATHER_API_BASE
      bins:
        - curl
    primaryEnv: POWERPALS_WEATHER_API_BASE
---

# PowerPals Weather

Use this skill when the user asks for China weather forecasts, PowerPals weather submissions, Feishu-ready summaries, or weather task publishing for the PowerPals community scoring flow.

## Boundaries

- Support city, region, and explicit latitude/longitude inputs.
- Use the configured default location when the user provides no location; the packaged default is Guangdong Shenzhen for backward compatibility.
- Follow the rhythm: task publish, Bot submission, Feishu display, record, later scoring and review.
- Do not provide trading advice, quote advice, investment advice, profit promises, or commercial certification.
- Do not invent weather values. Use the PowerPals Weather Bot API response.
- If the API reports a provider as disabled or error, clearly state which source was unavailable.

## Environment

The service base URL is read from:

```text
POWERPALS_WEATHER_API_BASE
```

## Forecast API

Single day:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/weather/forecast" \
  -H "Content-Type: application/json" \
  -d '{"region":"广州","target_date":"YYYY-MM-DD","granularity":"1h","providers":["open_meteo","qweather","caiyun"]}'
```

Coordinates:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/weather/forecast" \
  -H "Content-Type: application/json" \
  -d '{"region":"广州南沙","latitude":22.8016,"longitude":113.5252,"target_date":"YYYY-MM-DD"}'
```

Multiple days:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/weather/forecast/range" \
  -H "Content-Type: application/json" \
  -d '{"region":"广州","target_date":"YYYY-MM-DD","days":3}'
```

Preserve these official fields from every `weather_submission_v1` object:

- `submission_type`
- `task_id`
- `track`
- `bot`
- `scope`
- `scope.location`
- `time_info`
- `data_profile`
- `payload`
- `confidence`
- `explanation`
- `scoring_profile`
- `disclaimer`

## Weather Task APIs

Publish a task:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/tasks/weather/publish" \
  -H "Content-Type: application/json" \
  -d '{"region":"广州","target_date":"YYYY-MM-DD"}'
```

Send a reminder:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/tasks/weather/remind" \
  -H "Content-Type: application/json" \
  -d '{"region":"广州","target_date":"YYYY-MM-DD"}'
```

Close the submission window:

```bash
curl -s -X POST "$POWERPALS_WEATHER_API_BASE/api/tasks/weather/close" \
  -H "Content-Type: application/json" \
  -d '{"region":"广州","target_date":"YYYY-MM-DD"}'
```

Task IDs use:

```text
WEATHER-CN-<location-token>-YYYYMMDD-DAYAHEAD-001
```

## Example Prompt Handling

User:

```text
广州未来三天天气，按 PowerPals 格式输出
```

Action:

1. Resolve the start date in Asia/Shanghai.
2. Call `/api/weather/forecast/range` with `days=3`.
3. Return concise summaries and preserve each standard JSON object if the user asks for structured output.

User:

```text
发布 2026-06-10 北京气象任务
```

Action:

1. Call `/api/tasks/weather/publish`.
2. Return the task ID, region, coordinates, data cutoff time, submission deadline, status, and Feishu-card summary.
3. Remind the user that outputs are only for community co-building, scoring, and review.
