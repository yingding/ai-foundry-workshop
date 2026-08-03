import argparse
import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from azure.identity import get_bearer_token_provider
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from utils.fdyauth import AuthHelper
from utils.aml_metrics import (
    DEFAULT_METRIC_PREFIX,
    MetricLoggingMode,
    RequestMeasurement,
    calculate_run_metrics,
    publish_run_metrics,
)
from utils.embedding_optimization import (
    input_pacing_interval_seconds,
    pack_compatible_requests,
    pacing_interval_seconds,
    token_counter_for_model,
)


class PackingMode(StrEnum):
    ONE_INPUT_PER_REQUEST = "none"
    PACKED_INPUT_ARRAY = "batch"


class EncodingFormat(StrEnum):
    FLOAT = "float"
    BASE64 = "base64"


@dataclass(frozen=True)
class RequestFields:
    input_id: str = "input_id"
    input: str = "input"
    model: str = "model"
    dimensions: str = "dimensions"
    encoding_format: str = "encoding_format"
    user: str = "user"
    input_ids: str = "input_ids"
    texts: str = "texts"
    body: str = "body"
    input_count: str = "input_count"
    estimated_tokens: str = "estimated_tokens"


@dataclass(frozen=True)
class ResponseFields:
    object: str = "object"
    data: str = "data"
    embedding: str = "embedding"
    index: str = "index"
    model: str = "model"
    usage: str = "usage"
    prompt_tokens: str = "prompt_tokens"
    total_tokens: str = "total_tokens"
    error: str = "error"
    code: str = "code"
    message: str = "message"


@dataclass(frozen=True)
class TraceContract:
    service_name: str = "aml-batch-embeddings"
    root_span: str = "batch.embed"
    request_span: str = "embeddings.create"
    deployment: str = "embedding.deployment"
    model: str = "embedding.model"
    dry_run: str = "embedding.dry_run"
    packing: str = "embedding.packing"
    max_inputs_per_request: str = "embedding.max_inputs_per_request"
    max_tokens_per_request: str = "embedding.max_tokens_per_request"
    target_tpm: str = "embedding.target_tpm"
    target_inputs_per_minute: str = "embedding.target_inputs_per_minute"
    max_retries: str = "embedding.max_retries"
    request_concurrency: str = "embedding.request_concurrency"
    token_scope: str = "embedding.token_scope"
    metric_logging: str = "embedding.metric_logging"
    metric_prefix: str = "embedding.metric_prefix"
    metric_logging_error: str = "embedding.metric_logging_error"
    source_line_count: str = "embedding.source_line_count"
    online_request_count: str = "embedding.online_request_count"
    embedding_input_count: str = "embedding.input_count"
    failed_count: str = "embedding.failed_count"
    duration_ms: str = "embedding.duration_ms"
    request_start_offset_ms: str = "request.start_offset_ms"
    request_duration_ms: str = "request.duration_ms"
    batch_number: str = "batch.number"
    batch_input_count: str = "batch.embedding_input_count"
    batch_estimated_tokens: str = "batch.estimated_tokens"
    batch_prompt_tokens: str = "batch.prompt_tokens"
    http_status_code: str = "http.status_code"
    http_retry_after_ms: str = "http.retry_after_ms"
    http_retry_after: str = "http.retry_after"


@dataclass(frozen=True)
class ComponentFiles:
    embeddings: str = "embeddings.jsonl"
    trace: str = "trace.jsonl"


@dataclass(frozen=True)
class ComponentLimits:
    max_array_inputs: int = 2048
    max_request_tokens: int = 300_000
    max_batch_inputs: int = 50_000
    max_request_concurrency: int = 100


@dataclass(frozen=True)
class ComponentDefaults:
    packing: PackingMode = PackingMode.PACKED_INPUT_ARRAY
    encoding_format: EncodingFormat = EncodingFormat.FLOAT
    max_inputs_per_request: int = 128
    max_tokens_per_request: int = 0
    target_tpm: int = 0
    target_inputs_per_minute: float = 0
    max_retries: int = 8
    request_concurrency: int = 1
    token_scope: str = "https://ai.azure.com/.default"
    dry_run_dimensions: int = 2
    dry_run_base64_embedding: str = "AAAAAA=="


@dataclass(frozen=True)
class RateLimitHeaders:
    values: tuple[str, ...] = (
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    )
    retry_after_ms: str = "retry-after-ms"
    retry_after: str = "retry-after"


REQUEST = RequestFields()
RESPONSE = ResponseFields()
TRACE = TraceContract()
FILES = ComponentFiles()
LIMITS = ComponentLimits()
DEFAULTS = ComponentDefaults()
RATE_LIMIT = RateLimitHeaders()


class JsonLinesSpanExporter(SpanExporter):
    def __init__(self, path: Path) -> None:
        self._stream = path.open("w", encoding="utf-8")

    def export(self, spans: Iterable[ReadableSpan]):
        for span in spans:
            context = span.get_span_context()
            parent_span_id = span.parent.span_id if span.parent else None
            self._stream.write(
                json.dumps(
                    {
                        "name": span.name,
                        "trace_id": format(context.trace_id, "032x"),
                        "span_id": format(context.span_id, "016x"),
                        "parent_span_id": format(parent_span_id, "016x") if parent_span_id else None,
                        "start_time_unix_nano": span.start_time,
                        "end_time_unix_nano": span.end_time,
                        "status": span.status.status_code.name,
                        "attributes": dict(span.attributes or {}),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        self._stream.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._stream.close()


def configure_tracing(output_dir: Path) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": TRACE.service_name}))
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stdout)))
    provider.add_span_processor(
        SimpleSpanProcessor(JsonLinesSpanExporter(output_dir / FILES.trace))
    )
    trace.set_tracer_provider(provider)
    return provider


def validate_request(row: Any, source: str, expected_model: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{source}: each line must be a JSON object")
    allowed_fields = {
        REQUEST.input_id,
        REQUEST.input,
        REQUEST.model,
        REQUEST.dimensions,
        REQUEST.encoding_format,
        REQUEST.user,
    }
    unknown_fields = sorted(set(row) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"{source}: unsupported fields: {', '.join(unknown_fields)}")
    if row.get(REQUEST.model) != expected_model:
        raise ValueError(
            f"{source}: model must match the selected deployment model "
            f"{expected_model!r}"
        )
    input_value = row.get(REQUEST.input)
    input_id_value = row.get(REQUEST.input_id)
    if isinstance(input_value, str):
        if not input_value:
            raise ValueError(f"{source}: input must not be empty")
        if not isinstance(input_id_value, str) or not input_id_value:
            raise ValueError(f"{source}: input_id must be a non-empty string")
        input_ids = [input_id_value]
        texts = [input_value]
    elif isinstance(input_value, list) and input_value:
        if len(input_value) > LIMITS.max_array_inputs:
            raise ValueError(
                f"{source}: input must contain at most "
                f"{LIMITS.max_array_inputs} texts"
            )
        if not all(isinstance(text, str) and bool(text) for text in input_value):
            raise ValueError(f"{source}: input must contain only non-empty strings")
        if not isinstance(input_id_value, list) or len(input_id_value) != len(input_value):
            raise ValueError(
                f"{source}: input_id must be an array with one ID per input text"
            )
        if not all(isinstance(input_id, str) and bool(input_id) for input_id in input_id_value):
            raise ValueError(f"{source}: input_id must contain only non-empty strings")
        input_ids = input_id_value
        texts = input_value
    else:
        raise ValueError(f"{source}: input must be a non-empty string or array of strings")
    if len(set(input_ids)) != len(input_ids):
        raise ValueError(f"{source}: input_id values must be unique")

    encoding_format = row.get(REQUEST.encoding_format, DEFAULTS.encoding_format)
    if encoding_format not in (EncodingFormat.FLOAT, EncodingFormat.BASE64):
        raise ValueError(f"{source}: encoding_format must be float or base64")
    dimensions = row.get(REQUEST.dimensions)
    if dimensions is not None and (
        not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0
    ):
        raise ValueError(f"{source}: dimensions must be a positive integer")
    user = row.get(REQUEST.user)
    if user is not None and not isinstance(user, str):
        raise ValueError(f"{source}: user must be a string")

    body = {
        key: value
        for key, value in row.items()
        if key != REQUEST.input_id
    }
    return {
        REQUEST.input_ids: input_ids,
        REQUEST.texts: texts,
        REQUEST.body: body,
        REQUEST.input_count: len(texts),
    }


def read_requests(input_dir: Path, expected_model: str) -> Iterable[dict[str, Any]]:
    input_ids: set[str] = set()
    total_inputs = 0
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.suffix != ".jsonl":
            continue
        with path.open(encoding="utf-8") as stream:
            for row_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                source = f"{path.name}:{row_number}"
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"{source}: invalid JSON: {error.msg}") from error
                request = validate_request(row, source, expected_model)
                for input_id in request[REQUEST.input_ids]:
                    if input_id in input_ids:
                        raise ValueError(
                            f"{source}: duplicate batch input_id {input_id!r}"
                        )
                    input_ids.add(input_id)
                total_inputs += request[REQUEST.input_count]
                if total_inputs > LIMITS.max_batch_inputs:
                    raise ValueError(
                        f"batch exceeds the {LIMITS.max_batch_inputs:,} "
                        "embedding input limit"
                    )
                yield request


def dry_run_response(body: dict[str, Any], model: str) -> dict[str, Any]:
    count = 1 if isinstance(body[REQUEST.input], str) else len(body[REQUEST.input])
    dimensions = body.get(REQUEST.dimensions, DEFAULTS.dry_run_dimensions)
    encoding_format = body.get(REQUEST.encoding_format, DEFAULTS.encoding_format)
    embedding: Any = (
        DEFAULTS.dry_run_base64_embedding
        if encoding_format == EncodingFormat.BASE64
        else [0.0] * dimensions
    )
    return {
        RESPONSE.object: "list",
        RESPONSE.data: [
            {
                RESPONSE.object: "embedding",
                RESPONSE.embedding: embedding,
                RESPONSE.index: index,
            }
            for index in range(count)
        ],
        RESPONSE.model: model,
        RESPONSE.usage: {
            RESPONSE.prompt_tokens: 0,
            RESPONSE.total_tokens: 0,
        },
    }


def run(
    input_dir: Path,
    output_dir: Path,
    endpoint: str,
    deployment: str,
    model: str,
    packing: str = DEFAULTS.packing,
    max_inputs_per_request: int = DEFAULTS.max_inputs_per_request,
    max_tokens_per_request: int = DEFAULTS.max_tokens_per_request,
    target_tpm: int = DEFAULTS.target_tpm,
    target_inputs_per_minute: float = DEFAULTS.target_inputs_per_minute,
    max_retries: int = DEFAULTS.max_retries,
    request_concurrency: int = DEFAULTS.request_concurrency,
    token_scope: str = DEFAULTS.token_scope,
    dry_run: bool = False,
    metric_logging: str = MetricLoggingMode.DISABLED,
    metric_prefix: str = DEFAULT_METRIC_PREFIX,
) -> None:
    if max_inputs_per_request < 1 or max_inputs_per_request > LIMITS.max_array_inputs:
        raise ValueError(
            "max_inputs_per_request must be between 1 and "
            f"{LIMITS.max_array_inputs}"
        )
    if max_tokens_per_request < 0 or max_tokens_per_request > LIMITS.max_request_tokens:
        raise ValueError(
            "max_tokens_per_request must be between 0 and "
            f"{LIMITS.max_request_tokens}"
        )
    if target_tpm < 0:
        raise ValueError("target_tpm must be non-negative")
    if target_inputs_per_minute < 0:
        raise ValueError("target_inputs_per_minute must be non-negative")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if request_concurrency < 1 or request_concurrency > LIMITS.max_request_concurrency:
        raise ValueError(
            "request_concurrency must be between 1 and "
            f"{LIMITS.max_request_concurrency}"
        )
    try:
        selected_metric_logging = MetricLoggingMode(metric_logging)
    except ValueError as error:
        raise ValueError(
            f"metric_logging must be one of: {', '.join(MetricLoggingMode)}"
        ) from error
    output_dir.mkdir(parents=True, exist_ok=True)
    provider = configure_tracing(output_dir)
    tracer = trace.get_tracer(__name__)
    client: Any = None
    if not dry_run:
        credential = AuthHelper.test_credential(
            scope=token_scope,
            allow_interactive=False,
        )
        token_provider = get_bearer_token_provider(credential, token_scope)
        client = OpenAI(
            base_url=endpoint.rstrip("/") + "/",
            api_key=token_provider,
            max_retries=max_retries,
            timeout=120,
        )

    output_path = output_dir / FILES.embeddings
    source_line_count = 0
    online_request_count = 0
    embedding_input_count = 0
    failed_count = 0
    first_request_error: Exception | None = None
    request_measurements: list[RequestMeasurement] = []
    measurement_lock = threading.Lock()
    started = time.perf_counter()
    with tracer.start_as_current_span(TRACE.root_span) as root_span:
        root_span.set_attribute(TRACE.deployment, deployment)
        root_span.set_attribute(TRACE.model, model)
        root_span.set_attribute(TRACE.dry_run, dry_run)
        root_span.set_attribute(TRACE.packing, packing)
        root_span.set_attribute(TRACE.max_inputs_per_request, max_inputs_per_request)
        root_span.set_attribute(TRACE.max_tokens_per_request, max_tokens_per_request)
        root_span.set_attribute(TRACE.target_tpm, target_tpm)
        root_span.set_attribute(
            TRACE.target_inputs_per_minute,
            target_inputs_per_minute,
        )
        root_span.set_attribute(TRACE.max_retries, max_retries)
        root_span.set_attribute(TRACE.request_concurrency, request_concurrency)
        root_span.set_attribute(TRACE.token_scope, token_scope)
        root_span.set_attribute(TRACE.metric_logging, selected_metric_logging)
        root_span.set_attribute(TRACE.metric_prefix, metric_prefix)
        with output_path.open("w", encoding="utf-8") as output:
            input_requests = list(read_requests(input_dir, model))
            source_line_count = len(input_requests)
            requests = list(
                pack_compatible_requests(
                    input_requests,
                    packing,
                    max_inputs_per_request,
                    max_tokens_per_request=(max_tokens_per_request or None),
                    count_tokens=(
                        token_counter_for_model(model)
                        if max_tokens_per_request
                        else None
                    ),
                )
            )

            start_gate = threading.Event()
            pacing_lock = threading.Lock()
            next_request_at = started

            def execute_request(item: tuple[int, dict[str, Any]]) -> dict[str, Any]:
                nonlocal next_request_at
                batch_number, request = item
                start_gate.wait()
                if target_tpm or target_inputs_per_minute:
                    with pacing_lock:
                        now = time.perf_counter()
                        if next_request_at > now:
                            time.sleep(next_request_at - now)
                            now = time.perf_counter()
                        intervals = []
                        if target_tpm:
                            intervals.append(
                                pacing_interval_seconds(
                                    request.get(REQUEST.estimated_tokens, 0),
                                    target_tpm,
                                )
                            )
                        if target_inputs_per_minute:
                            intervals.append(
                                input_pacing_interval_seconds(
                                    request[REQUEST.input_count],
                                    target_inputs_per_minute,
                                )
                            )
                        next_request_at = max(next_request_at, now) + max(intervals)
                with trace.use_span(root_span, end_on_exit=False):
                    with tracer.start_as_current_span(TRACE.request_span) as span:
                        request_started = time.perf_counter()
                        request_status_code: int | None = None
                        request_prompt_tokens = 0
                        span.set_attribute(
                            TRACE.request_start_offset_ms,
                            round((request_started - started) * 1000, 3),
                        )
                        span.set_attribute(TRACE.batch_number, batch_number)
                        span.set_attribute(
                            TRACE.batch_input_count,
                            request[REQUEST.input_count],
                        )
                        span.set_attribute(
                            TRACE.batch_estimated_tokens,
                            request.get(REQUEST.estimated_tokens, 0),
                        )
                        body = request[REQUEST.body]
                        try:
                            if dry_run:
                                response_body = dry_run_response(body, model)
                            else:
                                request_body = {
                                    key: value
                                    for key, value in body.items()
                                    if key != REQUEST.model
                                }
                                raw_response = client.embeddings.with_raw_response.create(
                                    model=deployment, **request_body
                                )
                                response = raw_response.parse()
                                for header_name in RATE_LIMIT.values:
                                    header_value = raw_response.headers.get(header_name)
                                    if header_value is not None:
                                        span.set_attribute(f"http.{header_name}", header_value)
                                response_body = response.model_dump(mode="json")
                            span.set_attribute(
                                TRACE.batch_prompt_tokens,
                                int(
                                    response_body.get(RESPONSE.usage, {}).get(
                                        RESPONSE.prompt_tokens,
                                        0,
                                    )
                                ),
                            )
                            request_prompt_tokens = int(
                                response_body.get(RESPONSE.usage, {}).get(
                                    RESPONSE.prompt_tokens,
                                    0,
                                )
                            )
                            request_status_code = 200
                            span.set_attribute(TRACE.http_status_code, 200)
                            response_items = sorted(
                                response_body[RESPONSE.data],
                                key=lambda response_item: response_item[RESPONSE.index],
                            )
                            if [
                                response_item[RESPONSE.index]
                                for response_item in response_items
                            ] != list(
                                range(request[REQUEST.input_count])
                            ):
                                raise ValueError("embedding response indexes are incomplete")
                            span.set_status(Status(StatusCode.OK))
                            return {
                                RESPONSE.object: response_body[RESPONSE.object],
                                RESPONSE.data: [
                                    {
                                        **response_item,
                                        REQUEST.input_id: request[REQUEST.input_ids][
                                            response_item[RESPONSE.index]
                                        ],
                                    }
                                    for response_item in response_items
                                ],
                                RESPONSE.model: response_body[RESPONSE.model],
                                RESPONSE.usage: response_body[RESPONSE.usage],
                            }
                        except Exception as error:
                            response = getattr(error, "response", None)
                            status_code = getattr(error, "status_code", None)
                            if status_code is not None:
                                span.set_attribute(TRACE.http_status_code, status_code)
                                request_status_code = int(status_code)
                            if response is not None:
                                retry_after_ms = response.headers.get(
                                    RATE_LIMIT.retry_after_ms
                                )
                                retry_after = response.headers.get(RATE_LIMIT.retry_after)
                                if retry_after_ms is not None:
                                    span.set_attribute(
                                        TRACE.http_retry_after_ms,
                                        retry_after_ms,
                                    )
                                elif retry_after is not None:
                                    span.set_attribute(TRACE.http_retry_after, retry_after)
                            span.record_exception(error)
                            span.set_status(Status(StatusCode.ERROR, str(error)))
                            return {
                                REQUEST.input_ids: request[REQUEST.input_ids],
                                RESPONSE.error: {
                                    RESPONSE.code: type(error).__name__,
                                    RESPONSE.message: str(error),
                                },
                                "_exception": error,
                            }
                        finally:
                            request_completed = time.perf_counter()
                            span.set_attribute(
                                TRACE.request_duration_ms,
                                round((request_completed - request_started) * 1000, 3),
                            )
                            with measurement_lock:
                                request_measurements.append(
                                    RequestMeasurement(
                                        started_seconds=request_started,
                                        completed_seconds=request_completed,
                                        input_count=request[REQUEST.input_count],
                                        estimated_tokens=request.get(
                                            REQUEST.estimated_tokens,
                                            0,
                                        ),
                                        prompt_tokens=request_prompt_tokens,
                                        status_code=request_status_code,
                                    )
                                )

            with ThreadPoolExecutor(max_workers=request_concurrency) as executor:
                futures = [
                    executor.submit(execute_request, item)
                    for item in enumerate(requests)
                ]
                start_gate.set()
                results = (future.result() for future in futures)
                for request, result in zip(requests, results, strict=True):
                    request_error = result.pop("_exception", None)
                    if RESPONSE.error in result:
                        failed_count += 1
                        if first_request_error is None:
                            first_request_error = request_error
                    output.write(json.dumps(result, separators=(",", ":")) + "\n")
                    online_request_count += 1
                    embedding_input_count += request[REQUEST.input_count]

        root_span.set_attribute(TRACE.source_line_count, source_line_count)
        root_span.set_attribute(TRACE.online_request_count, online_request_count)
        root_span.set_attribute(TRACE.embedding_input_count, embedding_input_count)
        root_span.set_attribute(TRACE.failed_count, failed_count)
        run_metrics = calculate_run_metrics(
            request_measurements,
            max_inputs_per_request=max_inputs_per_request,
            max_tokens_per_request=max_tokens_per_request,
            target_tpm=target_tpm,
            target_inputs_per_minute=target_inputs_per_minute,
        )
        for name, value in run_metrics.items():
            root_span.set_attribute(f"metric.{name}", value)
        try:
            published_metrics = publish_run_metrics(
                run_metrics,
                selected_metric_logging,
                metric_prefix,
            )
            if published_metrics:
                print(
                    f"Published {len(published_metrics)} MLflow metrics with "
                    f"prefix {metric_prefix!r}"
                )
        except Exception as error:
            message = f"MLflow metric publishing failed: {error}"
            root_span.set_attribute(TRACE.metric_logging_error, message)
            print(f"WARNING: {message}", file=sys.stderr)
        root_span.set_attribute(
            TRACE.duration_ms,
            round((time.perf_counter() - started) * 1000, 3),
        )
        root_span.set_status(Status(StatusCode.ERROR if failed_count else StatusCode.OK))

    provider.shutdown()
    print(
        f"Processed {source_line_count} JSONL lines and {embedding_input_count} inputs "
        f"with {online_request_count} online requests"
    )
    print(
        f"Wrote {online_request_count} response records to {output_path} "
        f"({failed_count} failed)"
    )
    print(f"Wrote trace spans to {output_dir / FILES.trace}")
    if first_request_error is not None:
        raise first_request_error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--packing",
        choices=tuple(PackingMode),
        default=DEFAULTS.packing,
    )
    parser.add_argument(
        "--max-inputs-per-request",
        type=int,
        default=DEFAULTS.max_inputs_per_request,
    )
    parser.add_argument(
        "--max-tokens-per-request",
        type=int,
        default=DEFAULTS.max_tokens_per_request,
    )
    parser.add_argument(
        "--target-tpm",
        type=int,
        default=DEFAULTS.target_tpm,
        help="Pace packed request starts to this token-per-minute target; zero disables pacing",
    )
    parser.add_argument(
        "--target-inputs-per-minute",
        type=float,
        default=DEFAULTS.target_inputs_per_minute,
        help="Apply an empirical logical-input pacing ceiling; zero disables it",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULTS.max_retries,
    )
    parser.add_argument(
        "--request-concurrency",
        type=int,
        default=DEFAULTS.request_concurrency,
    )
    parser.add_argument(
        "--token-scope",
        default=DEFAULTS.token_scope,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--metric-logging",
        choices=tuple(MetricLoggingMode),
        default=MetricLoggingMode.DISABLED,
    )
    parser.add_argument("--metric-prefix", default=DEFAULT_METRIC_PREFIX)
    args = parser.parse_args()
    run(
        args.input_dir,
        args.output_dir,
        args.endpoint,
        args.deployment,
        args.model,
        args.packing,
        args.max_inputs_per_request,
        args.max_tokens_per_request,
        args.target_tpm,
        args.target_inputs_per_minute,
        args.max_retries,
        args.request_concurrency,
        args.token_scope,
        args.dry_run,
        args.metric_logging,
        args.metric_prefix,
    )


if __name__ == "__main__":
    main()
