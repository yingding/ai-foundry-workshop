import argparse
import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
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
    provider = TracerProvider(resource=Resource.create({"service.name": "aml-batch-embeddings"}))
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stdout)))
    provider.add_span_processor(
        SimpleSpanProcessor(JsonLinesSpanExporter(output_dir / "trace.jsonl"))
    )
    trace.set_tracer_provider(provider)
    return provider


def validate_request(row: Any, source: str, expected_model: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{source}: each line must be a JSON object")
    allowed_fields = {
        "input_id",
        "input",
        "model",
        "dimensions",
        "encoding_format",
        "user",
    }
    unknown_fields = sorted(set(row) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"{source}: unsupported fields: {', '.join(unknown_fields)}")
    if row.get("model") != expected_model:
        raise ValueError(
            f"{source}: model must match the selected deployment model "
            f"{expected_model!r}"
        )
    input_value = row.get("input")
    input_id_value = row.get("input_id")
    if isinstance(input_value, str):
        if not input_value:
            raise ValueError(f"{source}: input must not be empty")
        if not isinstance(input_id_value, str) or not input_id_value:
            raise ValueError(f"{source}: input_id must be a non-empty string")
        input_ids = [input_id_value]
        texts = [input_value]
    elif isinstance(input_value, list) and input_value:
        if len(input_value) > 2048:
            raise ValueError(f"{source}: input must contain at most 2048 texts")
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

    encoding_format = row.get("encoding_format", "float")
    if encoding_format not in ("float", "base64"):
        raise ValueError(f"{source}: encoding_format must be float or base64")
    dimensions = row.get("dimensions")
    if dimensions is not None and (
        not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0
    ):
        raise ValueError(f"{source}: dimensions must be a positive integer")
    user = row.get("user")
    if user is not None and not isinstance(user, str):
        raise ValueError(f"{source}: user must be a string")

    body = {
        key: value
        for key, value in row.items()
        if key != "input_id"
    }
    return {
        "input_ids": input_ids,
        "texts": texts,
        "body": body,
        "input_count": len(texts),
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
                for input_id in request["input_ids"]:
                    if input_id in input_ids:
                        raise ValueError(
                            f"{source}: duplicate batch input_id {input_id!r}"
                        )
                    input_ids.add(input_id)
                total_inputs += request["input_count"]
                if total_inputs > 50_000:
                    raise ValueError("batch exceeds the 50,000 embedding input limit")
                yield request


def pack_requests(
    requests: Iterable[dict[str, Any]],
    packing: str,
    max_inputs_per_request: int,
) -> Iterable[dict[str, Any]]:
    if packing == "none":
        for request in requests:
            yield request
        return

    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for request in requests:
        body = request["body"]
        key = (
            body["model"],
            body.get("dimensions"),
            body.get("encoding_format", "float"),
            body.get("user"),
        )
        group = groups.setdefault(
            key,
            {
                "input_ids": [],
                "texts": [],
                "body": {key: value for key, value in body.items() if key != "input"},
            },
        )
        group["input_ids"].extend(request["input_ids"])
        group["texts"].extend(request["texts"])

    for group in groups.values():
        for start in range(0, len(group["texts"]), max_inputs_per_request):
            stop = start + max_inputs_per_request
            input_ids = group["input_ids"][start:stop]
            texts = group["texts"][start:stop]
            body = {**group["body"], "input": texts}
            yield {
                "input_ids": input_ids,
                "texts": texts,
                "body": body,
                "input_count": len(texts),
            }


def dry_run_response(body: dict[str, Any], model: str) -> dict[str, Any]:
    count = 1 if isinstance(body["input"], str) else len(body["input"])
    dimensions = body.get("dimensions", 2)
    encoding_format = body.get("encoding_format", "float")
    embedding: Any = "AAAAAA==" if encoding_format == "base64" else [0.0] * dimensions
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": embedding, "index": index}
            for index in range(count)
        ],
        "model": model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


def run(
    input_dir: Path,
    output_dir: Path,
    endpoint: str,
    deployment: str,
    model: str,
    packing: str = "batch",
    max_inputs_per_request: int = 128,
    max_retries: int = 8,
    request_concurrency: int = 1,
    dry_run: bool = False,
) -> None:
    if max_inputs_per_request < 1 or max_inputs_per_request > 2048:
        raise ValueError("max_inputs_per_request must be between 1 and 2048")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if request_concurrency < 1 or request_concurrency > 100:
        raise ValueError("request_concurrency must be between 1 and 100")
    output_dir.mkdir(parents=True, exist_ok=True)
    provider = configure_tracing(output_dir)
    tracer = trace.get_tracer(__name__)
    client: Any = None
    if not dry_run:
        credential = AuthHelper.test_credential(
            scope="https://ai.azure.com/.default",
            allow_interactive=False,
        )
        token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
        client = OpenAI(
            base_url=endpoint.rstrip("/") + "/",
            api_key=token_provider,
            max_retries=max_retries,
            timeout=120,
        )

    output_path = output_dir / "embeddings.jsonl"
    source_line_count = 0
    online_request_count = 0
    embedding_input_count = 0
    failed_count = 0
    first_request_error: Exception | None = None
    started = time.perf_counter()
    with tracer.start_as_current_span("batch.embed") as root_span:
        root_span.set_attribute("embedding.deployment", deployment)
        root_span.set_attribute("embedding.model", model)
        root_span.set_attribute("embedding.dry_run", dry_run)
        root_span.set_attribute("embedding.packing", packing)
        root_span.set_attribute("embedding.max_inputs_per_request", max_inputs_per_request)
        root_span.set_attribute("embedding.max_retries", max_retries)
        root_span.set_attribute("embedding.request_concurrency", request_concurrency)
        with output_path.open("w", encoding="utf-8") as output:
            input_requests = list(read_requests(input_dir, model))
            source_line_count = len(input_requests)
            requests = list(pack_requests(input_requests, packing, max_inputs_per_request))

            start_gate = threading.Event()

            def execute_request(item: tuple[int, dict[str, Any]]) -> dict[str, Any]:
                batch_number, request = item
                start_gate.wait()
                with trace.use_span(root_span, end_on_exit=False):
                    with tracer.start_as_current_span("embeddings.create") as span:
                        request_started = time.perf_counter()
                        span.set_attribute(
                            "request.start_offset_ms",
                            round((request_started - started) * 1000, 3),
                        )
                        span.set_attribute("batch.number", batch_number)
                        span.set_attribute("batch.embedding_input_count", request["input_count"])
                        body = request["body"]
                        try:
                            if dry_run:
                                response_body = dry_run_response(body, model)
                            else:
                                request_body = {
                                    key: value for key, value in body.items() if key != "model"
                                }
                                raw_response = client.embeddings.with_raw_response.create(
                                    model=deployment, **request_body
                                )
                                response = raw_response.parse()
                                for header_name in (
                                    "x-ratelimit-limit-requests",
                                    "x-ratelimit-remaining-requests",
                                    "x-ratelimit-reset-requests",
                                    "x-ratelimit-limit-tokens",
                                    "x-ratelimit-remaining-tokens",
                                    "x-ratelimit-reset-tokens",
                                ):
                                    header_value = raw_response.headers.get(header_name)
                                    if header_value is not None:
                                        span.set_attribute(f"http.{header_name}", header_value)
                                response_body = response.model_dump(mode="json")
                            span.set_attribute("http.status_code", 200)
                            response_items = sorted(
                                response_body["data"], key=lambda response_item: response_item["index"]
                            )
                            if [response_item["index"] for response_item in response_items] != list(
                                range(request["input_count"])
                            ):
                                raise ValueError("embedding response indexes are incomplete")
                            span.set_status(Status(StatusCode.OK))
                            return {
                                "object": response_body["object"],
                                "data": [
                                    {
                                        **response_item,
                                        "input_id": request["input_ids"][response_item["index"]],
                                    }
                                    for response_item in response_items
                                ],
                                "model": response_body["model"],
                                "usage": response_body["usage"],
                            }
                        except Exception as error:
                            response = getattr(error, "response", None)
                            status_code = getattr(error, "status_code", None)
                            if status_code is not None:
                                span.set_attribute("http.status_code", status_code)
                            if response is not None:
                                retry_after_ms = response.headers.get("retry-after-ms")
                                retry_after = response.headers.get("retry-after")
                                if retry_after_ms is not None:
                                    span.set_attribute("http.retry_after_ms", retry_after_ms)
                                elif retry_after is not None:
                                    span.set_attribute("http.retry_after", retry_after)
                            span.record_exception(error)
                            span.set_status(Status(StatusCode.ERROR, str(error)))
                            return {
                                "input_ids": request["input_ids"],
                                "error": {
                                    "code": type(error).__name__,
                                    "message": str(error),
                                },
                                "_exception": error,
                            }
                        finally:
                            span.set_attribute(
                                "request.duration_ms",
                                round((time.perf_counter() - request_started) * 1000, 3),
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
                    if "error" in result:
                        failed_count += 1
                        if first_request_error is None:
                            first_request_error = request_error
                    output.write(json.dumps(result, separators=(",", ":")) + "\n")
                    online_request_count += 1
                    embedding_input_count += request["input_count"]

        root_span.set_attribute("embedding.source_line_count", source_line_count)
        root_span.set_attribute("embedding.online_request_count", online_request_count)
        root_span.set_attribute("embedding.input_count", embedding_input_count)
        root_span.set_attribute("embedding.failed_count", failed_count)
        root_span.set_attribute(
            "embedding.duration_ms",
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
    print(f"Wrote trace spans to {output_dir / 'trace.jsonl'}")
    if first_request_error is not None:
        raise first_request_error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--packing", choices=("none", "batch"), default="batch")
    parser.add_argument("--max-inputs-per-request", type=int, default=128)
    parser.add_argument("--max-retries", type=int, default=8)
    parser.add_argument("--request-concurrency", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(
        args.input_dir,
        args.output_dir,
        args.endpoint,
        args.deployment,
        args.model,
        args.packing,
        args.max_inputs_per_request,
        args.max_retries,
        args.request_concurrency,
        args.dry_run,
    )


if __name__ == "__main__":
    main()
