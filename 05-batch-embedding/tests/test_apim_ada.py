import json
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from azure.core.exceptions import ResourceNotFoundError

from batch_embeddings import (
    ModelKey,
    PackingMode as AmlPackingMode,
    experiment_name,
    job_name,
    repeat_jsonl_inputs,
)
from component.embed import DEFAULTS as COMPONENT_DEFAULTS
from component.embed import LIMITS as COMPONENT_LIMITS
from component.embed import PackingMode as ComponentPackingMode
from component.embed import REQUEST as COMPONENT_REQUEST

from apim_ada_analyze import (
    FIELDS as ANALYSIS_FIELDS,
    OUTPUT as ANALYSIS_OUTPUT,
    SUMMARY as ANALYSIS_SUMMARY,
    ThrottleClassification,
    analyze_run,
    classify_throttle,
    percentile,
)
from apim_ada_poc import POC, api_policy
from apim_ada_load import LOAD, cosine_similarity, retry_after_seconds, targets
from permissions_setup import RbacRole, RequiredAssignment, ensure_assignment
from utils.embedding_optimization import (
    PLAN,
    REQUEST,
    capacity_units_to_tpm,
    embedding_request,
    input_pacing_interval_seconds,
    pack_compatible_requests,
    pacing_interval_seconds,
    percentile as optimization_percentile,
    target_tokens_per_request,
    token_counter_for_model,
    tokens_per_minute,
    utilization_target_tpm,
)
from utils.aml_metrics import (
    METRICS,
    RequestMeasurement,
    calculate_run_metrics,
    publish_run_metrics,
)


@dataclass(frozen=True)
class TestValues:
    model_ada: str = "text-embedding-ada-002"
    model_small: str = "text-embedding-3-small"
    model_large: str = "text-embedding-3-large"
    deployment: str = "ada-deployment"
    primary_account: str = "primary-account"
    secondary_account: str = "secondary-account"
    apim_resource_group: str = "apim-rg"
    apim_name: str = "apim-name"
    api_version: str = "2024-02-01"
    gateway_url: str = "https://gateway.example"
    subscription_key: str = "test-key"
    token: str = "test-token"


VALUES = TestValues()


def write_jsonl(path: Path, records: tuple[dict, ...]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class FakeCredential:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, scope: str) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(token=VALUES.token)


def context() -> SimpleNamespace:
    settings = SimpleNamespace(
        primary_account_name=VALUES.primary_account,
        secondary_account_name=VALUES.secondary_account,
        deployment_name=VALUES.deployment,
        api_version=VALUES.api_version,
        apim_resource_group=VALUES.apim_resource_group,
        apim_name=VALUES.apim_name,
    )
    subscription = Mock()
    subscription.list_secrets.return_value = SimpleNamespace(
        primary_key=VALUES.subscription_key
    )
    return SimpleNamespace(
        settings=settings,
        gateway_url=VALUES.gateway_url,
        apim_client=SimpleNamespace(subscription=subscription),
    )


class Response:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class LoadContractTests(unittest.TestCase):
    def test_rate_limit_header_mapping_is_explicit_and_complete(self) -> None:
        self.assertEqual(
            dict(LOAD.rate_limit_headers),
            {
                "x-ratelimit-limit-requests": "rate_limit_limit_requests",
                "x-ratelimit-remaining-requests": "rate_limit_remaining_requests",
                "x-ratelimit-reset-requests": "rate_limit_reset_requests",
                "x-ratelimit-limit-tokens": "rate_limit_limit_tokens",
                "x-ratelimit-remaining-tokens": "rate_limit_remaining_tokens",
                "x-ratelimit-reset-tokens": "rate_limit_reset_tokens",
            },
        )

    def test_retry_after_normalizes_milliseconds_and_seconds(self) -> None:
        self.assertEqual(
            retry_after_seconds(Response({LOAD.retry_after_ms_header: "1500"})),
            1.5,
        )
        self.assertEqual(
            retry_after_seconds(Response({LOAD.retry_after_header: "12"})),
            12.0,
        )
        self.assertIsNone(
            retry_after_seconds(Response({LOAD.retry_after_header: "invalid"}))
        )

    def test_gateway_target_does_not_acquire_foundry_token(self) -> None:
        credential = FakeCredential()
        selected = targets(context(), credential, (LOAD.gateway_target,))
        self.assertEqual(set(selected), {LOAD.gateway_target})
        self.assertEqual(credential.calls, 0)

    def test_direct_target_does_not_retrieve_apim_key(self) -> None:
        credential = FakeCredential()
        test_context = context()
        selected = targets(test_context, credential, (LOAD.primary_target,))
        self.assertEqual(set(selected), {LOAD.primary_target})
        self.assertEqual(credential.calls, 1)
        test_context.apim_client.subscription.list_secrets.assert_not_called()

    def test_cosine_similarity_rejects_zero_norm(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero-norm"):
            cosine_similarity([0.0, 0.0], [1.0, 0.0])


class OptimizationUtilityTests(unittest.TestCase):
    def test_packing_groups_compatible_requests_and_chunks_inputs(self) -> None:
        requests = [
            embedding_request(
                input_ids=f"id-{index}",
                texts=f"text-{index}",
                model=VALUES.model_ada,
            )
            for index in range(5)
        ]

        packed = list(pack_compatible_requests(requests, "batch", 2))

        self.assertEqual(
            [item[REQUEST.input_count] for item in packed],
            [2, 2, 1],
        )
        self.assertEqual(
            [
                input_id
                for item in packed
                for input_id in item[REQUEST.input_ids]
            ],
            [f"id-{index}" for index in range(5)],
        )

    def test_packing_keeps_different_settings_separate(self) -> None:
        requests = [
            embedding_request(
                input_ids="float",
                texts="one",
                model=VALUES.model_ada,
                encoding_format="float",
            ),
            embedding_request(
                input_ids="base64",
                texts="two",
                model=VALUES.model_ada,
                encoding_format="base64",
            ),
        ]

        packed = list(pack_compatible_requests(requests, "batch", 10))

        self.assertEqual(len(packed), 2)

    def test_pacing_and_throughput_share_inverse_contract(self) -> None:
        interval = pacing_interval_seconds(1400, 24000)
        self.assertEqual(interval, 3.5)
        self.assertEqual(tokens_per_minute(1400, interval), 24000)
        self.assertEqual(input_pacing_interval_seconds(100, 720), 100 / 12)

    def test_shared_percentile_validates_boundaries(self) -> None:
        self.assertIsNone(optimization_percentile([], 50))
        self.assertEqual(optimization_percentile([0.0, 10.0], 50), 5.0)
        with self.assertRaises(ValueError):
            optimization_percentile([1.0], 101)

    def test_token_target_splits_before_exceeding_ceiling(self) -> None:
        requests = [
            embedding_request(
                input_ids=f"id-{index}",
                texts=text,
                model=VALUES.model_ada,
            )
            for index, text in enumerate(("aaaa", "bbbb", "cc", "dddd"))
        ]

        packed = list(
            pack_compatible_requests(
                requests,
                "batch",
                max_inputs_per_request=10,
                max_tokens_per_request=6,
                count_tokens=len,
            )
        )

        self.assertEqual(
            [item[REQUEST.estimated_tokens] for item in packed],
            [4, 6, 4],
        )

    def test_ada_token_counter_counts_nonempty_text(self) -> None:
        counter = token_counter_for_model(VALUES.model_ada)
        self.assertGreater(counter("Azure embedding input"), 0)

    def test_capacity_drives_utilization_and_request_target(self) -> None:
        aggregate_tpm = capacity_units_to_tpm(15) + capacity_units_to_tpm(15)
        target_tpm = utilization_target_tpm(aggregate_tpm, 0.8)

        self.assertEqual(aggregate_tpm, 30000)
        self.assertEqual(target_tpm, 24000)
        self.assertEqual(target_tokens_per_request(target_tpm, 20), 1200)
        self.assertEqual(PLAN.assigned_tpm, "assigned_tpm")
        self.assertEqual(PLAN.max_batch_tokens, "max_batch_tokens")


class AmlMetricTests(unittest.TestCase):
    def test_run_metrics_separate_attempts_success_and_throttling(self) -> None:
        metrics = calculate_run_metrics(
            [
                RequestMeasurement(0.0, 2.0, 80, 1000, 1000, 200),
                RequestMeasurement(2.0, 4.0, 20, 400, 0, 429),
            ],
            max_inputs_per_request=100,
            max_tokens_per_request=1200,
            target_tpm=12000,
            target_inputs_per_minute=720,
        )

        self.assertEqual(metrics[METRICS.attempted_rpm], 30.0)
        self.assertEqual(metrics[METRICS.configured_target_tpm], 12000.0)
        self.assertEqual(
            metrics[METRICS.configured_target_inputs_per_minute],
            720.0,
        )
        self.assertEqual(metrics[METRICS.successful_rpm], 15.0)
        self.assertEqual(metrics[METRICS.accepted_tpm], 15000.0)
        self.assertEqual(metrics[METRICS.success_rate], 0.5)
        self.assertEqual(metrics[METRICS.throttle_rate], 0.5)
        self.assertEqual(metrics[METRICS.attempted_logical_inputs], 100.0)
        self.assertEqual(metrics[METRICS.successful_logical_inputs], 80.0)
        self.assertEqual(metrics[METRICS.logical_inputs_per_minute], 1200.0)
        self.assertEqual(metrics[METRICS.inputs_per_request], 50.0)
        self.assertEqual(metrics[METRICS.token_ceiling_fill_ratio], 1_000 / 1_200)
        self.assertEqual(metrics[METRICS.item_ceiling_fill_ratio], 0.5)
        self.assertEqual(metrics[METRICS.estimated_to_actual_token_ratio], 1.0)
        self.assertEqual(metrics[METRICS.request_latency_p95_ms], 2000.0)

    def test_metric_publishing_is_namespaced_and_configurable(self) -> None:
        logger = Mock()
        metrics = {METRICS.attempted_rpm: 20.0, METRICS.accepted_tpm: 14_000.0}

        published = publish_run_metrics(
            metrics,
            "mlflow",
            "ada_batch",
            logger=logger,
        )

        self.assertEqual(
            published,
            {
                "ada_batch.attempted_rpm": 20.0,
                "ada_batch.accepted_tpm": 14_000.0,
            },
        )
        logger.log_metrics.assert_called_once_with(published)

        logger.reset_mock()
        self.assertEqual(
            publish_run_metrics(metrics, "disabled", logger=logger),
            {},
        )
        logger.log_metrics.assert_not_called()

    def test_metric_prefix_rejects_unsafe_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "metric_prefix"):
            publish_run_metrics(
                {METRICS.attempted_rpm: 20.0},
                "mlflow",
                "invalid prefix",
                logger=Mock(),
            )


class AnalyzerContractTests(unittest.TestCase):
    def test_throttle_classifications(self) -> None:
        cases = (
            ({ANALYSIS_FIELDS.status_code: 429, ANALYSIS_FIELDS.error_message: "call rate limit"}, ThrottleClassification.RPM_EXPLICIT),
            ({ANALYSIS_FIELDS.status_code: 429, ANALYSIS_FIELDS.error_message: "token rate limit"}, ThrottleClassification.TPM_EXPLICIT),
            ({ANALYSIS_FIELDS.status_code: 429, ANALYSIS_FIELDS.rate_limit_remaining_requests: "0", ANALYSIS_FIELDS.rate_limit_remaining_tokens: "1"}, ThrottleClassification.RPM_LIKELY),
            ({ANALYSIS_FIELDS.status_code: 429, ANALYSIS_FIELDS.rate_limit_remaining_requests: "1", ANALYSIS_FIELDS.rate_limit_remaining_tokens: "0"}, ThrottleClassification.TPM_LIKELY),
            ({ANALYSIS_FIELDS.status_code: 429, ANALYSIS_FIELDS.rate_limit_remaining_requests: "0", ANALYSIS_FIELDS.rate_limit_remaining_tokens: "0"}, ThrottleClassification.UNKNOWN),
            ({ANALYSIS_FIELDS.status_code: 429}, ThrottleClassification.UNKNOWN),
        )
        for record, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_throttle(record)[ANALYSIS_OUTPUT.classification],
                    expected,
                )

    def test_percentile_interpolates(self) -> None:
        self.assertEqual(percentile([], 50), None)
        self.assertEqual(percentile([10.0], 95), 10.0)
        self.assertEqual(percentile([0.0, 10.0], 50), 5.0)

    def test_analysis_latency_excludes_failed_requests(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            (run_dir / "summary.json").write_text(
                json.dumps(
                    {
                        ANALYSIS_SUMMARY.mode: "load",
                        ANALYSIS_SUMMARY.target: LOAD.primary_target,
                        ANALYSIS_SUMMARY.steady_state_tpm: 800,
                        ANALYSIS_SUMMARY.optimization_plan: {
                            PLAN.assigned_tpm: 1000,
                            PLAN.max_batch_inputs: 4,
                            PLAN.max_batch_tokens: 100,
                        },
                    }
                ),
                encoding="utf-8",
            )
            records = (
                {
                    ANALYSIS_FIELDS.status_code: 200,
                    ANALYSIS_FIELDS.duration_ms: 10,
                    REQUEST.input_count: 4,
                    ANALYSIS_FIELDS.estimated_tokens: 80,
                    ANALYSIS_FIELDS.prompt_tokens: 80,
                },
                {
                    ANALYSIS_FIELDS.status_code: 200,
                    ANALYSIS_FIELDS.duration_ms: 20,
                    REQUEST.input_count: 2,
                    ANALYSIS_FIELDS.estimated_tokens: 40,
                    ANALYSIS_FIELDS.prompt_tokens: 40,
                },
                {ANALYSIS_FIELDS.status_code: 429, ANALYSIS_FIELDS.duration_ms: 1000, REQUEST.input_count: 1},
            )
            write_jsonl(run_dir / "requests.jsonl", records)

            analysis = analyze_run(run_dir)

            self.assertEqual(
                analysis[ANALYSIS_OUTPUT.latency_ms][ANALYSIS_OUTPUT.p50],
                15.0,
            )
            scorecard = analysis[ANALYSIS_OUTPUT.optimization_scorecard]
            self.assertEqual(
                scorecard[ANALYSIS_OUTPUT.item_capacity_fill_ratio],
                0.75,
            )
            self.assertEqual(
                scorecard[ANALYSIS_OUTPUT.estimated_token_fill_ratio],
                0.6,
            )
            self.assertEqual(
                scorecard[ANALYSIS_OUTPUT.actual_token_fill_ratio],
                0.6,
            )
            self.assertEqual(
                scorecard[ANALYSIS_OUTPUT.logical_inputs_per_http_request],
                2.0,
            )
            self.assertEqual(
                scorecard[ANALYSIS_OUTPUT.steady_state_capacity_utilization],
                0.8,
            )


class RbacContractTests(unittest.TestCase):
    def test_not_found_assignment_is_created(self) -> None:
        role_definition = SimpleNamespace(id="/roles/openai-user")
        role_definitions = Mock()
        role_definitions.list.return_value = [role_definition]
        role_assignments = Mock()
        role_assignments.get.side_effect = ResourceNotFoundError("missing")
        authorization_client = SimpleNamespace(
            role_definitions=role_definitions,
            role_assignments=role_assignments,
        )
        assignment = RequiredAssignment(
            principal_name="apim:test",
            principal_id="principal-id",
            role_name=RbacRole.COGNITIVE_SERVICES_OPENAI_USER,
            scope="/subscriptions/test/resourceGroups/test/providers/test/account",
        )

        ensure_assignment(authorization_client, assignment)

        role_assignments.create.assert_called_once()


class ApimPolicyTests(unittest.TestCase):
    def test_aml_policy_validates_compute_but_local_policy_does_not(self) -> None:
        test_context = SimpleNamespace(
            tenant_id="tenant-id",
            aml_compute_principal_id="compute-principal-id",
            settings=SimpleNamespace(api_version="2024-02-01"),
        )

        local_policy = api_policy(test_context)
        aml_policy = api_policy(test_context, validate_aml_compute=True)

        self.assertNotIn("validate-azure-ad-token", local_policy)
        self.assertIn("validate-azure-ad-token", aml_policy)
        self.assertIn("compute-principal-id", aml_policy)
        self.assertIn(POC.aml_client_token_audience, aml_policy)
        self.assertIn(POC.pool_backend_id, local_policy)
        self.assertIn(POC.pool_backend_id, aml_policy)


class AmlNamingTests(unittest.TestCase):
    def test_experiment_name_is_stable_and_human_readable(self) -> None:
        self.assertEqual(
            experiment_name("ada-apim", "batch", "tpm"),
            "embeddings-tpm-ada-apim-packed-input-array",
        )

    def test_job_name_describes_run_settings(self) -> None:
        value = job_name(
            model_key="ada-apim",
            packing="batch",
            experiment_kind="tpm",
            record_count=200,
            max_inputs_per_request=128,
            max_tokens_per_request=1200,
            max_retries=0,
            request_concurrency=1,
            timestamp=datetime(2026, 8, 3, 13, 15, 0, tzinfo=UTC),
        )

        self.assertEqual(
            value,
            "embeddings-tpm-ada-apim-packed-input-array-records-200-items-128-"
            "tokens-1200-"
            "retries-0-workers-1-2026-08-03-131500z",
        )

    def test_one_input_per_request_label_is_explicit(self) -> None:
        self.assertEqual(
            experiment_name("ada", "none", "rpm"),
            "embeddings-rpm-ada-one-input-per-request",
        )

    def test_experiment_kind_separates_same_route_and_packing(self) -> None:
        self.assertNotEqual(
            experiment_name("ada", "batch", "rpm"),
            experiment_name("ada", "batch", "tpm"),
        )

    def test_public_model_and_packing_values_remain_stable(self) -> None:
        self.assertEqual(ModelKey.ADA_APIM, "ada-apim")
        self.assertEqual(AmlPackingMode.ONE_INPUT_PER_REQUEST, "none")
        self.assertEqual(AmlPackingMode.PACKED_INPUT_ARRAY, "batch")
        self.assertEqual(ComponentPackingMode.ONE_INPUT_PER_REQUEST, "none")
        self.assertEqual(ComponentPackingMode.PACKED_INPUT_ARRAY, "batch")
        self.assertEqual(COMPONENT_DEFAULTS.max_inputs_per_request, 128)
        self.assertEqual(COMPONENT_LIMITS.max_array_inputs, 2048)

    def test_single_pass_preparation_rewrites_model_and_preserves_id(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            write_jsonl(
                source / "sample.jsonl",
                (
                    {
                        COMPONENT_REQUEST.input_id: "chunk-001",
                        COMPONENT_REQUEST.input: "text",
                        COMPONENT_REQUEST.model: VALUES.model_small,
                    },
                ),
            )

            count = repeat_jsonl_inputs(
                source,
                destination,
                repetitions=1,
                model=VALUES.model_ada,
            )
            row = json.loads((destination / "sample.jsonl").read_text())

            self.assertEqual(count, 1)
            self.assertEqual(row[COMPONENT_REQUEST.input_id], "chunk-001")
            self.assertEqual(row[COMPONENT_REQUEST.model], VALUES.model_ada)


if __name__ == "__main__":
    unittest.main()