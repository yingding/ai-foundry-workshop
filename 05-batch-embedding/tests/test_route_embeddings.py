import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compare_route_embeddings import (
    compare_routes,
    distinct_vectors_by_group,
    evaluate,
    load_route,
    resolve_output_file,
    validate_route,
)

DIM = 4


def record(items, model="text-embedding-ada-002", prompt_tokens=10):
    return {
        "object": "list",
        "model": model,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        "data": [
            {
                "object": "embedding",
                "index": index,
                "input_id": input_id,
                "embedding": list(vector),
            }
            for index, (input_id, vector) in enumerate(items)
        ],
    }


def write_jsonl(directory: Path, name: str, records) -> Path:
    path = directory / name / "named-outputs" / "embeddings"
    path.mkdir(parents=True)
    target = path / "embeddings.jsonl"
    target.write_text(
        "\n".join(json.dumps(entry) for entry in records) + "\n", encoding="utf-8"
    )
    return directory / name


def vector(seed: float):
    return tuple(seed + offset for offset in range(DIM))


class RouteLoadingTests(unittest.TestCase):
    def test_resolves_download_directory_and_preserves_batch_shape(self):
        with TemporaryDirectory() as tmp:
            root = write_jsonl(
                Path(tmp),
                "packed",
                [
                    record([("a-repeat-1", vector(1.0)), ("a-repeat-2", vector(2.0))]),
                    record([("b-repeat-1", vector(3.0))]),
                ],
            )
            route = load_route(root, "packed")
        self.assertEqual(len(route.vectors), 3)
        self.assertEqual(route.batch_sizes, [2, 1])
        self.assertEqual(route.dimensions, {DIM})
        self.assertEqual(route.prompt_tokens, 20)
        self.assertEqual(route.models, {"text-embedding-ada-002"})
        self.assertEqual(validate_route(route, expected_dim=DIM), [])

    def test_missing_output_file_is_reported(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                resolve_output_file(Path(tmp))

    def test_duplicate_and_error_records_become_issues(self):
        with TemporaryDirectory() as tmp:
            root = write_jsonl(
                Path(tmp),
                "dirty",
                [
                    record([("a", vector(1.0)), ("a", vector(9.0))]),
                    {"error": {"code": "429"}},
                ],
            )
            route = load_route(root, "dirty")
        issues = validate_route(route)
        self.assertEqual(route.error_records, 1)
        self.assertTrue(any("duplicate input_id" in issue for issue in issues))
        self.assertTrue(any("error records" in issue for issue in issues))

    def test_non_finite_values_are_rejected(self):
        with TemporaryDirectory() as tmp:
            root = write_jsonl(
                Path(tmp), "nan", [record([("a", (1.0, 2.0, 3.0, float("nan")))])]
            )
            route = load_route(root, "nan")
        self.assertTrue(any("non-finite" in issue for issue in validate_route(route)))


class ComparisonTests(unittest.TestCase):
    def _routes(self, baseline_items, candidate_items):
        with TemporaryDirectory() as tmp:
            base = write_jsonl(Path(tmp), "base", [record(baseline_items)])
            cand = write_jsonl(Path(tmp), "cand", [record(candidate_items)])
            return load_route(base, "direct"), load_route(cand, "pooled")

    def test_identical_vectors_pass_the_strict_gate(self):
        items = [("a-repeat-1", vector(1.0)), ("a-repeat-2", vector(2.0))]
        baseline, candidate = self._routes(items, items)
        comparison, issues = evaluate(
            baseline,
            candidate,
            cosine_min=0.999999,
            max_abs_diff=1e-6,
            require_identical=True,
            expected_dim=DIM,
        )
        self.assertEqual(issues, [])
        self.assertEqual(comparison.identical, 2)
        self.assertEqual(comparison.common, 2)
        self.assertAlmostEqual(comparison.cosine_min, 1.0)
        self.assertEqual(comparison.max_abs_diff, 0.0)

    def test_float_drift_passes_tolerance_but_fails_require_identical(self):
        baseline_items = [("a", vector(1.0))]
        drifted = tuple(value + 1e-9 for value in vector(1.0))
        baseline, candidate = self._routes(baseline_items, [("a", drifted)])

        _, tolerant = evaluate(
            baseline, candidate, cosine_min=0.999999, max_abs_diff=1e-6,
            require_identical=False, expected_dim=DIM,
        )
        self.assertEqual(tolerant, [])

        comparison, strict = evaluate(
            baseline, candidate, cosine_min=0.999999, max_abs_diff=1e-6,
            require_identical=True, expected_dim=DIM,
        )
        self.assertEqual(comparison.identical, 0)
        self.assertTrue(any("bit-identical" in issue for issue in strict))

    def test_diverging_vectors_fail_cosine_and_tolerance(self):
        baseline, candidate = self._routes(
            [("a", (1.0, 0.0, 0.0, 0.0))], [("a", (0.0, 1.0, 0.0, 0.0))]
        )
        comparison, issues = evaluate(
            baseline, candidate, cosine_min=0.999999, max_abs_diff=1e-6,
            require_identical=False, expected_dim=DIM,
        )
        self.assertEqual(comparison.below_cosine_min, ["a"])
        self.assertEqual(comparison.above_abs_tolerance, ["a"])
        self.assertTrue(any("below cosine" in issue for issue in issues))

    def test_mismatched_input_id_sets_are_reported(self):
        baseline, candidate = self._routes(
            [("a", vector(1.0)), ("b", vector(2.0))], [("a", vector(1.0))]
        )
        comparison, issues = evaluate(
            baseline, candidate, cosine_min=0.999999, max_abs_diff=1e-6,
            require_identical=False, expected_dim=DIM,
        )
        self.assertEqual(comparison.only_baseline, ["b"])
        self.assertEqual(comparison.only_candidate, [])
        self.assertTrue(any("input_id sets differ" in issue for issue in issues))

    def test_join_is_by_input_id_not_file_position(self):
        baseline, candidate = self._routes(
            [("a", vector(1.0)), ("b", vector(2.0))],
            [("b", vector(2.0)), ("a", vector(1.0))],
        )
        comparison = compare_routes(baseline, candidate)
        self.assertEqual(comparison.identical, 2)
        self.assertEqual(comparison.max_abs_diff, 0.0)


class GroupingTests(unittest.TestCase):
    def test_repeats_of_one_text_collapse_when_vectors_agree(self):
        with TemporaryDirectory() as tmp:
            root = write_jsonl(
                Path(tmp),
                "run",
                [
                    record(
                        [
                            ("chunk-01-repeat-1", vector(1.0)),
                            ("chunk-01-repeat-2", vector(1.0)),
                            ("chunk-02-repeat-1", vector(5.0)),
                            ("chunk-02-repeat-2", vector(6.0)),
                        ]
                    )
                ],
            )
            route = load_route(root, "run")
        self.assertEqual(
            distinct_vectors_by_group(route), {"chunk-01": 1, "chunk-02": 2}
        )


if __name__ == "__main__":
    unittest.main()
