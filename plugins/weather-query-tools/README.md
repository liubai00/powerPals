# Weather Query Tools

Small OpenClaw tool plugin exposing two peer query tools:

- `qweather_query`: QWeather location, current conditions, daily/hourly forecast, and official alert queries.
- `openmeteo_query`: Open-Meteo geocoding and current/daily/hourly forecast queries.

Open-Meteo calls are serialized inside the adapter, and transient HTTP 429 responses use a short bounded retry so multi-location analysis does not fan out against the public endpoint.

Both tools return the provider's native JSON under `data`, together with the native location lookup and request metadata. They do not normalize, average, rank, or fuse providers.

Provider implementations are kept separate:

- `src/qweather.ts`: QWeather schema and requests.
- `src/openmeteo.ts`: Open-Meteo schema and requests.
- `src/index.ts`: registers the two peer tools with OpenClaw.

## Configuration

QWeather requires its dedicated API Host and an API key:

```text
QWEATHER_API_HOST=abc1234xyz.def.qweatherapi.com
QWEATHER_API_KEY=replace-me
```

Open-Meteo works without credentials for its non-commercial public API. Commercial deployments may set `OPEN_METEO_API_KEY` and override the two API origins through plugin configuration or environment variables.

There is no UI, frontend application, normalization layer, or fusion layer in this package.

## Development

```powershell
npm.cmd ci
npm.cmd test
npm.cmd run plugin:validate
```
