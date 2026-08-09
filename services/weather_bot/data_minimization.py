from __future__ import annotations

import re

from services.weather_bot.models import ForecastSummary, WeatherSubmission


_METADATA_ONLY_WEATHER_LABEL = "未持久化（来源策略仅允许元数据）"
_LOCATION_PROVENANCE_FIELDS = (
    "source",
    "source_provider",
    "source_url",
    "retrieved_at",
    "content_sha256",
    "attribution",
    "retention_policy",
)
_DATED_TASK_SUFFIX_RE = re.compile(r"-\d{8}-DAYAHEAD-")


def minimize_submission_for_storage(submission: WeatherSubmission) -> WeatherSubmission:
    """Return a persistence-safe copy without provider or aggregate point series.

    A single metadata-only source makes the combined derivative non-storable,
    because an aggregate cannot reliably subtract that source after the fact.
    """

    used = set(submission.aggregated_forecast.providers_used)
    used_results = [item for item in submission.provider_results if item.provider in used]
    metadata_only = any(item.retention_policy == "metadata_only" for item in used_results)
    providers = [
        item.model_copy(update={"points": [], "daily": {}, "raw": None})
        for item in submission.provider_results
    ]

    if metadata_only:
        summary = ForecastSummary(
            max_temperature=None,
            min_temperature=None,
            rain_probability=None,
            wind_speed=None,
            cloud_cover=None,
            main_weather=_METADATA_ONLY_WEATHER_LABEL,
            high_risk_period=_METADATA_ONLY_WEATHER_LABEL,
        )
        payload_summary: dict = {}
        confidence: dict = {}
        key_factors: list[str] = []
        risk_notes: list[str] = []
        explanation = submission.explanation.model_copy(
            update={"key_factors": [], "risk_notes": [], "business_readable_summary": ""}
        )
    else:
        summary = submission.aggregated_forecast.summary.model_copy(deep=True)
        payload_summary = dict(submission.payload.summary)
        confidence = dict(submission.confidence)
        key_factors = list(submission.key_factors)
        risk_notes = list(submission.risk_notes)
        explanation = submission.explanation.model_copy(deep=True)

    return submission.model_copy(
        update={
            "task_id": _minimize_task_id_for_location(
                submission.task_id,
                submission.scope.location,
            ),
            "scope": submission.scope.model_copy(
                update={
                    "location": _minimize_location_for_storage(
                        submission.scope.location
                    )
                }
            ),
            "provider_results": providers,
            "aggregated_forecast": submission.aggregated_forecast.model_copy(
                update={"points": [], "summary": summary}
            ),
            "payload": submission.payload.model_copy(
                update={"values": [], "summary": payload_summary}
            ),
            "confidence": confidence,
            "key_factors": key_factors,
            "risk_notes": risk_notes,
            "explanation": explanation,
        }
    )


def _minimize_location_for_storage(location: dict) -> dict:
    copied = dict(location or {})
    if not copied.get("source_provider") or copied.get("retention_policy") != "metadata_only":
        return copied
    return {
        key: copied[key]
        for key in _LOCATION_PROVENANCE_FIELDS
        if copied.get(key) is not None
    }


def _minimize_task_id_for_location(task_id: str, location: dict) -> str:
    copied = dict(location or {})
    if not copied.get("source_provider") or copied.get("retention_policy") != "metadata_only":
        return task_id
    prefix, separator, coordinate_suffix = task_id.partition("-COORD-")
    dated_suffix = _DATED_TASK_SUFFIX_RE.search(coordinate_suffix)
    if not separator or dated_suffix is None:
        return task_id
    content_hash = str(copied.get("content_sha256") or "")
    provenance_token = content_hash[:12] if re.fullmatch(r"[0-9a-fA-F]{64}", content_hash) else "metadata-only"
    return (
        f"{prefix}-EXTERNAL-GEO-{provenance_token}"
        f"{coordinate_suffix[dated_suffix.start():]}"
    )
