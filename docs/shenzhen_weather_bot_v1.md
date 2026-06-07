# Shenzhen Weather Bot V1

## Scope

V1 only supports Guangdong Shenzhen weather forecasts for the PowerPals weather track.

Default request:

```json
{
  "region": "深圳",
  "target_date": "2026-06-10",
  "granularity": "1h",
  "providers": ["open_meteo", "qweather", "caiyun"]
}
```

## Data Flow

```text
Feishu command or scheduled task
  -> FastAPI bridge
  -> ForecastService
  -> Weather provider clients
  -> Weighted aggregation
  -> OpenClaw explainer or deterministic fallback
  -> Feishu card + JSON submission
  -> Feishu Bitable + local JSONL fallback
```

## Aggregation

- Temperature, wind speed, cloud cover: QWeather 0.4, Open-Meteo 0.35, Caiyun 0.25.
- Rain probability: Caiyun 0.45, QWeather 0.35, Open-Meteo 0.2.
- Failed or disabled providers are excluded and the remaining weights are renormalized.

## Bitable Fields

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

## Compliance

The bot output is only for community co-building, scoring, and review. It must not be presented as trading advice, quote advice, investment advice, profit promises, or commercial certification.
