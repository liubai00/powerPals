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
An immutable copy of provider forecasts and their location/date scope, retained so it can later be compared with observed weather.
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
