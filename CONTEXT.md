# PowerPals Weather Intelligence

This context describes how PowerPals turns weather requests and objective forecast outcomes into safe, reviewable improvements.

## Language

**Controlled Learning**:
A versioned improvement loop that produces evidence-backed candidates from deterministic replay and objective forecast verification, while requiring explicit approval before any behavior change.
_Avoid_: Self-evolution, autonomous self-modification

**Replay Case**:
A deterministic example containing an input scope and an expected intent or entity result, used to detect behavioral regressions without contacting external users.
_Avoid_: User feedback, training chat

**Learning Signal**:
A privacy-minimized observation of a low-confidence forecast, unavailable provider, failed replay, or unusable forecast that may justify investigation.
_Avoid_: Conversation transcript

**Forecast Snapshot**:
An immutable, retention-bounded evidence record containing only policy-allowed derived daily features plus run, valid-time, source URL/hash and retrieval metadata. Raw provider payloads and hourly point series are not retained by controlled learning.
_Avoid_: Live forecast cache

**Objective Verification**:
Scoring a forecast snapshot against a dated reference-weather dataset after the target date, with its source and retrieval time preserved; model/reanalysis grids are not treated as station observations.
_Avoid_: Model self-critique

**Improvement Candidate**:
A non-executable, evidence-linked proposal for a rule, prompt, location vocabulary, context policy, or provider weighting change.
_Avoid_: Automatic patch, automatic release

**Candidate Decision**:
An explicit administrative approval, rejection, or rollback recorded against an improvement candidate; approval alone does not alter runtime behavior.
_Avoid_: Automatic adoption

**Power-weather Analysis Area**:
A configured geographic/electrical reporting scope used to group representative weather points. It is not automatically an independent trading market.
_Avoid_: Market (when only the weather-analysis scope is known)

**Representative Point**:
A named city or coordinate used as a transparent proxy for one analysis area. One point must not be described as province-wide observation.
_Avoid_: Province-wide truth

**Weather-side Proxy**:
A weather-derived pressure or resource signal, such as cooling pressure, shortwave-radiation resource, 10-metre surface-wind resource, or forecast-complexity proxy. It is not actual load, generation, supply-demand balance or price.
_Avoid_: Load forecast, generation forecast, price signal

**Forecast Version Comparison**:
A comparison between distinct forecast runs for the same analysis area, target valid time, proxy-method version and weight version.
_Avoid_: Comparing different target dates as a forecast revision
