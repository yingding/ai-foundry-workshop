import argparse
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from azure.ai.ml import Input, Output, MLClient, command, dsl
from azure.ai.ml.constants import AssetTypes, ManagedServiceIdentityType
from azure.ai.ml.entities import (
    AmlCompute,
    BatchEndpoint,
    Environment,
    IdentityConfiguration,
    PipelineComponentBatchDeployment,
)
from azure.ai.ml.exceptions import JobException
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.cognitiveservices.models import (
    Deployment,
    DeploymentModel,
    DeploymentProperties,
    Sku,
)
from dotenv import load_dotenv
from network_setup import configure_private_network
from permissions_setup import configure_permissions
from utils.fdyauth import AuthHelper
from utils.aml_metrics import DEFAULT_METRIC_PREFIX, METRICS, MetricLoggingMode
from utils.embedding_optimization import percentile

ROOT = Path(__file__).resolve().parent


class ModelKey(StrEnum):
    SMALL = "small"
    LARGE = "large"
    ADA = "ada"
    ADA_APIM = "ada-apim"


class PackingMode(StrEnum):
    ONE_INPUT_PER_REQUEST = "none"
    PACKED_INPUT_ARRAY = "batch"


@dataclass(frozen=True)
class EnvironmentKeys:
    subscription_id: str = "AZURE_SUBSCRIPTION_ID"
    aml_resource_group: str = "AML_RESOURCE_GROUP"
    aml_workspace: str = "AML_WORKSPACE_NAME"
    aml_compute: str = "AML_COMPUTE_NAME"
    aml_endpoint: str = "AML_ENDPOINT_NAME"
    aml_small_deployment: str = "AML_SMALL_DEPLOYMENT_NAME"
    aml_large_deployment: str = "AML_LARGE_DEPLOYMENT_NAME"
    aml_ada_deployment: str = "AML_ADA_DEPLOYMENT_NAME"
    aml_ada_apim_deployment: str = "AML_ADA_APIM_DEPLOYMENT_NAME"
    foundry_resource_group: str = "FOUNDRY_RESOURCE_GROUP"
    foundry_account: str = "FOUNDRY_ACCOUNT_NAME"
    foundry_project: str = "FOUNDRY_PROJECT_NAME"
    foundry_project_endpoint: str = "FOUNDRY_PROJECT_ENDPOINT"
    openai_endpoint: str = "AZURE_OPENAI_ENDPOINT"
    openai_small_deployment: str = "AZURE_OPENAI_SMALL_DEPLOYMENT"
    openai_large_deployment: str = "AZURE_OPENAI_LARGE_DEPLOYMENT"
    openai_ada_deployment: str = "AZURE_OPENAI_ADA_DEPLOYMENT"
    openai_small_model: str = "AZURE_OPENAI_SMALL_MODEL"
    openai_large_model: str = "AZURE_OPENAI_LARGE_MODEL"
    openai_ada_model: str = "AZURE_OPENAI_ADA_MODEL"
    openai_model_version: str = "AZURE_OPENAI_MODEL_VERSION"
    openai_ada_model_version: str = "AZURE_OPENAI_ADA_MODEL_VERSION"
    apim_aml_endpoint: str = "APIM_AML_ENDPOINT"
    openai_sku: str = "AZURE_OPENAI_SKU"
    openai_capacity: str = "AZURE_OPENAI_CAPACITY"


@dataclass(frozen=True)
class BatchDefaults:
    ada_aml_deployment: str = "embedding-ada-v1"
    ada_openai_deployment: str = "text-embedding-ada-002-test"
    ada_model: str = "text-embedding-ada-002"
    ada_model_version: str = "2"
    direct_token_scope: str = "https://ai.azure.com/.default"
    apim_token_scope: str = "https://cognitiveservices.azure.com/.default"
    packing: PackingMode = PackingMode.PACKED_INPUT_ARRAY
    max_inputs_per_request: int = 128
    max_tokens_per_request: int = 0
    max_retries: int = 8
    request_concurrency: int = 1
    metric_logging: MetricLoggingMode = MetricLoggingMode.MLFLOW
    local_metric_logging: MetricLoggingMode = MetricLoggingMode.DISABLED
    metric_prefix: str = DEFAULT_METRIC_PREFIX
    max_repeat_inputs: int = 100
    compute_size: str = "Standard_DS3_v2"
    compute_max_instances: int = 2
    compute_idle_seconds: int = 120
    environment_image: str = "mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu22.04:latest"


@dataclass(frozen=True)
class PipelineFields:
    documents: str = "documents"
    packing: str = "packing"
    max_inputs_per_request: str = "max_inputs_per_request"
    max_tokens_per_request: str = "max_tokens_per_request"
    max_retries: str = "max_retries"
    request_concurrency: str = "request_concurrency"
    metric_logging: str = "metric_logging"
    metric_prefix: str = "metric_prefix"
    embeddings: str = "embeddings"


@dataclass(frozen=True)
class ArtifactContract:
    embeddings_file: str = "embeddings.jsonl"
    trace_file: str = "trace.jsonl"
    named_outputs: str = "named-outputs"
    artifacts: str = "artifacts"
    root_span: str = "batch.embed"
    request_span: str = "embeddings.create"


ENV = EnvironmentKeys()
DEFAULTS = BatchDefaults()
FIELDS = PipelineFields()
ARTIFACTS = ArtifactContract()
MODEL_KEYS = tuple(ModelKey)
FOUNDRY_MODEL_KEYS = (ModelKey.SMALL, ModelKey.LARGE, ModelKey.ADA)


def packing_label(packing: str) -> str:
    if packing == PackingMode.PACKED_INPUT_ARRAY:
        return "packed-input-array"
    if packing == PackingMode.ONE_INPUT_PER_REQUEST:
        return "one-input-per-request"
    raise ValueError(f"Unsupported packing mode: {packing}")


def experiment_name(model_key: str, packing: str) -> str:
    """Return a stable portal grouping for comparable embedding runs."""
    return f"embeddings-{model_key}-{packing_label(packing)}"


def job_name(
    model_key: str,
    packing: str,
    record_count: int,
    max_inputs_per_request: int,
    max_tokens_per_request: int,
    max_retries: int,
    request_concurrency: int,
    timestamp: datetime,
) -> str:
    """Return a unique, readable AML batch job name with key run settings."""
    mode_label = packing_label(packing)
    token_label = str(max_tokens_per_request) if max_tokens_per_request else "off"
    return (
        f"embeddings-{model_key}-{mode_label}-records-{record_count}-"
        f"items-{max_inputs_per_request}-tokens-{token_label}-"
        f"retries-{max_retries}-workers-{request_concurrency}-"
        f"{timestamp.strftime('%Y-%m-%d-%H%M%Sz')}"
    )


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.subscription_id = required(ENV.subscription_id)
        self.aml_resource_group = required(ENV.aml_resource_group)
        self.aml_workspace = required(ENV.aml_workspace)
        self.compute_name = required(ENV.aml_compute)
        self.endpoint_name = required(ENV.aml_endpoint)
        self.batch_deployments = {
            ModelKey.SMALL: required(ENV.aml_small_deployment),
            ModelKey.LARGE: required(ENV.aml_large_deployment),
            ModelKey.ADA: os.getenv(
                ENV.aml_ada_deployment,
                DEFAULTS.ada_aml_deployment,
            ),
            ModelKey.ADA_APIM: required(ENV.aml_ada_apim_deployment),
        }
        self.foundry_resource_group = required(ENV.foundry_resource_group)
        self.foundry_account = required(ENV.foundry_account)
        self.foundry_project = required(ENV.foundry_project)
        self.foundry_project_endpoint = required(ENV.foundry_project_endpoint)
        self.openai_endpoint = required(ENV.openai_endpoint)
        self.openai_deployments = {
            ModelKey.SMALL: required(ENV.openai_small_deployment),
            ModelKey.LARGE: required(ENV.openai_large_deployment),
            ModelKey.ADA: os.getenv(
                ENV.openai_ada_deployment,
                DEFAULTS.ada_openai_deployment,
            ),
            ModelKey.ADA_APIM: os.getenv(
                ENV.openai_ada_deployment,
                DEFAULTS.ada_openai_deployment,
            ),
        }
        self.openai_models = {
            ModelKey.SMALL: required(ENV.openai_small_model),
            ModelKey.LARGE: required(ENV.openai_large_model),
            ModelKey.ADA: os.getenv(ENV.openai_ada_model, DEFAULTS.ada_model),
            ModelKey.ADA_APIM: os.getenv(
                ENV.openai_ada_model,
                DEFAULTS.ada_model,
            ),
        }
        embedding_3_version = required(ENV.openai_model_version)
        self.openai_model_versions = {
            ModelKey.SMALL: embedding_3_version,
            ModelKey.LARGE: embedding_3_version,
            ModelKey.ADA: os.getenv(
                ENV.openai_ada_model_version,
                DEFAULTS.ada_model_version,
            ),
            ModelKey.ADA_APIM: os.getenv(
                ENV.openai_ada_model_version,
                DEFAULTS.ada_model_version,
            ),
        }
        ada_apim_base = required(ENV.apim_aml_endpoint).rstrip("/")
        self.openai_endpoints = {
            ModelKey.SMALL: self.openai_endpoint,
            ModelKey.LARGE: self.openai_endpoint,
            ModelKey.ADA: self.openai_endpoint,
            ModelKey.ADA_APIM: (
                f"{ada_apim_base}/deployments/"
                f"{self.openai_deployments[ModelKey.ADA_APIM]}"
            ),
        }
        self.token_scopes = {
            ModelKey.SMALL: DEFAULTS.direct_token_scope,
            ModelKey.LARGE: DEFAULTS.direct_token_scope,
            ModelKey.ADA: DEFAULTS.direct_token_scope,
            ModelKey.ADA_APIM: DEFAULTS.apim_token_scope,
        }
        self.openai_sku = required(ENV.openai_sku)
        self.openai_capacity = int(required(ENV.openai_capacity))

    @property
    def foundry_scope(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.foundry_resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.foundry_account}"
        )


def clients(settings: Settings):
    credential = AuthHelper.test_credential()
    ml_client = MLClient(
        credential,
        settings.subscription_id,
        settings.aml_resource_group,
        settings.aml_workspace,
    )
    cognitive_client = CognitiveServicesManagementClient(credential, settings.subscription_id)
    authorization_client = AuthorizationManagementClient(credential, settings.subscription_id)
    return ml_client, cognitive_client, authorization_client


def print_plan(settings: Settings) -> None:
    print("Foundry new project:")
    print(f"  account:    {settings.foundry_account}")
    print(f"  project:    {settings.foundry_project}")
    for model_key in MODEL_KEYS:
        route = (
            "APIM pooled"
            if model_key == ModelKey.ADA_APIM
            else "Foundry direct"
        )
        print(
            f"  {model_key}:      {route} -> "
            f"{settings.openai_endpoints[model_key]} "
            f"[{settings.openai_deployments[model_key]} / "
            f"{settings.openai_models[model_key]}:{settings.openai_model_versions[model_key]}]"
        )
    print("Shared AML batch endpoint:")
    print(f"  workspace:  {settings.aml_workspace}")
    print(f"  compute:    {settings.compute_name}")
    print(f"  endpoint:   {settings.endpoint_name}")
    print(f"  deployments:{settings.batch_deployments}")


def ensure_model_deployment(
    settings: Settings,
    cognitive_client: CognitiveServicesManagementClient,
    model_key: str,
) -> None:
    deployment_name = settings.openai_deployments[model_key]
    model_name = settings.openai_models[model_key]
    model_version = settings.openai_model_versions[model_key]
    try:
        existing = cognitive_client.deployments.get(
            settings.foundry_resource_group,
            settings.foundry_account,
            deployment_name,
        )
        existing_model = existing.properties.model.name if existing.properties.model else None
        if existing_model != model_name:
            raise RuntimeError(
                f"Foundry deployment {deployment_name} targets {existing_model}, expected {model_name}"
            )
        print(f"Ready: existing Foundry deployment {deployment_name}")
        return
    except ResourceNotFoundError:
        pass

    deployment = Deployment(
        sku=Sku(name=settings.openai_sku, capacity=settings.openai_capacity),
        properties=DeploymentProperties(
            model=DeploymentModel(
                format="OpenAI",
                name=model_name,
                version=model_version,
            ),
            version_upgrade_option="NoAutoUpgrade",
        ),
    )
    cognitive_client.deployments.begin_create_or_update(
        settings.foundry_resource_group,
        settings.foundry_account,
        deployment_name,
        deployment,
    ).result()
    print(f"Ready: Foundry deployment {deployment_name}")


def ensure_compute(settings: Settings, ml_client: MLClient) -> str:
    try:
        compute = ml_client.compute.get(settings.compute_name)
    except ResourceNotFoundError:
        compute = ml_client.compute.begin_create_or_update(
            AmlCompute(
                name=settings.compute_name,
                size=DEFAULTS.compute_size,
                min_instances=0,
                max_instances=DEFAULTS.compute_max_instances,
                idle_time_before_scale_down=DEFAULTS.compute_idle_seconds,
                identity=IdentityConfiguration(type=ManagedServiceIdentityType.SYSTEM_ASSIGNED),
            )
        ).result()

    if not compute.identity or not compute.identity.principal_id:
        raise RuntimeError(f"Compute {settings.compute_name} has no system-assigned principal ID")
    print(f"Ready: AML compute {settings.compute_name}")
    return compute.identity.principal_id


def create_pipeline_component(
    settings: Settings,
    model_key: str,
    code_path: str | Path = ROOT,
    default_max_tokens_per_request: int = 0,
):
    model_slug = model_key.replace("-", "_")
    environment = Environment(
        image=DEFAULTS.environment_image,
        conda_file=ROOT / "component" / "conda.yaml",
    )
    embed = command(
        name=f"embed_documents_{model_slug}_openai_v1",
        display_name=f"Embed documents with {settings.openai_models[model_key]}",
        code=code_path,
        command=(
            "PYTHONPATH=. python component/embed.py --input-dir ${{inputs.documents}} "
            "--output-dir ${{outputs.embeddings}} "
            f"--endpoint {settings.openai_endpoints[model_key]} "
            f"--deployment {settings.openai_deployments[model_key]} "
            f"--model {settings.openai_models[model_key]} "
            f"--token-scope {settings.token_scopes[model_key]} "
            "--metric-logging ${{inputs.metric_logging}} "
            "--metric-prefix ${{inputs.metric_prefix}} "
            "--packing ${{inputs.packing}} "
            "--max-inputs-per-request ${{inputs.max_inputs_per_request}} "
            "--max-tokens-per-request ${{inputs.max_tokens_per_request}} "
            "--max-retries ${{inputs.max_retries}} "
            "--request-concurrency ${{inputs.request_concurrency}}"
        ),
        inputs={
            FIELDS.documents: Input(type=AssetTypes.URI_FOLDER),
            FIELDS.packing: Input(
                type="string",
                default=DEFAULTS.packing.value,
            ),
            FIELDS.max_inputs_per_request: Input(
                type="integer",
                default=DEFAULTS.max_inputs_per_request,
            ),
            FIELDS.max_tokens_per_request: Input(
                type="integer",
                default=default_max_tokens_per_request,
            ),
            FIELDS.max_retries: Input(
                type="integer",
                default=DEFAULTS.max_retries,
            ),
            FIELDS.request_concurrency: Input(
                type="integer",
                default=DEFAULTS.request_concurrency,
            ),
            FIELDS.metric_logging: Input(
                type="string",
                default=DEFAULTS.metric_logging.value,
            ),
            FIELDS.metric_prefix: Input(
                type="string",
                default=DEFAULTS.metric_prefix,
            ),
        },
        outputs={FIELDS.embeddings: Output(type=AssetTypes.URI_FOLDER)},
        environment=environment,
        is_deterministic=False,
    )

    @dsl.pipeline(name=f"batch_embedding_pipeline_{model_slug}")
    def pipeline(
        documents: Input,
        packing: str = DEFAULTS.packing.value,
        max_inputs_per_request: int = DEFAULTS.max_inputs_per_request,
        max_tokens_per_request: int = default_max_tokens_per_request,
        max_retries: int = DEFAULTS.max_retries,
        request_concurrency: int = DEFAULTS.request_concurrency,
        metric_logging: str = DEFAULTS.metric_logging.value,
        metric_prefix: str = DEFAULTS.metric_prefix,
    ):
        step = embed(
            documents=documents,
            packing=packing,
            max_inputs_per_request=max_inputs_per_request,
            max_tokens_per_request=max_tokens_per_request,
            max_retries=max_retries,
            request_concurrency=request_concurrency,
            metric_logging=metric_logging,
            metric_prefix=metric_prefix,
        )
        return {FIELDS.embeddings: step.outputs.embeddings}

    return pipeline(
        documents=Input(type=AssetTypes.URI_FOLDER),
        packing=DEFAULTS.packing.value,
        max_inputs_per_request=DEFAULTS.max_inputs_per_request,
        max_tokens_per_request=default_max_tokens_per_request,
        max_retries=DEFAULTS.max_retries,
        request_concurrency=DEFAULTS.request_concurrency,
        metric_logging=DEFAULTS.metric_logging.value,
        metric_prefix=DEFAULTS.metric_prefix,
    ).component


def ensure_batch_endpoint(
    settings: Settings,
    ml_client: MLClient,
    model_keys: tuple[str, ...] = MODEL_KEYS,
    max_tokens_per_request: dict[str, int] | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="aml-batch-embedding-multi-") as temp_dir:
        stage = Path(temp_dir)
        (stage / "component").mkdir()
        (stage / "utils").mkdir()
        shutil.copy2(ROOT / "component" / "embed.py", stage / "component" / "embed.py")
        shutil.copy2(ROOT / "utils" / "__init__.py", stage / "utils" / "__init__.py")
        shutil.copy2(ROOT / "utils" / "fdyauth.py", stage / "utils" / "fdyauth.py")
        shutil.copy2(
            ROOT / "utils" / "embedding_optimization.py",
            stage / "utils" / "embedding_optimization.py",
        )
        shutil.copy2(
            ROOT / "utils" / "aml_metrics.py",
            stage / "utils" / "aml_metrics.py",
        )
        try:
            endpoint = ml_client.batch_endpoints.get(settings.endpoint_name)
        except ResourceNotFoundError:
            endpoint = ml_client.batch_endpoints.begin_create_or_update(
                BatchEndpoint(
                    name=settings.endpoint_name,
                    description="Selectable direct and APIM-pooled embeddings",
                )
            ).result()
        for model_key in model_keys:
            component = ml_client.components.create_or_update(
                create_pipeline_component(
                    settings,
                    model_key=model_key,
                    code_path=stage,
                    default_max_tokens_per_request=(
                        (max_tokens_per_request or {}).get(model_key, 0)
                    ),
                )
            )
            deployment = PipelineComponentBatchDeployment(
                name=settings.batch_deployments[model_key],
                endpoint_name=endpoint.name,
                component=component,
                settings={
                    "continue_on_step_failure": False,
                    "default_compute": settings.compute_name,
                },
            )
            ml_client.batch_deployments.begin_create_or_update(deployment).result()
            print(
                f"Ready: batch deployment {model_key} -> "
                f"{'APIM pooled' if model_key == ModelKey.ADA_APIM else 'Foundry direct'} "
                f"({settings.openai_endpoints[model_key]})"
            )
        print("Ready: minimal component code uploaded (5 files)")
        if not endpoint.defaults.deployment_name:
            endpoint.defaults.deployment_name = settings.batch_deployments[ModelKey.SMALL]
            ml_client.batch_endpoints.begin_create_or_update(endpoint).result()
        print(f"Ready: AML batch endpoint {endpoint.name}")


def provision(settings: Settings) -> None:
    ml_client, cognitive_client, authorization_client = clients(settings)
    for model_key in FOUNDRY_MODEL_KEYS:
        ensure_model_deployment(settings, cognitive_client, model_key)
    ensure_compute(settings, ml_client)
    configure_permissions(settings, ml_client, authorization_client)
    ensure_batch_endpoint(settings, ml_client)


def provision_apim_deployment(settings: Settings) -> None:
    from apim_ada_poc import Settings as ApimSettings, build_context

    ml_client, _, _ = clients(settings)
    ensure_compute(settings, ml_client)
    apim_context = build_context(ApimSettings.from_environment())
    ensure_batch_endpoint(
        settings,
        ml_client,
        model_keys=(ModelKey.ADA_APIM,),
        max_tokens_per_request={
            ModelKey.ADA_APIM: apim_context.capacity.target_tokens_per_request
        },
    )


def configure_network(settings: Settings, cidr_prefix: int) -> None:
    credential = AuthHelper.test_credential()
    ml_client = MLClient(
        credential,
        settings.subscription_id,
        settings.aml_resource_group,
        settings.aml_workspace,
    )
    result = configure_private_network(settings, credential, ml_client, cidr_prefix)
    print(f"Ready: NSP {result.nsp_name}/{result.profile_name}")


def setup_permissions(settings: Settings) -> None:
    ml_client, _, authorization_client = clients(settings)
    ensure_compute(settings, ml_client)
    configure_permissions(settings, ml_client, authorization_client)


def monitor(settings: Settings, job_name: str) -> None:
    ml_client, _, _ = clients(settings)
    job = ml_client.jobs.get(job_name)
    print(f"Job {job.name}: {job.status}")
    if job.experiment_name:
        print(f"  run label: {job.experiment_name}")
    children = list(ml_client.jobs.list(parent_job_name=job_name))
    for child in children:
        print(f"  child {child.name} ({child.display_name}): {child.status}")
    try:
        ml_client.jobs.stream(job_name)
    except JobException as error:
        failed_children = list(ml_client.jobs.list(parent_job_name=job_name))
        for child in failed_children:
            print(f"  child {child.name} ({child.display_name}): {child.status}")
            if child.status == "Failed":
                print_failure_trace(ml_client, child.name)
        raise RuntimeError(f"AML batch job {job_name} failed") from error
    print_output_summary(ml_client, job_name)


def print_output_summary(ml_client: MLClient, job_name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="aml-embedding-summary-") as temp_dir:
        destination = Path(temp_dir)
        ml_client.jobs.download(
            name=job_name,
            output_name=FIELDS.embeddings,
            download_path=destination,
        )
        output_dir = destination / ARTIFACTS.named_outputs / FIELDS.embeddings
        results_path = output_dir / ARTIFACTS.embeddings_file
        trace_path = output_dir / ARTIFACTS.trace_file
        if not results_path.exists() or not trace_path.exists():
            print("Output summary unavailable; download the named output for details.")
            return

        results = [json.loads(line) for line in results_path.read_text().splitlines()]
        errors = [result for result in results if "error" in result]
        spans = [json.loads(line) for line in trace_path.read_text().splitlines()]
        batch_span = next(
            (span for span in spans if span["name"] == ARTIFACTS.root_span),
            None,
        )
        if not batch_span:
            print("Output summary unavailable; batch trace span was not found.")
            return

        attributes = batch_span["attributes"]
        request_spans = [
            span for span in spans if span["name"] == ARTIFACTS.request_span
        ]
        request_durations = sorted(
            span["attributes"]["request.duration_ms"]
            for span in request_spans
            if "request.duration_ms" in span["attributes"]
        )
        request_starts = sorted(
            span["attributes"]["request.start_offset_ms"]
            for span in request_spans
            if "request.start_offset_ms" in span["attributes"]
        )
        estimated_batch_tokens = sorted(
            span["attributes"]["batch.estimated_tokens"]
            for span in request_spans
            if "batch.estimated_tokens" in span["attributes"]
        )
        actual_batch_tokens = sorted(
            span["attributes"]["batch.prompt_tokens"]
            for span in request_spans
            if "batch.prompt_tokens" in span["attributes"]
        )
        status_counts = Counter(
            str(span["attributes"]["http.status_code"])
            for span in request_spans
            if "http.status_code" in span["attributes"]
        )
        retry_delays = sorted(
            float(span["attributes"]["http.retry_after_ms"])
            for span in request_spans
            if "http.retry_after_ms" in span["attributes"]
        )
        duration_ms = float(attributes["embedding.duration_ms"])
        print("Embedding output summary:")
        print(f"  packing:         {attributes['embedding.packing']}")
        print(f"  max retries:     {attributes['embedding.max_retries']}")
        print(f"  concurrency:     {attributes['embedding.request_concurrency']}")
        print(f"  source lines:    {attributes['embedding.source_line_count']}")
        print(f"  embedding inputs:{attributes['embedding.input_count']}")
        print(f"  online requests: {attributes['embedding.online_request_count']}")
        print(f"  duration ms:     {duration_ms}")
        print(f"  failed requests: {attributes['embedding.failed_count']}")
        run_metrics = {
            name.removeprefix("metric."): value
            for name, value in attributes.items()
            if name.startswith("metric.")
        }
        if run_metrics:
            print(f"  attempted RPM:   {run_metrics[METRICS.attempted_rpm]:.3f}")
            print(f"  successful RPM:  {run_metrics[METRICS.successful_rpm]:.3f}")
            print(f"  accepted TPM:    {run_metrics[METRICS.accepted_tpm]:.3f}")
            print(f"  success rate:    {run_metrics[METRICS.success_rate]:.3%}")
            print(f"  throttle rate:   {run_metrics[METRICS.throttle_rate]:.3%}")
            print(
                f"  token fill:      "
                f"{run_metrics[METRICS.token_ceiling_fill_ratio]:.3%}"
            )
            print(
                f"  item fill:       "
                f"{run_metrics[METRICS.item_ceiling_fill_ratio]:.3%}"
            )
            print(
                f"  token estimate:  "
                f"{run_metrics[METRICS.estimated_to_actual_token_ratio]:.6f}"
            )
        if request_durations:
            print(f"  latency p50 ms:  {percentile(request_durations, 50):.3f}")
            print(f"  latency p95 ms:  {percentile(request_durations, 95):.3f}")
            print(f"  latency p99 ms:  {percentile(request_durations, 99):.3f}")
        if duration_ms > 0:
            print(
                f"  logical req/s:   "
                f"{len(request_spans) / (duration_ms / 1000):.3f}"
            )
        if request_starts:
            print(f"  start spread ms: {request_starts[-1] - request_starts[0]:.3f}")
            print(f"  peak starts/10s: {peak_window_count(request_starts, 10_000)}")
        if estimated_batch_tokens:
            print(
                "  estimated tokens: "
                f"min={min(estimated_batch_tokens):.0f}, "
                f"p50={percentile(estimated_batch_tokens, 50):.0f}, "
                f"p95={percentile(estimated_batch_tokens, 95):.0f}, "
                f"max={max(estimated_batch_tokens):.0f}"
            )
        if actual_batch_tokens:
            print(
                "  actual tokens:    "
                f"min={min(actual_batch_tokens):.0f}, "
                f"p50={percentile(actual_batch_tokens, 50):.0f}, "
                f"p95={percentile(actual_batch_tokens, 95):.0f}, "
                f"max={max(actual_batch_tokens):.0f}"
            )
        if status_counts:
            formatted_statuses = ", ".join(
                f"{status}={count}" for status, count in sorted(status_counts.items())
            )
            print(f"  HTTP statuses:   {formatted_statuses}")
        if retry_delays:
            print(f"  retry-after ms:  min={min(retry_delays):.0f}, max={max(retry_delays):.0f}")
        for header_name in (
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-reset-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-tokens",
        ):
            values = sorted(
                {
                    str(span["attributes"][f"http.{header_name}"])
                    for span in request_spans
                    if f"http.{header_name}" in span["attributes"]
                }
            )
            if values:
                print(f"  {header_name}: {', '.join(values)}")
        error_counts = Counter(result["error"]["code"] for result in errors)
        for code, count in sorted(error_counts.items()):
            print(f"  error count:     {code}={count}")
        for result in errors[:5]:
            print(
                f"  error {result['error']['code']} for input IDs "
                f"{result.get('input_ids', [])}: {result['error']['message']}"
            )
        if len(errors) > 5:
            print(f"  errors omitted:  {len(errors) - 5}")


def peak_window_count(start_offsets_ms: list[float], window_ms: float) -> int:
    peak = 0
    left = 0
    for right, start_offset in enumerate(start_offsets_ms):
        while start_offset - start_offsets_ms[left] >= window_ms:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def print_failure_trace(ml_client: MLClient, child_job_name: str) -> None:
    destination = ROOT / "outputs" / "jobs" / child_job_name
    if destination.exists():
        shutil.rmtree(destination)
    ml_client.jobs.download(
        name=child_job_name,
        download_path=destination,
        all=True,
    )

    patterns = re.compile(
        r"(Traceback|ModuleNotFoundError|PermissionDenied|StreamAccess|Authentication failed|"
        r"not authorized|HttpResponseError|APIConnectionError|APITimeoutError|Error:)",
        re.IGNORECASE,
    )
    matches: list[str] = []
    for path in sorted((destination / "artifacts").rglob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if patterns.search(text):
            matches.append(f"[{path.relative_to(destination)}]\n{text[-4000:]}")
    for path in sorted((destination / "artifacts").rglob("*.log")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if patterns.search(text):
            relevant = [line for line in text.splitlines() if patterns.search(line)]
            if relevant:
                matches.append(
                    f"[{path.relative_to(destination)}]\n" + "\n".join(relevant[-12:])
                )

    print(f"Failure artifacts: {destination}")
    if matches:
        print("\nFailure trace:\n" + "\n\n".join(matches[:5]))
    else:
        print("No matching failure trace found; inspect the downloaded artifacts directory.")


def invoke(
    settings: Settings,
    input_path: Path,
    model_key: str,
    packing: str,
    max_inputs_per_request: int,
    max_tokens_per_request: int | None,
    max_retries: int,
    request_concurrency: int,
    repeat_inputs: int,
    metric_logging: str,
    metric_prefix: str,
) -> None:
    if repeat_inputs < 1 or repeat_inputs > DEFAULTS.max_repeat_inputs:
        raise ValueError(
            "repeat_inputs must be between 1 and "
            f"{DEFAULTS.max_repeat_inputs}"
        )
    ml_client, _, _ = clients(settings)
    if max_tokens_per_request is None:
        if model_key == ModelKey.ADA_APIM:
            from apim_ada_poc import Settings as ApimSettings, build_context

            max_tokens_per_request = build_context(
                ApimSettings.from_environment()
            ).capacity.target_tokens_per_request
        else:
            max_tokens_per_request = 0
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="embedding-load-test-") as temp_dir:
        upload_path = Path(temp_dir) / "input"
        record_count = repeat_jsonl_inputs(
            input_path,
            upload_path,
            repeat_inputs,
            settings.openai_models[model_key],
        )
        if repeat_inputs > 1:
            print(
                f"Prepared {record_count} load-test records from "
                f"{repeat_inputs} repetitions"
            )
        else:
            print(
                f"Prepared {record_count} records for model "
                f"{settings.openai_models[model_key]}"
            )
        experiment = experiment_name(model_key, packing)
        readable_job_name = job_name(
            model_key,
            packing,
            record_count,
            max_inputs_per_request,
            max_tokens_per_request,
            max_retries,
            request_concurrency,
            started_at,
        )
        job = ml_client.batch_endpoints.invoke(
            endpoint_name=settings.endpoint_name,
            deployment_name=settings.batch_deployments[model_key],
            experiment_name=experiment,
            job_name=readable_job_name,
            inputs={
                FIELDS.documents: Input(
                    type=AssetTypes.URI_FOLDER,
                    path=str(upload_path.resolve()),
                ),
                FIELDS.packing: Input(type="string", default=packing),
                FIELDS.max_inputs_per_request: Input(
                    type="integer", default=max_inputs_per_request
                ),
                FIELDS.max_tokens_per_request: Input(
                    type="integer",
                    default=max_tokens_per_request,
                ),
                FIELDS.max_retries: Input(type="integer", default=max_retries),
                FIELDS.request_concurrency: Input(
                    type="integer", default=request_concurrency
                ),
                FIELDS.metric_logging: Input(
                    type="string",
                    default=metric_logging,
                ),
                FIELDS.metric_prefix: Input(
                    type="string",
                    default=metric_prefix,
                ),
            },
        )
        print(f"Experiment: {experiment}")
        print(f"Requested job name: {readable_job_name}")
        print(
            f"Submitted job ID: {job.name} "
            f"({model_key} -> {settings.openai_deployments[model_key]}, "
            f"packing={packing}, repeats={repeat_inputs}, max_retries={max_retries}, "
            f"concurrency={request_concurrency}, metrics={metric_logging}, "
            f"metric_prefix={metric_prefix})"
        )
        monitor(settings, job.name)


def count_jsonl_records(input_path: Path) -> int:
    record_count = 0
    for source_path in sorted(input_path.rglob("*.jsonl")):
        with source_path.open(encoding="utf-8") as source:
            record_count += sum(1 for line in source if line.strip())
    if record_count == 0:
        raise ValueError(f"No JSONL records found under {input_path}")
    return record_count


def repeat_jsonl_inputs(
    input_path: Path,
    output_path: Path,
    repetitions: int,
    model: str,
) -> int:
    output_path.mkdir(parents=True, exist_ok=True)
    record_count = 0
    for source_path in sorted(input_path.rglob("*.jsonl")):
        destination = output_path / source_path.relative_to(input_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open(encoding="utf-8") as source, destination.open(
            "w", encoding="utf-8"
        ) as output:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                for repetition in range(1, repetitions + 1):
                    suffix = (
                        f"-repeat-{repetition:02d}"
                        if repetitions > 1
                        else ""
                    )
                    input_id = row.get("input_id")
                    if isinstance(input_id, str):
                        repeated_input_id = input_id + suffix
                    elif isinstance(input_id, list):
                        repeated_input_id = [value + suffix for value in input_id]
                    else:
                        raise ValueError(
                            f"{source_path}: input_id must be a string or array"
                        )
                    output.write(
                        json.dumps(
                            {
                                **row,
                                "input_id": repeated_input_id,
                                "model": model,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    record_count += 1
    if record_count == 0:
        raise ValueError(f"No JSONL records found under {input_path}")
    return record_count


def download(settings: Settings, job_name: str, output_path: Path) -> None:
    ml_client, _, _ = clients(settings)
    ml_client.jobs.download(
        name=job_name,
        output_name=FIELDS.embeddings,
        download_path=output_path,
    )
    print(f"Downloaded output to {output_path}")


def test_local(
    settings: Settings,
    input_path: Path,
    output_path: Path,
    model_key: str,
    packing: str,
    max_inputs_per_request: int,
    max_tokens_per_request: int,
    max_retries: int,
    request_concurrency: int,
    metric_logging: str,
    metric_prefix: str,
) -> None:
    from component.embed import run

    run(
        input_dir=input_path,
        output_dir=output_path,
        endpoint=settings.openai_endpoint,
        deployment=settings.openai_deployments[model_key],
        model=settings.openai_models[model_key],
        packing=packing,
        max_inputs_per_request=max_inputs_per_request,
        max_tokens_per_request=max_tokens_per_request,
        max_retries=max_retries,
        request_concurrency=request_concurrency,
        dry_run=True,
        metric_logging=metric_logging,
        metric_prefix=metric_prefix,
    )


def main() -> None:
    load_dotenv(ROOT / "config" / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan", help="Print the resources that would be used")
    network_parser = subparsers.add_parser(
        "network", help="Configure additive NSP and storage firewall access with Azure SDKs"
    )
    network_parser.add_argument("--cidr-prefix", type=int, choices=range(24, 33), default=24)
    subparsers.add_parser(
        "permissions",
        help="Ensure and verify runtime roles for Foundry, AML compute, and storage",
    )
    subparsers.add_parser("provision", help="Create/update the model deployment and AML endpoint")
    subparsers.add_parser(
        "provision-apim",
        help="Create/update only the parallel APIM-pooled ADA AML deployment",
    )
    test_parser = subparsers.add_parser("test", help="Test parsing, outputs, and traces locally")
    test_parser.add_argument("--input", type=Path, default=ROOT / "data")
    test_parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "local-test")
    test_parser.add_argument("--model", choices=MODEL_KEYS, default=ModelKey.SMALL)
    test_parser.add_argument(
        "--packing",
        choices=tuple(PackingMode),
        default=DEFAULTS.packing,
        help=(
            "none sends one input per HTTP request; batch sends packed input arrays"
        ),
    )
    test_parser.add_argument(
        "--max-inputs-per-request",
        type=int,
        default=DEFAULTS.max_inputs_per_request,
    )
    test_parser.add_argument(
        "--max-tokens-per-request",
        type=int,
        default=DEFAULTS.max_tokens_per_request,
    )
    test_parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULTS.max_retries,
    )
    test_parser.add_argument(
        "--request-concurrency",
        type=int,
        default=DEFAULTS.request_concurrency,
    )
    test_parser.add_argument(
        "--metric-logging",
        choices=tuple(MetricLoggingMode),
        default=DEFAULTS.local_metric_logging,
    )
    test_parser.add_argument("--metric-prefix", default=DEFAULTS.metric_prefix)
    invoke_parser = subparsers.add_parser("invoke", help="Submit an AML batch job")
    invoke_parser.add_argument("--input", type=Path, default=ROOT / "data")
    invoke_parser.add_argument("--model", choices=MODEL_KEYS, required=True)
    invoke_parser.add_argument(
        "--packing",
        choices=tuple(PackingMode),
        default=DEFAULTS.packing,
        help=(
            "none sends one input per HTTP request; batch sends packed input arrays"
        ),
    )
    invoke_parser.add_argument(
        "--max-inputs-per-request",
        type=int,
        default=DEFAULTS.max_inputs_per_request,
    )
    invoke_parser.add_argument("--max-tokens-per-request", type=int)
    invoke_parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULTS.max_retries,
    )
    invoke_parser.add_argument(
        "--request-concurrency",
        type=int,
        default=DEFAULTS.request_concurrency,
    )
    invoke_parser.add_argument("--repeat-inputs", type=int, default=1)
    invoke_parser.add_argument(
        "--metric-logging",
        choices=tuple(MetricLoggingMode),
        default=DEFAULTS.metric_logging,
    )
    invoke_parser.add_argument("--metric-prefix", default=DEFAULTS.metric_prefix)
    monitor_parser = subparsers.add_parser("monitor", help="Stream a queued/running job and show child status")
    monitor_parser.add_argument("job_name")
    download_parser = subparsers.add_parser("download", help="Download a completed job output")
    download_parser.add_argument("job_name")
    download_parser.add_argument("--output", type=Path, default=ROOT / "outputs")
    args = parser.parse_args()

    settings = Settings()
    if args.command == "plan":
        print_plan(settings)
    elif args.command == "network":
        configure_network(settings, args.cidr_prefix)
    elif args.command == "permissions":
        setup_permissions(settings)
    elif args.command == "provision":
        print_plan(settings)
        provision(settings)
    elif args.command == "provision-apim":
        print_plan(settings)
        provision_apim_deployment(settings)
    elif args.command == "test":
        test_local(
            settings,
            args.input,
            args.output,
            args.model,
            args.packing,
            args.max_inputs_per_request,
            args.max_tokens_per_request,
            args.max_retries,
            args.request_concurrency,
            args.metric_logging,
            args.metric_prefix,
        )
    elif args.command == "invoke":
        invoke(
            settings,
            args.input,
            args.model,
            args.packing,
            args.max_inputs_per_request,
            args.max_tokens_per_request,
            args.max_retries,
            args.request_concurrency,
            args.repeat_inputs,
            args.metric_logging,
            args.metric_prefix,
        )
    elif args.command == "monitor":
        monitor(settings, args.job_name)
    elif args.command == "download":
        download(settings, args.job_name, args.output)


if __name__ == "__main__":
    main()
