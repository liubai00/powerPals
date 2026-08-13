import { setTimeout as delay } from "node:timers/promises";
import { Type } from "typebox";

type Fetcher = (input: string | URL, init?: RequestInit) => Promise<Response>;

const MAX_RATE_LIMIT_RETRIES = 2;
let openMeteoQueue: Promise<void> = Promise.resolve();

export interface OpenMeteoConfig {
  openMeteoForecastHost?: string;
  openMeteoGeocodingHost?: string;
  openMeteoApiKey?: string;
  requestTimeoutMs?: number;
}

export interface OpenMeteoQuery {
  location?: string;
  latitude?: number;
  longitude?: number;
  kind: "current" | "daily" | "hourly" | "combined";
  forecastDays?: number;
  countryCode?: string;
  language?: string;
  timezone?: string;
  temperatureUnit?: "celsius" | "fahrenheit";
  windSpeedUnit?: "kmh" | "ms" | "mph" | "kn";
  precipitationUnit?: "mm" | "inch";
  models?: string[];
  currentFields?: string[];
  hourlyFields?: string[];
  dailyFields?: string[];
}

export const openMeteoQuerySchema = Type.Object({
  location: Type.Optional(
    Type.String({
      minLength: 1,
      description: "Place name. Not needed when latitude and longitude are supplied.",
    }),
  ),
  latitude: Type.Optional(Type.Number({ minimum: -90, maximum: 90 })),
  longitude: Type.Optional(Type.Number({ minimum: -180, maximum: 180 })),
  kind: Type.Union(
    [
      Type.Literal("current"),
      Type.Literal("daily"),
      Type.Literal("hourly"),
      Type.Literal("combined"),
    ],
    { description: "Which native Open-Meteo forecast sections to request." },
  ),
  forecastDays: Type.Optional(
    Type.Integer({ minimum: 1, maximum: 16, description: "Defaults to 7." }),
  ),
  countryCode: Type.Optional(
    Type.String({
      minLength: 2,
      maxLength: 2,
      description: "ISO 3166-1 alpha-2 filter for place-name lookup.",
    }),
  ),
  language: Type.Optional(
    Type.String({ description: "Geocoding response language. Defaults to zh." }),
  ),
  timezone: Type.Optional(
    Type.String({ description: "IANA time zone or auto. Defaults to the selected place." }),
  ),
  temperatureUnit: Type.Optional(
    Type.Union([Type.Literal("celsius"), Type.Literal("fahrenheit")]),
  ),
  windSpeedUnit: Type.Optional(
    Type.Union([
      Type.Literal("kmh"),
      Type.Literal("ms"),
      Type.Literal("mph"),
      Type.Literal("kn"),
    ]),
  ),
  precipitationUnit: Type.Optional(
    Type.Union([Type.Literal("mm"), Type.Literal("inch")]),
  ),
  models: Type.Optional(
    Type.Array(Type.String({ minLength: 1 }), {
      minItems: 1,
      maxItems: 8,
      description: "Optional native Open-Meteo model identifiers.",
    }),
  ),
  currentFields: Type.Optional(
    Type.Array(Type.String({ minLength: 1 }), { minItems: 1, maxItems: 30 }),
  ),
  hourlyFields: Type.Optional(
    Type.Array(Type.String({ minLength: 1 }), { minItems: 1, maxItems: 30 }),
  ),
  dailyFields: Type.Optional(
    Type.Array(Type.String({ minLength: 1 }), { minItems: 1, maxItems: 30 }),
  ),
}, { additionalProperties: false });

const DEFAULT_CURRENT_FIELDS = [
  "temperature_2m",
  "relative_humidity_2m",
  "apparent_temperature",
  "precipitation",
  "weather_code",
  "cloud_cover",
  "pressure_msl",
  "wind_speed_10m",
  "wind_direction_10m",
];

const DEFAULT_HOURLY_FIELDS = [
  "temperature_2m",
  "apparent_temperature",
  "precipitation_probability",
  "precipitation",
  "weather_code",
  "cloud_cover",
  "wind_speed_10m",
  "wind_gusts_10m",
];

const DEFAULT_DAILY_FIELDS = [
  "weather_code",
  "temperature_2m_max",
  "temperature_2m_min",
  "apparent_temperature_max",
  "apparent_temperature_min",
  "precipitation_sum",
  "precipitation_probability_max",
  "wind_speed_10m_max",
  "wind_gusts_10m_max",
  "sunrise",
  "sunset",
];

function nonEmpty(value: string | undefined): string | undefined {
  const normalized = value?.trim();
  return normalized ? normalized : undefined;
}

function httpsOrigin(value: string, label: string): string {
  const candidate = value.includes("://") ? value : `https://${value}`;
  const url = new URL(candidate);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    (url.pathname !== "/" && url.pathname !== "") ||
    url.search ||
    url.hash
  ) {
    throw new Error(`${label} must be a plain HTTPS origin.`);
  }
  return url.origin;
}

function combinedSignal(signal: AbortSignal | undefined, timeoutMs: number): AbortSignal {
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal;
}

async function fetchJson(
  url: URL,
  init: RequestInit,
  provider: string,
  fetcher: Fetcher,
): Promise<Record<string, unknown>> {
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetcher(url, init);
    const text = await response.text();
    if (response.status === 429 && attempt < MAX_RATE_LIMIT_RETRIES) {
      const retryAfter = response.headers.get("retry-after");
      const retryAfterSeconds = retryAfter === null ? Number.NaN : Number(retryAfter);
      const waitMs = Number.isFinite(retryAfterSeconds) && retryAfterSeconds >= 0
        ? Math.min(retryAfterSeconds * 1_000, 5_000)
        : Math.min(1_000 * (2 ** attempt), 4_000);
      await delay(waitMs, undefined, { signal: init.signal ?? undefined });
      continue;
    }

    let payload: unknown;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      throw new Error(`${provider} returned a non-JSON response (HTTP ${response.status}).`);
    }

    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && "reason" in payload
          ? `: ${String((payload as { reason?: unknown }).reason)}`
          : "";
      throw new Error(`${provider} request failed with HTTP ${response.status}${detail}`);
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error(`${provider} returned an unexpected JSON payload.`);
    }
    return payload as Record<string, unknown>;
  }
}

function runSerialized<T>(operation: () => Promise<T>): Promise<T> {
  const result = openMeteoQueue.then(operation, operation);
  openMeteoQueue = result.then(() => undefined, () => undefined);
  return result;
}

function openMeteoOrigin(value: string | undefined, fallback: string, label: string): string {
  return httpsOrigin(nonEmpty(value) ?? fallback, label);
}

function arrayParam(url: URL, name: string, values: string[]): void {
  url.searchParams.set(name, values.join(","));
}

async function queryOpenMeteoInternal(
  query: OpenMeteoQuery,
  config: OpenMeteoConfig = {},
  signal?: AbortSignal,
  fetcher: Fetcher = globalThis.fetch,
): Promise<Record<string, unknown>> {
  const hasLatitude = query.latitude !== undefined;
  const hasLongitude = query.longitude !== undefined;
  if (hasLatitude !== hasLongitude) {
    throw new Error("Open-Meteo requires latitude and longitude together.");
  }
  if (!hasLatitude && !nonEmpty(query.location)) {
    throw new Error("Open-Meteo requires a location name or latitude and longitude.");
  }

  const timeoutMs = config.requestTimeoutMs ?? 15_000;
  const requestSignal = combinedSignal(signal, timeoutMs);
  const language = nonEmpty(query.language) ?? "zh";
  const geocodingOrigin = openMeteoOrigin(
    config.openMeteoGeocodingHost ?? process.env.OPEN_METEO_GEOCODING_HOST,
    "https://geocoding-api.open-meteo.com",
    "Open-Meteo geocoding host",
  );
  const forecastOrigin = openMeteoOrigin(
    config.openMeteoForecastHost ?? process.env.OPEN_METEO_FORECAST_HOST,
    "https://api.open-meteo.com",
    "Open-Meteo forecast host",
  );

  let locationLookup: Record<string, unknown> | null = null;
  let selectedLocation: Record<string, unknown>;
  if (hasLatitude && hasLongitude) {
    selectedLocation = {
      name: nonEmpty(query.location) ?? "coordinates",
      latitude: query.latitude,
      longitude: query.longitude,
      timezone: nonEmpty(query.timezone) ?? "auto",
    };
  } else {
    const lookupUrl = new URL("/v1/search", geocodingOrigin);
    lookupUrl.searchParams.set("name", query.location!.trim());
    lookupUrl.searchParams.set("count", "5");
    lookupUrl.searchParams.set("language", language.toLowerCase());
    lookupUrl.searchParams.set("format", "json");
    if (nonEmpty(query.countryCode)) {
      lookupUrl.searchParams.set("countryCode", query.countryCode!.trim().toUpperCase());
    }
    const apiKey = nonEmpty(config.openMeteoApiKey) ?? nonEmpty(process.env.OPEN_METEO_API_KEY);
    if (apiKey) {
      lookupUrl.searchParams.set("apikey", apiKey);
    }

    locationLookup = await fetchJson(
      lookupUrl,
      { headers: { Accept: "application/json" }, signal: requestSignal },
      "Open-Meteo geocoding",
      fetcher,
    );
    const results = Array.isArray(locationLookup.results) ? locationLookup.results : [];
    const selected = results[0];
    if (!selected || typeof selected !== "object") {
      throw new Error(`Open-Meteo found no location for "${query.location}".`);
    }
    selectedLocation = selected as Record<string, unknown>;
  }

  const latitude = Number(selectedLocation.latitude);
  const longitude = Number(selectedLocation.longitude);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
    throw new Error("Open-Meteo location lookup returned invalid coordinates.");
  }

  const forecastUrl = new URL("/v1/forecast", forecastOrigin);
  forecastUrl.searchParams.set("latitude", String(latitude));
  forecastUrl.searchParams.set("longitude", String(longitude));
  forecastUrl.searchParams.set("forecast_days", String(query.forecastDays ?? 7));
  forecastUrl.searchParams.set(
    "timezone",
    nonEmpty(query.timezone) ?? String(selectedLocation.timezone ?? "auto"),
  );
  forecastUrl.searchParams.set("temperature_unit", query.temperatureUnit ?? "celsius");
  forecastUrl.searchParams.set("wind_speed_unit", query.windSpeedUnit ?? "kmh");
  forecastUrl.searchParams.set("precipitation_unit", query.precipitationUnit ?? "mm");

  if (query.kind === "current" || query.kind === "combined") {
    arrayParam(forecastUrl, "current", query.currentFields ?? DEFAULT_CURRENT_FIELDS);
  }
  if (query.kind === "hourly" || query.kind === "combined") {
    arrayParam(forecastUrl, "hourly", query.hourlyFields ?? DEFAULT_HOURLY_FIELDS);
  }
  if (query.kind === "daily" || query.kind === "combined") {
    arrayParam(forecastUrl, "daily", query.dailyFields ?? DEFAULT_DAILY_FIELDS);
  }
  if (query.models?.length) {
    arrayParam(forecastUrl, "models", query.models);
  }
  const apiKey = nonEmpty(config.openMeteoApiKey) ?? nonEmpty(process.env.OPEN_METEO_API_KEY);
  if (apiKey) {
    forecastUrl.searchParams.set("apikey", apiKey);
  }

  const data = await fetchJson(
    forecastUrl,
    { headers: { Accept: "application/json" }, signal: requestSignal },
    "Open-Meteo forecast",
    fetcher,
  );
  if (data.error === true) {
    throw new Error(`Open-Meteo forecast failed: ${String(data.reason ?? "unknown error")}.`);
  }

  return {
    source: "Open-Meteo",
    retrievedAt: new Date().toISOString(),
    request: { ...query, language },
    selectedLocation,
    locationLookup,
    data,
  };
}

export function queryOpenMeteo(
  query: OpenMeteoQuery,
  config: OpenMeteoConfig = {},
  signal?: AbortSignal,
  fetcher: Fetcher = globalThis.fetch,
): Promise<Record<string, unknown>> {
  return runSerialized(() => queryOpenMeteoInternal(query, config, signal, fetcher));
}
