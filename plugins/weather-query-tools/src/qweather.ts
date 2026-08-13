import { Type } from "typebox";

type Fetcher = (input: string | URL, init?: RequestInit) => Promise<Response>;

export interface QWeatherConfig {
  qweatherApiHost?: string;
  qweatherApiKey?: string;
  requestTimeoutMs?: number;
}

export interface QWeatherQuery {
  location: string;
  kind: "current" | "daily" | "hourly" | "warning";
  days?: 3 | 7 | 10 | 15 | 30;
  hours?: 24 | 72 | 168;
  administrativeArea?: string;
  countryCode?: string;
  language?: string;
  unit?: "metric" | "imperial";
}

export const qweatherQuerySchema = Type.Object({
  location: Type.String({
    minLength: 1,
    description:
      "Place name, QWeather LocationID, or longitude,latitude coordinates.",
  }),
  kind: Type.Union(
    [
      Type.Literal("current"),
      Type.Literal("daily"),
      Type.Literal("hourly"),
      Type.Literal("warning"),
    ],
    { description: "The QWeather dataset to request." },
  ),
  days: Type.Optional(
    Type.Union([
      Type.Literal(3),
      Type.Literal(7),
      Type.Literal(10),
      Type.Literal(15),
      Type.Literal(30),
    ]),
  ),
  hours: Type.Optional(
    Type.Union([Type.Literal(24), Type.Literal(72), Type.Literal(168)]),
  ),
  administrativeArea: Type.Optional(
    Type.String({ description: "Administrative area used to disambiguate a place name." }),
  ),
  countryCode: Type.Optional(
    Type.String({
      minLength: 2,
      maxLength: 2,
      description: "ISO 3166-1 alpha-2 country/region filter, for example CN.",
    }),
  ),
  language: Type.Optional(
    Type.String({ description: "QWeather language code. Defaults to zh." }),
  ),
  unit: Type.Optional(
    Type.Union([Type.Literal("metric"), Type.Literal("imperial")]),
  ),
}, { additionalProperties: false });

function nonEmpty(value: string | undefined): string | undefined {
  const normalized = value?.trim();
  return normalized ? normalized : undefined;
}

function httpsOrigin(value: string): string {
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
    throw new Error("QWeather API Host must be a plain HTTPS origin.");
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
  const response = await fetcher(url, init);
  const text = await response.text();
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

function runtimeConfig(config: QWeatherConfig): {
  origin: string;
  apiKey: string;
  timeoutMs: number;
} {
  const apiHost = nonEmpty(config.qweatherApiHost) ?? nonEmpty(process.env.QWEATHER_API_HOST);
  const apiKey = nonEmpty(config.qweatherApiKey) ?? nonEmpty(process.env.QWEATHER_API_KEY);
  if (!apiHost || !apiKey) {
    throw new Error(
      "QWeather is not configured. Set QWEATHER_API_HOST and QWEATHER_API_KEY in the Gateway environment.",
    );
  }
  return {
    origin: httpsOrigin(apiHost),
    apiKey,
    timeoutMs: config.requestTimeoutMs ?? 15_000,
  };
}

function assertResponseCode(payload: Record<string, unknown>, operation: string): void {
  if (payload.code !== "200") {
    throw new Error(`${operation} failed with QWeather code ${String(payload.code ?? "unknown")}.`);
  }
}

export async function queryQWeather(
  query: QWeatherQuery,
  config: QWeatherConfig = {},
  signal?: AbortSignal,
  fetcher: Fetcher = globalThis.fetch,
): Promise<Record<string, unknown>> {
  const runtime = runtimeConfig(config);
  const language = nonEmpty(query.language) ?? "zh";
  const requestSignal = combinedSignal(signal, runtime.timeoutMs);
  const headers = {
    Accept: "application/json",
    "Accept-Encoding": "gzip",
    "X-QW-Api-Key": runtime.apiKey,
  };

  const lookupUrl = new URL("/geo/v2/city/lookup", runtime.origin);
  lookupUrl.searchParams.set("location", query.location.trim());
  lookupUrl.searchParams.set("number", "5");
  lookupUrl.searchParams.set("lang", language);
  if (nonEmpty(query.administrativeArea)) {
    lookupUrl.searchParams.set("adm", query.administrativeArea!.trim());
  }
  if (nonEmpty(query.countryCode)) {
    lookupUrl.searchParams.set("range", query.countryCode!.trim().toLowerCase());
  }

  const locationLookup = await fetchJson(
    lookupUrl,
    { headers, signal: requestSignal },
    "QWeather GeoAPI",
    fetcher,
  );
  assertResponseCode(locationLookup, "QWeather location lookup");

  const locations = Array.isArray(locationLookup.location) ? locationLookup.location : [];
  const selectedLocation = locations[0];
  if (!selectedLocation || typeof selectedLocation !== "object") {
    throw new Error(`QWeather found no location for "${query.location}".`);
  }

  let dataUrl: URL;
  if (query.kind === "warning") {
    const latitude = String((selectedLocation as Record<string, unknown>).lat ?? "");
    const longitude = String((selectedLocation as Record<string, unknown>).lon ?? "");
    if (!latitude || !longitude) {
      throw new Error("QWeather location lookup did not return coordinates for warning lookup.");
    }
    dataUrl = new URL(
      `/weatheralert/v1/current/${encodeURIComponent(latitude)}/${encodeURIComponent(longitude)}`,
      runtime.origin,
    );
    dataUrl.searchParams.set("localTime", "true");
  } else {
    const locationId = String((selectedLocation as Record<string, unknown>).id ?? "");
    if (!locationId) {
      throw new Error("QWeather location lookup did not return a LocationID.");
    }
    const endpoint =
      query.kind === "current"
        ? "now"
        : query.kind === "daily"
          ? `${query.days ?? 7}d`
          : `${query.hours ?? 24}h`;
    dataUrl = new URL(`/v7/weather/${endpoint}`, runtime.origin);
    dataUrl.searchParams.set("location", locationId);
    dataUrl.searchParams.set("unit", query.unit === "imperial" ? "i" : "m");
  }
  dataUrl.searchParams.set("lang", language);

  const data = await fetchJson(
    dataUrl,
    { headers, signal: requestSignal },
    "QWeather",
    fetcher,
  );
  if (query.kind !== "warning") {
    assertResponseCode(data, "QWeather weather query");
  }

  return {
    source: "QWeather",
    retrievedAt: new Date().toISOString(),
    request: { ...query, language },
    selectedLocation,
    locationLookup,
    data,
  };
}
