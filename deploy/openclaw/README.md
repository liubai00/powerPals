# Linux OpenClaw deployment

This package runs the weather agent as an isolated Docker Compose project. It
does not reuse or restart another OpenClaw deployment on the same host.

## Persistent inputs

Keep these paths outside a release checkout:

- `WEATHER_AGENT_STATE_DIR`: OpenClaw config, plugin packages, sessions and state.
- `WEATHER_AGENT_WORKSPACE_DIR`: agent identity, memory and runtime briefings.
- `WEATHER_AGENT_ENV_FILE`: runtime secrets; mode `0600`, never committed.

The gateway is published only on host loopback. The default host port is
`18895`; callers on another machine must use an SSH tunnel.

## Build and start

```bash
export WEATHER_AGENT_IMAGE_TAG=<git-commit>
export WEATHER_AGENT_STATE_DIR=/srv/weather-agent-v2/openclaw
export WEATHER_AGENT_WORKSPACE_DIR=/srv/weather-agent-v2/workspace
export WEATHER_AGENT_ENV_FILE=/srv/weather-agent-v2/openclaw/.env
export WEATHER_AGENT_GATEWAY_PORT=18895

docker compose -f deploy/openclaw/compose.yaml build openclaw-gateway
docker compose -f deploy/openclaw/compose.yaml up -d openclaw-gateway
curl -fsS http://127.0.0.1:18895/healthz
curl -fsS http://127.0.0.1:18895/readyz
```

Install the Linux builds of non-bundled OpenClaw plugins into the persistent
state directory before the first production start. Never copy Windows
`node_modules`, `npm`, or `extensions` directories to Linux.

Use `normalize_config.py` to convert an already reviewed live config. The
command requires an explicit ComfyUI decision so that port `8188` cannot be
silently connected to an unrelated service:

```bash
python3 deploy/openclaw/normalize_config.py \
  --input /secure-staging/openclaw.windows.json \
  --output "$WEATHER_AGENT_STATE_DIR/openclaw.json" \
  --disable-comfy
```

## Rollback

Stop this Compose project and restart the previously verified gateway. Keep the
persistent state directory intact so the failed container can be inspected or
forward-fixed. Do not start two gateways with the same Feishu app at once.
