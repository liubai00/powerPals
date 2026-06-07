# ClawHub Weather Skill Review

## Finding

ClawHub has a generic `Weather` skill by `steipete`.

Observed listing details:

- Category: Data & APIs
- Description: current weather and forecasts, no API key required
- Primary sources: `wttr.in` and Open-Meteo
- Security audit: pass
- Current version: v1.0.0
- License: MIT-0

## Fit For PowerPals

The skill is useful as a reference or fallback because it is simple and keyless. It is not enough as the main PowerPals weather bot because it does not handle:

- Feishu event callbacks or cards;
- Feishu Bitable recording;
- QWeather and Caiyun provider integration;
- PowerPals `weather_submission_v1` JSON;
- Shenzhen-only task IDs and scoring workflow;
- community disclaimers and risk notes.

## Decision

Do not install the third-party skill directly into the production bot. Keep our own auditable OpenClaw skill in:

```text
openclaw/skills/powerpals-shenzhen-weather/
```

The local skill calls our FastAPI service and preserves the PowerPals data contract.

## Safe Use

If the ClawHub skill is tested later, install it only in a sandboxed OpenClaw workspace and do not grant it secrets, Feishu credentials, or filesystem write access. Treat it as a weather lookup helper, not as the official PowerPals submission engine.
