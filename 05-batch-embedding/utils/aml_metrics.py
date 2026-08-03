from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Protocol


class MetricLoggingMode(StrEnum):
    DISABLED = "disabled"
    MLFLOW = "mlflow"


@dataclass(frozen=True)
class RequestMeasurement:
    started_seconds: float
    completed_seconds: float
    input_count: int
    estimated_tokens: int
    prompt_tokens: int
    status_code: int | None


@dataclass(frozen=True)
class MetricNames:
    configured_target_tpm: str = "configured_target_tpm"
    configured_target_inputs_per_minute: str = "configured_target_inputs_per_minute"
    attempted_requests: str = "attempted_requests"
    successful_requests: str = "successful_requests"
    failed_requests: str = "failed_requests"
    attempted_logical_inputs: str = "attempted_logical_inputs"
    successful_logical_inputs: str = "successful_logical_inputs"
    prompt_tokens: str = "prompt_tokens"
    request_window_seconds: str = "request_window_seconds"
    attempted_rpm: str = "attempted_rpm"
    successful_rpm: str = "successful_rpm"
    accepted_tpm: str = "accepted_tpm"
    logical_inputs_per_minute: str = "logical_inputs_per_minute"
    success_rate: str = "success_rate"
    throttled_requests: str = "throttled_requests"
    throttle_rate: str = "throttle_rate"
    request_latency_p50_ms: str = "request_latency_p50_ms"
    request_latency_p95_ms: str = "request_latency_p95_ms"
    request_latency_p99_ms: str = "request_latency_p99_ms"
    inputs_per_request: str = "inputs_per_request"
    prompt_tokens_per_successful_request: str = "prompt_tokens_per_successful_request"
    token_ceiling_fill_ratio: str = "token_ceiling_fill_ratio"
    item_ceiling_fill_ratio: str = "item_ceiling_fill_ratio"
    estimated_to_actual_token_ratio: str = "estimated_to_actual_token_ratio"


METRICS = MetricNames()
DEFAULT_METRIC_PREFIX = "embedding_batch"
_METRIC_PREFIX_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class MetricLogger(Protocol):
    def log_metrics(self, metrics: dict[str, float]) -> None: ...


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def calculate_run_metrics(
    measurements: list[RequestMeasurement],
    max_inputs_per_request: int = 0,
    max_tokens_per_request: int = 0,
    target_tpm: int = 0,
    target_inputs_per_minute: float = 0,
) -> dict[str, float]:
    if not measurements:
        return {
            METRICS.configured_target_tpm: float(target_tpm),
            METRICS.configured_target_inputs_per_minute: float(
                target_inputs_per_minute
            ),
            METRICS.attempted_requests: 0.0,
            METRICS.successful_requests: 0.0,
            METRICS.failed_requests: 0.0,
            METRICS.attempted_logical_inputs: 0.0,
            METRICS.successful_logical_inputs: 0.0,
            METRICS.prompt_tokens: 0.0,
            METRICS.request_window_seconds: 0.0,
            METRICS.attempted_rpm: 0.0,
            METRICS.successful_rpm: 0.0,
            METRICS.accepted_tpm: 0.0,
            METRICS.logical_inputs_per_minute: 0.0,
            METRICS.success_rate: 0.0,
            METRICS.throttled_requests: 0.0,
            METRICS.throttle_rate: 0.0,
            METRICS.request_latency_p50_ms: 0.0,
            METRICS.request_latency_p95_ms: 0.0,
            METRICS.request_latency_p99_ms: 0.0,
            METRICS.inputs_per_request: 0.0,
            METRICS.prompt_tokens_per_successful_request: 0.0,
            METRICS.token_ceiling_fill_ratio: 0.0,
            METRICS.item_ceiling_fill_ratio: 0.0,
            METRICS.estimated_to_actual_token_ratio: 0.0,
        }

    attempted_requests = len(measurements)
    successful = [
        measurement for measurement in measurements if measurement.status_code == 200
    ]
    successful_requests = len(successful)
    throttled_requests = sum(
        measurement.status_code == 429 for measurement in measurements
    )
    attempted_logical_inputs = sum(
        measurement.input_count for measurement in measurements
    )
    successful_logical_inputs = sum(
        measurement.input_count for measurement in successful
    )
    prompt_tokens = sum(measurement.prompt_tokens for measurement in successful)
    estimated_tokens = sum(measurement.estimated_tokens for measurement in successful)
    latencies_ms = [
        (measurement.completed_seconds - measurement.started_seconds) * 1000
        for measurement in measurements
    ]
    window_seconds = max(
        max(measurement.completed_seconds for measurement in measurements)
        - min(measurement.started_seconds for measurement in measurements),
        0.0,
    )
    rate_factor = 60.0 / window_seconds if window_seconds > 0 else 0.0
    return {
        METRICS.configured_target_tpm: float(target_tpm),
        METRICS.configured_target_inputs_per_minute: float(
            target_inputs_per_minute
        ),
        METRICS.attempted_requests: float(attempted_requests),
        METRICS.successful_requests: float(successful_requests),
        METRICS.failed_requests: float(attempted_requests - successful_requests),
        METRICS.attempted_logical_inputs: float(attempted_logical_inputs),
        METRICS.successful_logical_inputs: float(successful_logical_inputs),
        METRICS.prompt_tokens: float(prompt_tokens),
        METRICS.request_window_seconds: window_seconds,
        METRICS.attempted_rpm: attempted_requests * rate_factor,
        METRICS.successful_rpm: successful_requests * rate_factor,
        METRICS.accepted_tpm: prompt_tokens * rate_factor,
        METRICS.logical_inputs_per_minute: successful_logical_inputs * rate_factor,
        METRICS.success_rate: successful_requests / attempted_requests,
        METRICS.throttled_requests: float(throttled_requests),
        METRICS.throttle_rate: throttled_requests / attempted_requests,
        METRICS.request_latency_p50_ms: percentile(latencies_ms, 50),
        METRICS.request_latency_p95_ms: percentile(latencies_ms, 95),
        METRICS.request_latency_p99_ms: percentile(latencies_ms, 99),
        METRICS.inputs_per_request: attempted_logical_inputs / attempted_requests,
        METRICS.prompt_tokens_per_successful_request: (
            prompt_tokens / successful_requests if successful_requests else 0.0
        ),
        METRICS.token_ceiling_fill_ratio: (
            prompt_tokens / (successful_requests * max_tokens_per_request)
            if successful_requests and max_tokens_per_request
            else 0.0
        ),
        METRICS.item_ceiling_fill_ratio: (
            attempted_logical_inputs / (attempted_requests * max_inputs_per_request)
            if max_inputs_per_request
            else 0.0
        ),
        METRICS.estimated_to_actual_token_ratio: (
            estimated_tokens / prompt_tokens if prompt_tokens else 0.0
        ),
    }


def validate_metric_prefix(prefix: str) -> str:
    if not _METRIC_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError(
            "metric_prefix must start with a letter and contain at most 64 "
            "letters, digits, dots, dashes, or underscores"
        )
    return prefix


def publish_run_metrics(
    metrics: dict[str, float],
    mode: str,
    prefix: str = DEFAULT_METRIC_PREFIX,
    logger: MetricLogger | None = None,
) -> dict[str, float]:
    selected_mode = MetricLoggingMode(mode)
    if selected_mode == MetricLoggingMode.DISABLED:
        return {}

    validated_prefix = validate_metric_prefix(prefix)
    namespaced_metrics = {
        f"{validated_prefix}.{name}": value for name, value in metrics.items()
    }
    if logger is None:
        import mlflow

        logger = mlflow
    logger.log_metrics(namespaced_metrics)
    return namespaced_metrics