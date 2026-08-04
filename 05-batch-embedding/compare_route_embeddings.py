"""Compare embedding outputs from two AML batch routes.

Answers one question with evidence: do the same logical inputs produce the same
vectors on a token-aware packed route and on the APIM-pooled route?

    uv run compare-route-embeddings \\
      --baseline  outputs/workshop/tpm-direct-safe-output \\
      --candidate outputs/workshop/tpm-pool-safe-output \\
      --require-identical

Vectors are joined on ``input_id``, never on file position, because packing
changes how many items share a request. Exits non-zero when a check fails.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

OUTPUT_GLOB = "**/embeddings.jsonl"
DEFAULT_COSINE_MIN = 0.999999
DEFAULT_MAX_ABS_DIFF = 1e-6
DEFAULT_GROUP_SEPARATOR = "-repeat-"
SAMPLE_LIMIT = 5


@dataclass
class RouteOutput:
    """Vectors and request shape recovered from one downloaded run."""

    label: str
    path: Path
    vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    batch_sizes: list[int] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    error_records: int = 0
    prompt_tokens: int = 0
    models: set[str] = field(default_factory=set)

    @property
    def dimensions(self) -> set[int]:
        return {len(vector) for vector in self.vectors.values()}


@dataclass(frozen=True)
class Comparison:
    baseline: str
    candidate: str
    common: int
    only_baseline: list[str]
    only_candidate: list[str]
    identical: int
    cosine_min: float | None
    cosine_mean: float | None
    max_abs_diff: float | None
    max_l2_distance: float | None
    below_cosine_min: list[str]
    above_abs_tolerance: list[str]


def resolve_output_file(target: Path) -> Path:
    """Accept either the JSONL file or any parent directory of the download."""
    if target.is_file():
        return target
    matches = sorted(target.glob(OUTPUT_GLOB))
    if not matches:
        raise FileNotFoundError(f"no embeddings.jsonl under {target}")
    if len(matches) > 1:
        raise ValueError(f"multiple embeddings.jsonl under {target}: {matches}")
    return matches[0]


def _records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number} is not valid JSON") from error


def load_route(target: Path, label: str) -> RouteOutput:
    path = resolve_output_file(target)
    route = RouteOutput(label=label, path=path)
    for record in _records(path):
        if "error" in record:
            route.error_records += 1
            continue
        usage = record.get("usage") or {}
        route.prompt_tokens += int(usage.get("prompt_tokens", 0))
        if record.get("model"):
            route.models.add(str(record["model"]))
        items = record.get("data") or []
        route.batch_sizes.append(len(items))
        for item in items:
            input_id = item.get("input_id")
            if input_id is None:
                raise ValueError(f"{path} has an item without input_id")
            if input_id in route.vectors:
                route.duplicate_ids.append(input_id)
                continue
            route.vectors[input_id] = tuple(float(v) for v in item["embedding"])
    return route


def validate_route(route: RouteOutput, expected_dim: int | None = None) -> list[str]:
    issues: list[str] = []
    if not route.vectors:
        issues.append(f"{route.label}: no vectors found")
        return issues
    if route.duplicate_ids:
        issues.append(
            f"{route.label}: {len(route.duplicate_ids)} duplicate input_id "
            f"(sample {route.duplicate_ids[:SAMPLE_LIMIT]})"
        )
    if route.error_records:
        issues.append(f"{route.label}: {route.error_records} error records")
    if len(route.dimensions) != 1:
        issues.append(f"{route.label}: mixed dimensions {sorted(route.dimensions)}")
    elif expected_dim is not None and route.dimensions != {expected_dim}:
        issues.append(
            f"{route.label}: dimension {route.dimensions.pop()} != {expected_dim}"
        )
    if len(route.models) > 1:
        issues.append(f"{route.label}: mixed models {sorted(route.models)}")
    nonfinite = [
        input_id
        for input_id, vector in route.vectors.items()
        if not all(math.isfinite(value) for value in vector)
    ]
    if nonfinite:
        issues.append(
            f"{route.label}: {len(nonfinite)} vectors contain non-finite values "
            f"(sample {nonfinite[:SAMPLE_LIMIT]})"
        )
    return issues


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if norm == 0.0:
        raise ValueError("cannot compare a zero-magnitude vector")
    return dot / norm


def compare_routes(
    baseline: RouteOutput,
    candidate: RouteOutput,
    *,
    cosine_min: float = DEFAULT_COSINE_MIN,
    max_abs_diff: float = DEFAULT_MAX_ABS_DIFF,
) -> Comparison:
    common = sorted(set(baseline.vectors) & set(candidate.vectors))
    identical = 0
    cosines: list[float] = []
    worst_abs = 0.0
    worst_l2 = 0.0
    below_cosine: list[str] = []
    above_tolerance: list[str] = []

    for input_id in common:
        left = baseline.vectors[input_id]
        right = candidate.vectors[input_id]
        if len(left) != len(right):
            above_tolerance.append(input_id)
            continue
        if left == right:
            identical += 1
        similarity = cosine_similarity(left, right)
        cosines.append(similarity)
        if similarity < cosine_min:
            below_cosine.append(input_id)
        deltas = [abs(a - b) for a, b in zip(left, right)]
        peak = max(deltas)
        worst_abs = max(worst_abs, peak)
        worst_l2 = max(worst_l2, math.sqrt(sum(d * d for d in deltas)))
        if peak > max_abs_diff:
            above_tolerance.append(input_id)

    return Comparison(
        baseline=baseline.label,
        candidate=candidate.label,
        common=len(common),
        only_baseline=sorted(set(baseline.vectors) - set(candidate.vectors)),
        only_candidate=sorted(set(candidate.vectors) - set(baseline.vectors)),
        identical=identical,
        cosine_min=min(cosines) if cosines else None,
        cosine_mean=sum(cosines) / len(cosines) if cosines else None,
        max_abs_diff=worst_abs if cosines else None,
        max_l2_distance=worst_l2 if cosines else None,
        below_cosine_min=below_cosine,
        above_abs_tolerance=above_tolerance,
    )


def distinct_vectors_by_group(
    route: RouteOutput, separator: str = DEFAULT_GROUP_SEPARATOR
) -> dict[str, int]:
    """Count distinct vectors per source text, e.g. `chunk-01-repeat-07`.

    More than one distinct vector for repeats of the same text means the
    service is sensitive to array composition or is not bit-deterministic.
    """
    groups: dict[str, set[tuple[float, ...]]] = {}
    for input_id, vector in route.vectors.items():
        base = input_id.rsplit(separator, 1)[0] if separator in input_id else input_id
        groups.setdefault(base, set()).add(vector)
    return {base: len(vectors) for base, vectors in sorted(groups.items())}


def _summarize(route: RouteOutput) -> dict[str, Any]:
    return {
        "label": route.label,
        "path": str(route.path),
        "records": len(route.batch_sizes),
        "vectors": len(route.vectors),
        "batch_sizes": route.batch_sizes,
        "dimensions": sorted(route.dimensions),
        "prompt_tokens": route.prompt_tokens,
        "models": sorted(route.models),
        "error_records": route.error_records,
        "duplicate_ids": route.duplicate_ids[:SAMPLE_LIMIT],
    }


def build_report(
    baseline: RouteOutput,
    candidate: RouteOutput,
    comparison: Comparison,
    issues: list[str],
    *,
    group_separator: str,
) -> dict[str, Any]:
    return {
        "routes": [_summarize(baseline), _summarize(candidate)],
        "identical_batching": baseline.batch_sizes == candidate.batch_sizes,
        "comparison": {
            "common_input_ids": comparison.common,
            "only_baseline": comparison.only_baseline[:SAMPLE_LIMIT],
            "only_candidate": comparison.only_candidate[:SAMPLE_LIMIT],
            "bit_identical": comparison.identical,
            "cosine_min": comparison.cosine_min,
            "cosine_mean": comparison.cosine_mean,
            "max_abs_diff": comparison.max_abs_diff,
            "max_l2_distance": comparison.max_l2_distance,
            "below_cosine_min": comparison.below_cosine_min[:SAMPLE_LIMIT],
            "above_abs_tolerance": comparison.above_abs_tolerance[:SAMPLE_LIMIT],
        },
        "distinct_vectors_per_source_text": {
            baseline.label: distinct_vectors_by_group(baseline, group_separator),
            candidate.label: distinct_vectors_by_group(candidate, group_separator),
        },
        "issues": issues,
        "passed": not issues,
    }


def evaluate(
    baseline: RouteOutput,
    candidate: RouteOutput,
    *,
    cosine_min: float,
    max_abs_diff: float,
    require_identical: bool,
    expected_dim: int | None,
) -> tuple[Comparison, list[str]]:
    issues = validate_route(baseline, expected_dim) + validate_route(
        candidate, expected_dim
    )
    comparison = compare_routes(
        baseline, candidate, cosine_min=cosine_min, max_abs_diff=max_abs_diff
    )
    if comparison.only_baseline or comparison.only_candidate:
        issues.append(
            f"input_id sets differ: {len(comparison.only_baseline)} only in "
            f"{baseline.label}, {len(comparison.only_candidate)} only in "
            f"{candidate.label}"
        )
    if not comparison.common:
        issues.append("no shared input_id values to compare")
        return comparison, issues
    if comparison.below_cosine_min:
        issues.append(
            f"{len(comparison.below_cosine_min)} vectors below cosine "
            f"{cosine_min} (sample {comparison.below_cosine_min[:SAMPLE_LIMIT]})"
        )
    if comparison.above_abs_tolerance:
        issues.append(
            f"{len(comparison.above_abs_tolerance)} vectors exceed max abs diff "
            f"{max_abs_diff} (sample {comparison.above_abs_tolerance[:SAMPLE_LIMIT]})"
        )
    if require_identical and comparison.identical != comparison.common:
        issues.append(
            f"only {comparison.identical}/{comparison.common} vectors are "
            "bit-identical"
        )
    return comparison, issues


def print_report(report: dict[str, Any]) -> None:
    baseline, candidate = report["routes"]
    for route in (baseline, candidate):
        print(
            f"{route['label']:<10} {route['vectors']:>5} vectors  "
            f"{route['records']:>3} records  dim {route['dimensions']}  "
            f"{route['prompt_tokens']:>7,} prompt tokens  {route['path']}"
        )
    comparison = report["comparison"]
    print()
    print(f"identical batching     : {report['identical_batching']}")
    print(f"common input_id        : {comparison['common_input_ids']}")
    print(
        f"bit-identical vectors  : {comparison['bit_identical']} / "
        f"{comparison['common_input_ids']}"
    )
    if comparison["cosine_min"] is not None:
        print(
            f"cosine min / mean      : {comparison['cosine_min']:.9f} / "
            f"{comparison['cosine_mean']:.9f}"
        )
        print(f"worst |difference|     : {comparison['max_abs_diff']:.3e}")
        print(f"worst L2 distance      : {comparison['max_l2_distance']:.3e}")
    for label, groups in report["distinct_vectors_per_source_text"].items():
        spread = sorted(set(groups.values()))
        print(
            f"distinct vectors/text  : {label} -> {len(groups)} source texts, "
            f"counts {spread}"
        )
    print()
    if report["passed"]:
        print("PASS: routes agree within tolerance")
    else:
        print("FAIL:")
        for issue in report["issues"]:
            print(f"  - {issue}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-label", default="baseline")
    parser.add_argument("--candidate-label", default="candidate")
    parser.add_argument("--cosine-min", type=float, default=DEFAULT_COSINE_MIN)
    parser.add_argument("--max-abs-diff", type=float, default=DEFAULT_MAX_ABS_DIFF)
    parser.add_argument("--expected-dimensions", type=int, default=None)
    parser.add_argument("--group-separator", default=DEFAULT_GROUP_SEPARATOR)
    parser.add_argument(
        "--require-identical",
        action="store_true",
        help="fail unless every shared vector matches bit-for-bit",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    baseline = load_route(args.baseline, args.baseline_label)
    candidate = load_route(args.candidate, args.candidate_label)
    comparison, issues = evaluate(
        baseline,
        candidate,
        cosine_min=args.cosine_min,
        max_abs_diff=args.max_abs_diff,
        require_identical=args.require_identical,
        expected_dim=args.expected_dimensions,
    )
    report = build_report(
        baseline, candidate, comparison, issues,
        group_separator=args.group_separator,
    )
    print_report(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
