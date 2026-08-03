import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from utils.embedding_optimization import PLAN, percentile


@dataclass(frozen=True)
class AnalysisContract:
    summary_file: str = "summary.json"
    requests_file: str = "requests.jsonl"
    analysis_json_file: str = "analysis.json"
    analysis_markdown_file: str = "analysis.md"
    success_status: int = 200
    throttled_status: int = 429
    unavailable_status: int = 503
    pool_backend_type: str = "Pool"
    all_targets: str = "all"


@dataclass(frozen=True)
class LogFields:
    status_code: str = "status_code"
    sequence: str = "sequence"
    duration_ms: str = "duration_ms"
    start_offset_seconds: str = "start_offset_seconds"
    input_count: str = "input_count"
    correlated_inputs: str = "correlated_inputs"
    prompt_tokens: str = "prompt_tokens"
    estimated_tokens: str = "estimated_tokens"
    retry_after: str = "retry_after"
    error_message: str = "error_message"
    backend_id: str = "backend_id"
    backend_type: str = "backend_type"
    backend_region: str = "backend_region"
    rate_limit_limit_requests: str = "rate_limit_limit_requests"
    rate_limit_remaining_requests: str = "rate_limit_remaining_requests"
    rate_limit_reset_requests: str = "rate_limit_reset_requests"
    rate_limit_limit_tokens: str = "rate_limit_limit_tokens"
    rate_limit_remaining_tokens: str = "rate_limit_remaining_tokens"
    rate_limit_reset_tokens: str = "rate_limit_reset_tokens"


@dataclass(frozen=True)
class SummaryFields:
    mode: str = "mode"
    target: str = "target"
    requests: str = "requests"
    configured_target_tpm: str = "configured_target_tpm"
    window_tpm: str = "window_tpm"
    observed_tpm: str = "observed_tpm"
    steady_state_tpm: str = "steady_state_tpm"
    comparisons: str = "comparisons"
    optimization_plan: str = "optimization_plan"


@dataclass(frozen=True)
class AnalysisOutputFields:
    classification: str = "classification"
    latency_ms: str = "latency_ms"
    p50: str = "p50"
    optimization_scorecard: str = "optimization_scorecard"
    item_capacity_fill_ratio: str = "item_capacity_fill_ratio"
    estimated_token_fill_ratio: str = "estimated_token_fill_ratio"
    actual_token_fill_ratio: str = "actual_token_fill_ratio"
    logical_inputs_per_http_request: str = "logical_inputs_per_http_request"
    steady_state_capacity_utilization: str = "steady_state_capacity_utilization"


class ThrottleClassification(StrEnum):
    NOT_THROTTLED = "not-throttled"
    RPM_EXPLICIT = "rpm-explicit"
    TPM_EXPLICIT = "tpm-explicit"
    RPM_LIKELY = "rpm-likely"
    TPM_LIKELY = "tpm-likely"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AnalysisMessages:
    status_not_throttled: str = "HTTP status is not 429"
    server_error_message: str = "server error message"
    request_counter_exhausted: str = "request counter exhausted"
    token_counter_exhausted: str = "token counter exhausted"
    both_counters_exhausted: str = "both counters exhausted"
    insufficient_evidence: str = "insufficient response evidence"
    pool_only_context: str = (
        "Pool-only context does not identify the selected backend member."
    )
    no_backend_context: str = "No backend context was recorded."
    member_context: str = "Backend context contains member-level values."
    breaker_interpretation: str = (
        "429 followed by 503 is consistent with breaker or pool unavailability, "
        "but status logs alone do not prove breaker state."
    )
    throttle_interpretation: str = (
        "Only explicit server wording proves a limiter. Counter-based labels are "
        "inferences; HTTP 429 alone remains unknown."
    )


@dataclass(frozen=True)
class ThrottlePhrases:
    request_rate: tuple[str, ...] = (
        "call rate",
        "request rate",
        "requests per",
    )
    token_rate: tuple[str, ...] = (
        "token rate",
        "tokens per",
        "token limit",
    )


ANALYSIS = AnalysisContract()
FIELDS = LogFields()
SUMMARY = SummaryFields()
OUTPUT = AnalysisOutputFields()
MESSAGES = AnalysisMessages()
PHRASES = ThrottlePhrases()


def rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(value)
    return records


def read_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run_dir = path if path.is_dir() else path.parent
    summary_path = path if path.is_file() else run_dir / ANALYSIS.summary_file
    if not summary_path.exists():
        raise FileNotFoundError(f"Missing run summary: {summary_path}")
    summary = read_json(summary_path)
    records = read_jsonl(run_dir / ANALYSIS.requests_file)
    if not records and isinstance(summary.get(SUMMARY.requests), list):
        records = [
            record
            for record in summary[SUMMARY.requests]
            if isinstance(record, dict)
        ]
    return summary, records


def numeric_values(records: list[dict[str, Any]], name: str) -> list[float]:
    return [
        float(record[name])
        for record in records
        if isinstance(record.get(name), (int, float))
    ]


def status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(
        str(record[FIELDS.status_code])
        for record in records
        if isinstance(record.get(FIELDS.status_code), int)
    )
    return dict(sorted(counts.items()))


def value_counts(records: list[dict[str, Any]], name: str) -> dict[str, int]:
    counts = Counter(
        str(record[name])
        for record in records
        if record.get(name) not in (None, "", "n/a")
    )
    return dict(sorted(counts.items()))


def numeric_value(record: dict[str, Any], name: str) -> float | None:
    value = record.get(name)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_throttle(record: dict[str, Any]) -> dict[str, str]:
    if record.get(FIELDS.status_code) != ANALYSIS.throttled_status:
        return {
            "classification": ThrottleClassification.NOT_THROTTLED,
            "basis": MESSAGES.status_not_throttled,
        }

    message = str(record.get(FIELDS.error_message, "")).casefold()
    if any(phrase in message for phrase in PHRASES.request_rate):
        return {
            "classification": ThrottleClassification.RPM_EXPLICIT,
            "basis": MESSAGES.server_error_message,
        }
    if any(phrase in message for phrase in PHRASES.token_rate):
        return {
            "classification": ThrottleClassification.TPM_EXPLICIT,
            "basis": MESSAGES.server_error_message,
        }

    remaining_requests = numeric_value(record, FIELDS.rate_limit_remaining_requests)
    remaining_tokens = numeric_value(record, FIELDS.rate_limit_remaining_tokens)
    requests_exhausted = remaining_requests is not None and remaining_requests <= 0
    tokens_exhausted = remaining_tokens is not None and remaining_tokens <= 0
    if requests_exhausted and not tokens_exhausted:
        return {
            "classification": ThrottleClassification.RPM_LIKELY,
            "basis": MESSAGES.request_counter_exhausted,
        }
    if tokens_exhausted and not requests_exhausted:
        return {
            "classification": ThrottleClassification.TPM_LIKELY,
            "basis": MESSAGES.token_counter_exhausted,
        }
    if requests_exhausted and tokens_exhausted:
        return {
            "classification": ThrottleClassification.UNKNOWN,
            "basis": MESSAGES.both_counters_exhausted,
        }
    return {
        "classification": ThrottleClassification.UNKNOWN,
        "basis": MESSAGES.insufficient_evidence,
    }


def throttle_analysis(records: list[dict[str, Any]]) -> dict[str, Any]:
    classifications = [
        {FIELDS.sequence: record.get(FIELDS.sequence), **classify_throttle(record)}
        for record in records
        if record.get(FIELDS.status_code) == ANALYSIS.throttled_status
    ]
    return {
        "counts": dict(
            sorted(Counter(item["classification"] for item in classifications).items())
        ),
        "events": classifications,
        "interpretation": MESSAGES.throttle_interpretation,
    }


def rate_limit_evidence(records: list[dict[str, Any]]) -> dict[str, Any]:
    field_names = (
        FIELDS.rate_limit_limit_requests,
        FIELDS.rate_limit_remaining_requests,
        FIELDS.rate_limit_reset_requests,
        FIELDS.rate_limit_limit_tokens,
        FIELDS.rate_limit_remaining_tokens,
        FIELDS.rate_limit_reset_tokens,
    )
    observed = {
        name: counts
        for name in field_names
        if (counts := value_counts(records, name))
    }
    return {
        "observed": observed,
        "request_headers_available": any("requests" in name for name in observed),
        "token_headers_available": any("tokens" in name for name in observed),
    }


def breaker_signals(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda record: record.get(FIELDS.sequence, 0))
    throttled_indexes = [
        index
        for index, record in enumerate(ordered)
        if record.get(FIELDS.status_code) == ANALYSIS.throttled_status
    ]
    unavailable_after_throttle = sum(
        record.get(FIELDS.status_code) == ANALYSIS.unavailable_status
        and any(throttled_index < index for throttled_index in throttled_indexes)
        for index, record in enumerate(ordered)
    )
    return {
        "http_429": len(throttled_indexes),
        "http_503": sum(
            record.get(FIELDS.status_code) == ANALYSIS.unavailable_status
            for record in ordered
        ),
        "http_503_after_429": unavailable_after_throttle,
        "retry_after_seconds": numeric_values(ordered, FIELDS.retry_after),
        "interpretation": MESSAGES.breaker_interpretation,
    }


def analyze_run(path: Path) -> dict[str, Any]:
    summary, records = read_run(path)
    successful_records = [
        record
        for record in records
        if record.get(FIELDS.status_code) == ANALYSIS.success_status
    ]
    durations = numeric_values(successful_records, FIELDS.duration_ms)
    offsets = numeric_values(records, FIELDS.start_offset_seconds)
    intervals = [
        later - earlier
        for earlier, later in zip(offsets, offsets[1:])
        if later >= earlier
    ]
    backend_ids = value_counts(records, FIELDS.backend_id)
    backend_types = value_counts(records, FIELDS.backend_type)
    selected_member_attribution = bool(backend_ids) and not (
        set(backend_types) == {ANALYSIS.pool_backend_type}
    )
    successful = len(successful_records)
    request_count = len(records)
    estimated_batch_tokens = numeric_values(records, FIELDS.estimated_tokens)
    actual_batch_tokens = numeric_values(successful_records, FIELDS.prompt_tokens)
    estimated_total = sum(estimated_batch_tokens)
    actual_total = sum(actual_batch_tokens)
    batch_inputs = numeric_values(successful_records, FIELDS.input_count)
    optimization_plan = summary.get(SUMMARY.optimization_plan, {})
    max_batch_tokens = numeric_value(
        optimization_plan,
        PLAN.max_batch_tokens,
    )
    max_batch_inputs = numeric_value(
        optimization_plan,
        PLAN.max_batch_inputs,
    )
    assigned_tpm = numeric_value(optimization_plan, PLAN.assigned_tpm)
    steady_state_tpm = summary.get(SUMMARY.steady_state_tpm)
    return {
        "source": str(path),
        "mode": summary.get(SUMMARY.mode),
        "target": summary.get(SUMMARY.target, ANALYSIS.all_targets),
        "configured_target_tpm": summary.get(SUMMARY.configured_target_tpm),
        "request_count": request_count,
        "successful_requests": successful,
        "success_rate": round(successful / request_count, 6) if request_count else None,
        "status_counts": status_counts(records),
        "logical_inputs": sum(
            int(
                record.get(
                    FIELDS.correlated_inputs,
                    record.get(FIELDS.input_count, 0),
                )
            )
            for record in records
            if record.get(FIELDS.status_code) == ANALYSIS.success_status
        ),
        "prompt_tokens": sum(
            int(record.get(FIELDS.prompt_tokens, 0))
            for record in records
            if record.get(FIELDS.status_code) == ANALYSIS.success_status
        ),
        "batch_tokens": {
            "estimated_total": estimated_total,
            "actual_total": actual_total,
            "estimate_to_actual_ratio": (
                round(estimated_total / actual_total, 6)
                if actual_total and estimated_total
                else None
            ),
            "estimated": {
                "minimum": rounded(min(estimated_batch_tokens)) if estimated_batch_tokens else None,
                "mean": rounded(statistics.fmean(estimated_batch_tokens)) if estimated_batch_tokens else None,
                "p50": rounded(percentile(estimated_batch_tokens, 50)),
                "p95": rounded(percentile(estimated_batch_tokens, 95)),
                "maximum": rounded(max(estimated_batch_tokens)) if estimated_batch_tokens else None,
            },
            "actual": {
                "minimum": rounded(min(actual_batch_tokens)) if actual_batch_tokens else None,
                "mean": rounded(statistics.fmean(actual_batch_tokens)) if actual_batch_tokens else None,
                "p50": rounded(percentile(actual_batch_tokens, 50)),
                "p95": rounded(percentile(actual_batch_tokens, 95)),
                "maximum": rounded(max(actual_batch_tokens)) if actual_batch_tokens else None,
            },
        },
        "window_tpm": summary.get(
            SUMMARY.window_tpm,
            summary.get(SUMMARY.observed_tpm),
        ),
        "steady_state_tpm": steady_state_tpm,
        "optimization_plan": optimization_plan,
        OUTPUT.optimization_scorecard: {
            "inputs_per_request": {
                "minimum": rounded(min(batch_inputs)) if batch_inputs else None,
                "mean": rounded(statistics.fmean(batch_inputs)) if batch_inputs else None,
                "p50": rounded(percentile(batch_inputs, 50)),
                "p95": rounded(percentile(batch_inputs, 95)),
                "maximum": rounded(max(batch_inputs)) if batch_inputs else None,
            },
            OUTPUT.item_capacity_fill_ratio: (
                round(statistics.fmean(batch_inputs) / max_batch_inputs, 6)
                if batch_inputs and max_batch_inputs
                else None
            ),
            OUTPUT.estimated_token_fill_ratio: (
                round(statistics.fmean(estimated_batch_tokens) / max_batch_tokens, 6)
                if estimated_batch_tokens and max_batch_tokens
                else None
            ),
            OUTPUT.actual_token_fill_ratio: (
                round(statistics.fmean(actual_batch_tokens) / max_batch_tokens, 6)
                if actual_batch_tokens and max_batch_tokens
                else None
            ),
            OUTPUT.logical_inputs_per_http_request: (
                round(
                    sum(batch_inputs) / request_count,
                    6,
                )
                if request_count and batch_inputs
                else None
            ),
            OUTPUT.steady_state_capacity_utilization: (
                round(float(steady_state_tpm) / assigned_tpm, 6)
                if steady_state_tpm is not None and assigned_tpm
                else None
            ),
        },
        "latency_ms": {
            "mean": rounded(statistics.fmean(durations)) if durations else None,
            "p50": rounded(percentile(durations, 50)),
            "p95": rounded(percentile(durations, 95)),
            "p99": rounded(percentile(durations, 99)),
        },
        "request_spacing_seconds": {
            "mean": rounded(statistics.fmean(intervals)) if intervals else None,
            "minimum": rounded(min(intervals)) if intervals else None,
            "maximum": rounded(max(intervals)) if intervals else None,
        },
        "backend_context": {
            "ids": backend_ids,
            "types": backend_types,
            "regions": value_counts(records, FIELDS.backend_region),
            "selected_member_attribution_available": selected_member_attribution,
            "interpretation": (
                MESSAGES.pool_only_context
                if backend_ids and not selected_member_attribution
                else MESSAGES.no_backend_context
                if not backend_ids
                else MESSAGES.member_context
            ),
        },
        "breaker_signals": breaker_signals(records),
        "throttle_analysis": throttle_analysis(records),
        "rate_limit_evidence": rate_limit_evidence(records),
        "comparisons": summary.get(SUMMARY.comparisons),
    }


def markdown_table(runs: list[dict[str, Any]]) -> str:
    lines = [
        "| Source | Mode | Target | Requests | Success | 429 | 503 | Window TPM | Steady TPM | p95 ms |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in runs:
        statuses = run["status_counts"]
        latency = run["latency_ms"]
        lines.append(
            "| "
            + " | ".join(
                [
                    Path(run["source"]).name,
                    str(run["mode"]),
                    str(run["target"]),
                    str(run["request_count"]),
                    str(run["successful_requests"]),
                    str(statuses.get("429", 0)),
                    str(statuses.get("503", 0)),
                    str(run["window_tpm"] or "n/a"),
                    str(run["steady_state_tpm"] or "n/a"),
                    str(latency["p95"] or "n/a"),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_markdown(runs: list[dict[str, Any]]) -> str:
    lines = ["# APIM ADA behavior analysis", "", markdown_table(runs), ""]
    for run in runs:
        context = run["backend_context"]
        breaker = run["breaker_signals"]
        throttles = run["throttle_analysis"]
        rate_limits = run["rate_limit_evidence"]
        batch_tokens = run["batch_tokens"]
        scorecard = run[OUTPUT.optimization_scorecard]
        lines.extend(
            [
                f"## {Path(run['source']).name}",
                "",
                f"- Backend attribution: {context['interpretation']}",
                f"- Backend IDs: `{json.dumps(context['ids'], sort_keys=True)}`",
                f"- Status counts: `{json.dumps(run['status_counts'], sort_keys=True)}`",
                f"- Breaker interpretation: {breaker['interpretation']}",
                f"- Throttle classification: `{json.dumps(throttles['counts'], sort_keys=True)}`",
                f"- Rate-limit headers: `{json.dumps(rate_limits['observed'], sort_keys=True)}`",
                f"- Batch token sizes: `{json.dumps(batch_tokens, sort_keys=True)}`",
                f"- Optimization plan: `{json.dumps(run['optimization_plan'], sort_keys=True)}`",
                f"- Optimization scorecard: `{json.dumps(scorecard, sort_keys=True)}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Interpretation limits",
            "",
            "- A 429 records backend throttling but does not prove that a circuit opened.",
            "- HTTP 429 alone does not distinguish RPM from TPM.",
            "- Only explicit service wording proves a limiter; exhausted counters support a likely classification.",
            "- A later 503 is consistent with no eligible backend, but diagnostics are needed to prove breaker state.",
            "- Pool-level response headers cannot establish primary/secondary routing share.",
            "- Short runs validate behavior and instrumentation, not sustained aggregate TPM.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze secret-free APIM ADA smoke, RPM, and TPM output logs."
    )
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/apim-ada-analysis"),
    )
    args = parser.parse_args()
    analyses = [analyze_run(path) for path in args.runs]
    report = {"runs": analyses}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / ANALYSIS.analysis_json_file).write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown = render_markdown(analyses)
    (args.output / ANALYSIS.analysis_markdown_file).write_text(
        markdown,
        encoding="utf-8",
    )
    print(markdown)


if __name__ == "__main__":
    main()