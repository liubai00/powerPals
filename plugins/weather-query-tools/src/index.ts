import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

import {
  openMeteoQuerySchema,
  queryOpenMeteo,
  type OpenMeteoConfig,
} from "./openmeteo.js";
import {
  queryQWeather,
  qweatherQuerySchema,
  type QWeatherConfig,
} from "./qweather.js";

export { queryOpenMeteo } from "./openmeteo.js";
export type { OpenMeteoConfig, OpenMeteoQuery } from "./openmeteo.js";
export { queryQWeather } from "./qweather.js";
export type { QWeatherConfig, QWeatherQuery } from "./qweather.js";

export type WeatherPluginConfig = OpenMeteoConfig & QWeatherConfig;

const configSchema = Type.Object({
  qweatherApiHost: Type.Optional(
    Type.String({ description: "Dedicated QWeather API Host; may omit https://." }),
  ),
  qweatherApiKey: Type.Optional(
    Type.String({ description: "QWeather API key. Prefer QWEATHER_API_KEY in runtime env." }),
  ),
  openMeteoForecastHost: Type.Optional(
    Type.String({ description: "Open-Meteo forecast API origin override." }),
  ),
  openMeteoGeocodingHost: Type.Optional(
    Type.String({ description: "Open-Meteo geocoding API origin override." }),
  ),
  openMeteoApiKey: Type.Optional(
    Type.String({ description: "Optional Open-Meteo commercial customer API key." }),
  ),
  requestTimeoutMs: Type.Optional(
    Type.Integer({ minimum: 1_000, maximum: 60_000, description: "Defaults to 15 seconds." }),
  ),
}, { additionalProperties: false });

export default defineToolPlugin({
  id: "weather-query-tools",
  name: "Weather Query Tools",
  description: "Native QWeather and Open-Meteo query tools for a single OpenClaw agent.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "qweather_query",
      label: "QWeather Query",
      description:
        "Query native QWeather location, current, forecast, or official alert data. Returns provider-native JSON without cross-source normalization or fusion.",
      parameters: qweatherQuerySchema,
      async execute(params, config, context) {
        context.signal?.throwIfAborted();
        return queryQWeather(params, config, context.signal);
      },
    }),
    tool({
      name: "openmeteo_query",
      label: "Open-Meteo Query",
      description:
        "Query native Open-Meteo current or forecast data by place or coordinates. Returns provider-native JSON without cross-source normalization or fusion.",
      parameters: openMeteoQuerySchema,
      async execute(params, config, context) {
        context.signal?.throwIfAborted();
        return queryOpenMeteo(params, config, context.signal);
      },
    }),
  ],
});
