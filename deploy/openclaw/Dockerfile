ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.7.1-2

FROM node:24-bookworm-slim AS weather-plugin-builder

WORKDIR /build/weather-query-tools
COPY plugins/weather-query-tools/package.json plugins/weather-query-tools/package-lock.json ./
RUN npm ci
COPY plugins/weather-query-tools/tsconfig.json ./
COPY plugins/weather-query-tools/src ./src
COPY plugins/weather-query-tools/openclaw.plugin.json ./
RUN npm run build && npm prune --omit=dev

FROM ${OPENCLAW_IMAGE}

USER root
COPY --from=weather-plugin-builder --chown=node:node /build/weather-query-tools /opt/weather-query-tools
COPY --chown=node:node config/workflows /opt/weather-agent/config/workflows
USER node
