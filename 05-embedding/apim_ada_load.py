"""Local synchronous test client for direct Foundry and APIM embedding paths.

This module does not invoke the Azure Machine Learning batch endpoint or its
`embedding-ada-v1` deployment. It sends HTTP embedding requests from the local
development environment to isolate Foundry compatibility, APIM routing,
throttling, and latency before integrating the gateway into the AML workload.
"""

import argparse
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from azure.identity import DefaultAzureCredential

from apim_ada_poc import POC, Context, Settings, build_context
from utils.embedding_optimization import (
    pack_compatible_requests,
    pacing_interval_seconds,
    percentile,
    target_tokens_per_request,
    token_counter_for_model,
    tokens_per_minute,
    utilization_target_tpm,
)


@dataclass(frozen=True)
class LoadContract:
    token_scope: str = "https://cognitiveservices.azure.com/.default"
    subscription_header: str = "Ocp-Apim-Subscription-Key"
    expected_dimensions: int = 1536
    request_timeout_seconds: int = 120
    default_batch_size: int = 100
    default_duration_seconds: int = 120
    minimum_failure_backoff_seconds: float = 1.0
    primary_target: str = "primary"
    secondary_target: str = "secondary"
    gateway_target: str = "gateway"
    retry_after_ms_header: str = "retry-after-ms"
    retry_after_header: str = "retry-after"
    rate_limit_headers: tuple[tuple[str, str], ...] = (
        ("x-ratelimit-limit-requests", "rate_limit_limit_requests"),
        ("x-ratelimit-remaining-requests", "rate_limit_remaining_requests"),
        ("x-ratelimit-reset-requests", "rate_limit_reset_requests"),
        ("x-ratelimit-limit-tokens", "rate_limit_limit_tokens"),
        ("x-ratelimit-remaining-tokens", "rate_limit_remaining_tokens"),
        ("x-ratelimit-reset-tokens", "rate_limit_reset_tokens"),
    )


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    headers: dict[str, str]


LOAD = LoadContract()


def target_url(account_name: str, deployment_name: str, api_version: str) -> str:
    return (
        f"https://{account_name}.openai.azure.com/openai/deployments/"
        f"{deployment_name}/embeddings?api-version={api_version}"
    )


def targets(
    context: Context,
    credential: DefaultAzureCredential,
    requested_targets: tuple[str, ...],
) -> dict[str, Target]:
    settings = context.settings
    selected: dict[str, Target] = {}
    direct_names = tuple(
        name
        for name in requested_targets
        if name in (LOAD.primary_target, LOAD.secondary_target)
    )
    if direct_names:
        bearer_token = credential.get_token(LOAD.token_scope).token
        direct_headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }
    if LOAD.primary_target in direct_names:
        selected[LOAD.primary_target] = Target(
            LOAD.primary_target,
            target_url(
                settings.primary_account_name,
                settings.deployment_name,
                settings.api_version,
            ),
            direct_headers,
        )
    if LOAD.secondary_target in direct_names:
        selected[LOAD.secondary_target] = Target(
            LOAD.secondary_target,
            target_url(
                settings.secondary_account_name,
                settings.deployment_name,
                settings.api_version,
            ),
            direct_headers,
        )
    if LOAD.gateway_target in requested_targets:
        keys = context.apim_client.subscription.list_secrets(
            settings.apim_resource_group,
            settings.apim_name,
            POC.subscription_id,
        )
        if not keys.primary_key:
            raise RuntimeError("The APIM PoC subscription has no primary key")
        selected[LOAD.gateway_target] = Target(
            LOAD.gateway_target,
            (
                f"{context.gateway_url}/{POC.api_path}/deployments/"
                f"{settings.deployment_name}/embeddings"
            ),
            {
                LOAD.subscription_header: keys.primary_key,
                "Content-Type": "application/json",
            },
        )
    return selected


def sample_inputs(count: int, sequence: int = 0) -> list[str]:
    return [
        f"ADA APIM capacity proof input {sequence}-{index}: preserve correlation."
        for index in range(count)
    ]


def rpm_requests(
    input_count: int,
    batch_size: int,
    model: str,
    max_batch_tokens: int | None = None,
) -> list[dict[str, Any]]:
    requests_to_pack = [
        {
            "input_ids": [f"input-{index}"],
            "texts": [text],
            "body": {
                "model": model,
                "input": text,
                "encoding_format": "float",
            },
            "input_count": 1,
        }
        for index, text in enumerate(sample_inputs(input_count))
    ]
    return list(
        pack_compatible_requests(
            requests_to_pack,
            "batch",
            batch_size,
            max_tokens_per_request=max_batch_tokens,
            count_tokens=(token_counter_for_model(model) if max_batch_tokens else None),
        )
    )


def retry_after_seconds(response: requests.Response) -> float | None:
    retry_after_ms = response.headers.get(LOAD.retry_after_ms_header)
    if retry_after_ms is not None:
        try:
            return max(float(retry_after_ms) / 1000, 0)
        except ValueError:
            return None
    retry_after = response.headers.get(LOAD.retry_after_header)
    if retry_after is not None:
        try:
            return max(float(retry_after), 0)
        except ValueError:
            return None
    return None


def error_metadata(response: requests.Response) -> tuple[str | None, str]:
    try:
        body = response.json()
    except requests.exceptions.JSONDecodeError:
        return None, response.text[:500]
    error = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message", str(error))
        return str(code) if code is not None else None, str(message)[:500]
    return None, str(error)[:500]


def send_embeddings(
    session: requests.Session,
    target: Target,
    texts: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    response = session.post(
        target.url,
        headers=target.headers,
        json={"input": texts, "encoding_format": "float"},
        timeout=LOAD.request_timeout_seconds,
    )
    duration_ms = (time.perf_counter() - started) * 1000
    retry_after = retry_after_seconds(response)
    metric = {
        "target": target.name,
        "status_code": response.status_code,
        "duration_ms": round(duration_ms, 3),
        "input_count": len(texts),
        "retry_after": retry_after,
    }
    for field_name, header_name in (
        ("backend_id", POC.backend_id_header),
        ("backend_type", POC.backend_type_header),
        ("backend_region", POC.backend_region_header),
    ):
        header_value = response.headers.get(header_name)
        if header_value is not None:
            metric[field_name] = header_value
    for header_name, field_name in LOAD.rate_limit_headers:
        header_value = response.headers.get(header_name)
        if header_value is not None:
            metric[field_name] = header_value
    if not response.ok:
        error_code, error_message = error_metadata(response)
        metric["error_code"] = error_code
        metric["error_message"] = error_message
        return {}, metric

    body = response.json()
    data = sorted(body.get("data", []), key=lambda item: item["index"])
    indexes = [item["index"] for item in data]
    if indexes != list(range(len(texts))):
        raise ValueError(f"{target.name} returned incomplete embedding indexes")
    dimensions = {len(item["embedding"]) for item in data}
    if dimensions != {LOAD.expected_dimensions}:
        raise ValueError(f"{target.name} returned dimensions {sorted(dimensions)}")
    if not all(
        isinstance(value, (int, float)) and math.isfinite(value)
        for item in data
        for value in item["embedding"]
    ):
        raise ValueError(f"{target.name} returned a non-finite embedding value")
    prompt_tokens = int(body.get("usage", {}).get("prompt_tokens", 0))
    metric.update(
        {
            "prompt_tokens": prompt_tokens,
            "dimensions": LOAD.expected_dimensions,
            "correlated_inputs": len(data),
        }
    )
    return body, metric


def cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        raise ValueError("Cannot compute cosine similarity for a zero-norm embedding")
    return numerator / (left_norm * right_norm)


def compare_embeddings(left: dict[str, Any], right: dict[str, Any]) -> dict[str, float]:
    similarities = []
    maximum_difference = 0.0
    for left_item, right_item in zip(left["data"], right["data"], strict=True):
        left_vector = left_item["embedding"]
        right_vector = right_item["embedding"]
        similarities.append(cosine_similarity(left_vector, right_vector))
        maximum_difference = max(
            maximum_difference,
            max(
                abs(a - b)
                for a, b in zip(left_vector, right_vector, strict=True)
            ),
        )
    return {
        "minimum_cosine_similarity": min(similarities),
        "maximum_absolute_difference": maximum_difference,
    }


def run_smoke(all_targets: dict[str, Target], output_dir: Path, input_count: int) -> None:
    texts = sample_inputs(input_count)
    responses: dict[str, dict[str, Any]] = {}
    metrics = []
    with requests.Session() as session:
        for name in ("primary", "secondary", "gateway"):
            body, metric = send_embeddings(session, all_targets[name], texts)
            metrics.append(metric)
            if not body:
                raise RuntimeError(f"Smoke request failed: {metric}")
            responses[name] = body

    report = {
        "mode": "smoke",
        "input_count": input_count,
        "requests": metrics,
        "comparisons": {
            "primary_secondary": compare_embeddings(
                responses["primary"], responses["secondary"]
            ),
            "primary_gateway": compare_embeddings(
                responses["primary"], responses["gateway"]
            ),
        },
    }
    write_report(output_dir, report, [])
    print(json.dumps(report, indent=2))


def run_rpm(
    target: Target,
    output_dir: Path,
    input_count: int,
    batch_size: int,
    model: str,
    max_batch_tokens: int | None,
) -> None:
    batches = rpm_requests(input_count, batch_size, model, max_batch_tokens)
    records = []
    with requests.Session() as session:
        for sequence, batch in enumerate(batches):
            _, metric = send_embeddings(session, target, batch["texts"])
            metric["sequence"] = sequence
            metric["estimated_tokens"] = batch["estimated_tokens"]
            records.append(metric)
    successful = [record for record in records if record["status_code"] == 200]
    report = {
        "mode": "rpm",
        "target": target.name,
        "logical_inputs": input_count,
        "batch_size": batch_size,
        "max_batch_tokens": max_batch_tokens,
        "http_requests": len(records),
        "successful_requests": len(successful),
        "inputs_per_request": round(input_count / len(records), 3),
        "rpm_reduction": round(1 - len(records) / input_count, 6),
        "http_429": sum(record["status_code"] == 429 for record in records),
        "estimated_tokens": sum(
            record.get("estimated_tokens", 0) for record in records
        ),
        "actual_prompt_tokens": sum(
            record.get("prompt_tokens", 0) for record in successful
        ),
        "batch_token_sizes": [
            record.get("estimated_tokens", 0) for record in records
        ],
    }
    write_report(output_dir, report, records)
    print(json.dumps(report, indent=2))


def run_load(
    target: Target,
    output_dir: Path,
    batch_size: int,
    duration_seconds: int,
    target_tpm: int,
    max_batch_tokens: int,
    model: str,
) -> None:
    records = []
    sequence = 0
    started = time.monotonic()
    next_request_at = started
    token_counter = token_counter_for_model(model)
    with requests.Session() as session:
        while time.monotonic() - started < duration_seconds:
            remaining_wait = next_request_at - time.monotonic()
            if remaining_wait > 0:
                if next_request_at - started >= duration_seconds:
                    break
                time.sleep(remaining_wait)
            candidate_requests = rpm_requests(
                batch_size,
                batch_size,
                model,
                max_batch_tokens,
            )
            batch = candidate_requests[0]
            texts = [
                f"{text} sequence {sequence}"
                for text in batch["texts"]
            ]
            estimated_tokens = sum(token_counter(text) for text in texts)
            _, metric = send_embeddings(session, target, texts)
            metric["sequence"] = sequence
            metric["estimated_tokens"] = estimated_tokens
            metric["start_offset_seconds"] = round(time.monotonic() - started, 3)
            records.append(metric)
            sequence += 1
            prompt_tokens = metric.get("prompt_tokens")
            if not prompt_tokens:
                retry_after = metric.get("retry_after")
                next_request_at = time.monotonic() + max(
                    float(retry_after or 0),
                    LOAD.minimum_failure_backoff_seconds,
                )
                continue
            next_request_at = max(
                next_request_at + pacing_interval_seconds(prompt_tokens, target_tpm),
                time.monotonic(),
            )

    successful = [record for record in records if record["status_code"] == 200]
    durations = [record["duration_ms"] for record in successful]
    prompt_tokens = sum(record.get("prompt_tokens", 0) for record in successful)
    elapsed = time.monotonic() - started
    steady_state_tpm = None
    if len(successful) > 1:
        steady_seconds = (
            successful[-1]["start_offset_seconds"]
            - successful[0]["start_offset_seconds"]
        )
        steady_tokens = sum(
            record.get("prompt_tokens", 0) for record in successful[1:]
        )
        if steady_seconds > 0:
            steady_state_tpm = round(
                tokens_per_minute(steady_tokens, steady_seconds),
                3,
            )
    report = {
        "mode": "tpm",
        "target": target.name,
        "configured_target_tpm": target_tpm,
        "max_batch_tokens": max_batch_tokens,
        "elapsed_seconds": round(elapsed, 3),
        "requests": len(records),
        "successful_requests": len(successful),
        "http_429": sum(record["status_code"] == 429 for record in records),
        "http_503": sum(record["status_code"] == 503 for record in records),
        "prompt_tokens": prompt_tokens,
        "estimated_tokens": sum(
            record.get("estimated_tokens", 0) for record in records
        ),
        "batch_token_sizes": [
            record.get("estimated_tokens", 0) for record in records
        ],
        "window_tpm": round(tokens_per_minute(prompt_tokens, elapsed), 3),
        "steady_state_tpm": steady_state_tpm,
        "logical_inputs": sum(record["input_count"] for record in successful),
        "latency_ms": {
            "mean": round(statistics.fmean(durations), 3) if durations else None,
            "p50": round(value, 3) if (value := percentile(durations, 50)) is not None else None,
            "p95": round(value, 3) if (value := percentile(durations, 95)) is not None else None,
            "p99": round(value, 3) if (value := percentile(durations, 99)) is not None else None,
        },
    }
    write_report(output_dir, report, records)
    print(json.dumps(report, indent=2))


def write_report(output_dir: Path, report: dict[str, Any], records: list[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "requests.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / "config" / ".env")
    parser = argparse.ArgumentParser(description="Test direct and APIM ADA capacity paths.")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    smoke = subparsers.add_parser(
        "smoke",
        help="Run local synchronous HTTP checks; does not invoke the AML batch endpoint.",
    )
    smoke.add_argument("--inputs", type=int, default=8)
    smoke.add_argument("--output", type=Path, default=Path("outputs/apim-ada-smoke"))
    load = subparsers.add_parser(
        "tpm",
        aliases=("load",),
        help="Run local TPM-paced HTTP load; does not invoke the AML batch endpoint.",
    )
    load.add_argument("--target", choices=("primary", "secondary", "gateway"), required=True)
    load.add_argument("--batch-size", type=int, default=LOAD.default_batch_size)
    load.add_argument("--duration-seconds", type=int, default=LOAD.default_duration_seconds)
    load.add_argument("--target-tpm", type=int)
    load.add_argument("--max-batch-tokens", type=int)
    load.add_argument("--requests-per-minute", type=float)
    load.add_argument("--output", type=Path, default=Path("outputs/apim-ada-tpm"))
    rpm = subparsers.add_parser(
        "rpm",
        help="Compare logical inputs with packed HTTP requests using shared packing.",
    )
    rpm.add_argument("--target", choices=("primary", "secondary", "gateway"), required=True)
    rpm.add_argument("--inputs", type=int, default=100)
    rpm.add_argument("--batch-size", type=int, default=LOAD.default_batch_size)
    rpm.add_argument("--max-batch-tokens", type=int)
    rpm.add_argument("--output", type=Path, default=Path("outputs/apim-ada-rpm"))
    args = parser.parse_args()

    if getattr(args, "inputs", 1) < 1 or getattr(args, "batch_size", 1) < 1:
        parser.error("input counts must be positive")
    if getattr(args, "duration_seconds", 1) < 1:
        parser.error("duration must be positive")

    credential = DefaultAzureCredential()
    context = build_context(Settings.from_environment())
    requested_targets = (
        ("primary", "secondary", "gateway")
        if args.mode == "smoke"
        else (args.target,)
    )
    all_targets = targets(context, credential, requested_targets)
    assigned_tpm = (
        context.capacity.aggregate_tpm
        if getattr(args, "target", None) == "gateway"
        else context.capacity.primary_tpm
        if getattr(args, "target", None) == "primary"
        else context.capacity.secondary_tpm
    )
    target_tpm = getattr(args, "target_tpm", None) or utilization_target_tpm(
        assigned_tpm,
        context.settings.target_utilization,
    )
    requests_per_minute = (
        getattr(args, "requests_per_minute", None)
        or context.capacity.requests_per_minute
    )
    max_batch_tokens = getattr(args, "max_batch_tokens", None) or target_tokens_per_request(
        target_tpm,
        requests_per_minute,
    )
    if args.mode == "smoke":
        run_smoke(all_targets, args.output, args.inputs)
    elif args.mode == "rpm":
        run_rpm(
            all_targets[args.target],
            args.output,
            args.inputs,
            args.batch_size,
            context.settings.deployment_name,
            max_batch_tokens,
        )
    else:
        run_load(
            all_targets[args.target],
            args.output,
            args.batch_size,
            args.duration_seconds,
            target_tpm,
            max_batch_tokens,
            context.settings.deployment_name,
        )


if __name__ == "__main__":
    main()