import argparse
import csv
import json
import sys
import time
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


def read_documents(input_dir: Path) -> Iterable[dict[str, str]]:
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue

        if path.suffix == ".jsonl":
            with path.open(encoding="utf-8") as stream:
                for row_number, line in enumerate(stream):
                    row = json.loads(line)
                    yield {
                        "id": str(row.get("id", f"{path.name}:{row_number}")),
                        "text": str(row["text"]),
                    }
        elif path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as stream:
                for row_number, row in enumerate(csv.DictReader(stream)):
                    yield {
                        "id": str(row.get("id") or f"{path.name}:{row_number}"),
                        "text": str(row["text"]),
                    }
        elif path.suffix == ".txt":
            with path.open(encoding="utf-8") as stream:
                for row_number, line in enumerate(stream):
                    if text := line.strip():
                        yield {"id": f"{path.name}:{row_number}", "text": text}


def batched(items: Iterable[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    batch: list[dict[str, str]] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def run(
    input_dir: Path,
    output_dir: Path,
    endpoint: str,
    deployment: str,
    batch_size: int,
    dry_run: bool = False,
) -> None:
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
            max_retries=8,
            timeout=120,
        )

    output_path = output_dir / "embeddings.jsonl"
    document_count = 0
    started = time.perf_counter()
    with tracer.start_as_current_span("batch.embed") as root_span:
        root_span.set_attribute("embedding.deployment", deployment)
        root_span.set_attribute("embedding.dry_run", dry_run)
        root_span.set_attribute("embedding.batch_size", batch_size)
        with output_path.open("w", encoding="utf-8") as output:
            for batch_number, documents in enumerate(
                batched(read_documents(input_dir), batch_size)
            ):
                with tracer.start_as_current_span("embeddings.create") as span:
                    span.set_attribute("batch.number", batch_number)
                    span.set_attribute("batch.document_count", len(documents))
                    if dry_run:
                        vectors = [
                            [float(len(document["text"])), float(index)]
                            for index, document in enumerate(documents)
                        ]
                    else:
                        response = client.embeddings.create(
                            model=deployment,
                            input=[document["text"] for document in documents],
                        )
                        vectors = [item.embedding for item in response.data]

                    for document, vector in zip(documents, vectors, strict=True):
                        output.write(
                            json.dumps(
                                {"id": document["id"], "embedding": vector},
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                    document_count += len(documents)
                    span.set_status(Status(StatusCode.OK))

        root_span.set_attribute("embedding.document_count", document_count)
        root_span.set_attribute(
            "embedding.duration_ms",
            round((time.perf_counter() - started) * 1000, 3),
        )
        root_span.set_status(Status(StatusCode.OK))

    json_output_path = output_dir / "embeddings.json"
    with output_path.open(encoding="utf-8") as json_lines, json_output_path.open(
        "w", encoding="utf-8"
    ) as json_output:
        json_output.write("[\n")
        first = True
        for line in json_lines:
            if not first:
                json_output.write(",\n")
            json_output.write(line.rstrip())
            first = False
        json_output.write("\n]\n")

    provider.shutdown()
    print(f"Wrote {document_count} embeddings to {output_path}")
    print(f"Wrote JSON array to {json_output_path}")
    print(f"Wrote trace spans to {output_dir / 'trace.jsonl'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(
        args.input_dir,
        args.output_dir,
        args.endpoint,
        args.deployment,
        args.batch_size,
        args.dry_run,
    )


if __name__ == "__main__":
    main()
