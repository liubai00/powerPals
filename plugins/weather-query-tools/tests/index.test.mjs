import assert from "node:assert/strict";
import test from "node:test";
import { getToolPluginMetadata } from "openclaw/plugin-sdk/tool-plugin";
import entry, { queryOpenMeteo, queryQWeather } from "../dist/index.js";

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch(...responses) {
  const calls = [];
  const fetcher = async (input, init) => {
    calls.push([input, init]);
    const response = responses.shift();
    assert.ok(response, "unexpected fetch call");
    return response;
  };
  return { calls, fetcher };
}

test("plugin exposes only the two peer query tools", () => {
  assert.deepEqual(
    getToolPluginMetadata(entry)?.tools.map((tool) => tool.name),
    ["qweather_query", "openmeteo_query"],
  );
});

test("QWeather resolves a place and returns native current data", async () => {
  const lookup = {
    code: "200",
    location: [
      {
        name: "深圳",
        id: "101280601",
        lat: "22.55",
        lon: "114.09",
        tz: "Asia/Shanghai",
      },
    ],
  };
  const weather = {
    code: "200",
    updateTime: "2026-08-12T12:00+08:00",
    now: { temp: "31", text: "多云" },
  };
  const { calls, fetcher } = mockFetch(jsonResponse(lookup), jsonResponse(weather));

  const result = await queryQWeather(
    { location: "深圳", kind: "current" },
    {
      qweatherApiHost: "weather.example.qweatherapi.com",
      qweatherApiKey: "test-key",
    },
    undefined,
    fetcher,
  );

  assert.equal(result.source, "QWeather");
  assert.deepEqual(result.locationLookup, lookup);
  assert.deepEqual(result.data, weather);
  assert.equal(calls.length, 2);

  const lookupUrl = new URL(String(calls[0][0]));
  assert.equal(lookupUrl.pathname, "/geo/v2/city/lookup");
  assert.equal(lookupUrl.searchParams.get("location"), "深圳");

  const weatherUrl = new URL(String(calls[1][0]));
  assert.equal(weatherUrl.pathname, "/v7/weather/now");
  assert.equal(weatherUrl.searchParams.get("location"), "101280601");
  assert.equal(calls[1][1].headers["X-QW-Api-Key"], "test-key");
});

test("QWeather uses resolved coordinates for official warnings", async () => {
  const { calls, fetcher } = mockFetch(
    jsonResponse({
      code: "200",
      location: [{ id: "101010100", lat: "39.92", lon: "116.41" }],
    }),
    jsonResponse({ metadata: { zeroResult: true }, alerts: [] }),
  );

  await queryQWeather(
    { location: "北京", kind: "warning" },
    { qweatherApiHost: "https://weather.example.com", qweatherApiKey: "test-key" },
    undefined,
    fetcher,
  );

  const warningUrl = new URL(String(calls[1][0]));
  assert.equal(warningUrl.pathname, "/weatheralert/v1/current/39.92/116.41");
  assert.equal(warningUrl.searchParams.get("localTime"), "true");
});

test("QWeather fails clearly when credentials are missing", async () => {
  const previousHost = process.env.QWEATHER_API_HOST;
  const previousKey = process.env.QWEATHER_API_KEY;
  delete process.env.QWEATHER_API_HOST;
  delete process.env.QWEATHER_API_KEY;
  try {
    await assert.rejects(
      queryQWeather({ location: "北京", kind: "current" }),
      /QWeather is not configured/,
    );
  } finally {
    if (previousHost === undefined) delete process.env.QWEATHER_API_HOST;
    else process.env.QWEATHER_API_HOST = previousHost;
    if (previousKey === undefined) delete process.env.QWEATHER_API_KEY;
    else process.env.QWEATHER_API_KEY = previousKey;
  }
});

test("Open-Meteo geocodes a place and returns native daily data", async () => {
  const lookup = {
    results: [
      {
        name: "上海",
        latitude: 31.22222,
        longitude: 121.45806,
        timezone: "Asia/Shanghai",
      },
    ],
  };
  const forecast = {
    daily_units: { temperature_2m_max: "°C" },
    daily: { time: ["2026-08-12"], temperature_2m_max: [33.1] },
  };
  const { calls, fetcher } = mockFetch(jsonResponse(lookup), jsonResponse(forecast));

  const result = await queryOpenMeteo(
    { location: "上海", kind: "daily", forecastDays: 3 },
    {},
    undefined,
    fetcher,
  );

  assert.equal(result.source, "Open-Meteo");
  assert.deepEqual(result.locationLookup, lookup);
  assert.deepEqual(result.data, forecast);

  const forecastUrl = new URL(String(calls[1][0]));
  assert.equal(forecastUrl.searchParams.get("forecast_days"), "3");
  assert.equal(forecastUrl.searchParams.get("timezone"), "Asia/Shanghai");
  assert.equal(forecastUrl.searchParams.has("daily"), true);
  assert.equal(forecastUrl.searchParams.has("hourly"), false);
  assert.equal(forecastUrl.searchParams.has("current"), false);
});

test("Open-Meteo accepts coordinates without geocoding", async () => {
  const { calls, fetcher } = mockFetch(
    jsonResponse({ current: { temperature_2m: 30 } }),
  );

  const result = await queryOpenMeteo(
    {
      latitude: 22.55,
      longitude: 114.09,
      kind: "current",
      timezone: "Asia/Shanghai",
    },
    {},
    undefined,
    fetcher,
  );

  assert.equal(calls.length, 1);
  assert.equal(result.locationLookup, null);
  const forecastUrl = new URL(String(calls[0][0]));
  assert.equal(forecastUrl.hostname, "api.open-meteo.com");
  assert.equal(forecastUrl.searchParams.has("current"), true);
});

test("Open-Meteo retries a transient rate limit response", async () => {
  const limited = new Response(JSON.stringify({ reason: "Too many concurrent requests" }), {
    status: 429,
    headers: { "content-type": "application/json", "retry-after": "0" },
  });
  const { calls, fetcher } = mockFetch(
    limited,
    jsonResponse({ current: { temperature_2m: 30 } }),
  );

  const result = await queryOpenMeteo(
    {
      latitude: 22.55,
      longitude: 114.09,
      kind: "current",
      timezone: "Asia/Shanghai",
    },
    {},
    undefined,
    fetcher,
  );

  assert.equal(calls.length, 2);
  assert.deepEqual(result.data, { current: { temperature_2m: 30 } });
});

test("Open-Meteo serializes concurrent tool invocations", async () => {
  let active = 0;
  let maximumActive = 0;
  const fetcher = async () => {
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    await new Promise((resolve) => setImmediate(resolve));
    active -= 1;
    return jsonResponse({ current: { temperature_2m: 30 } });
  };

  await Promise.all([
    queryOpenMeteo({ latitude: 22.55, longitude: 114.09, kind: "current" }, {}, undefined, fetcher),
    queryOpenMeteo({ latitude: 31.23, longitude: 121.47, kind: "current" }, {}, undefined, fetcher),
  ]);

  assert.equal(maximumActive, 1);
});

test("Open-Meteo requires both coordinate values", async () => {
  await assert.rejects(
    queryOpenMeteo({ latitude: 22.55, kind: "current" }),
    /latitude and longitude together/,
  );
});
