import argparse
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
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

ROOT = Path(__file__).resolve().parent
MODEL_KEYS = ("small", "large", "ada")


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.subscription_id = required("AZURE_SUBSCRIPTION_ID")
        self.aml_resource_group = required("AML_RESOURCE_GROUP")
        self.aml_workspace = required("AML_WORKSPACE_NAME")
        self.compute_name = required("AML_COMPUTE_NAME")
        self.endpoint_name = required("AML_ENDPOINT_NAME")
        self.batch_deployments = {
            "small": required("AML_SMALL_DEPLOYMENT_NAME"),
            "large": required("AML_LARGE_DEPLOYMENT_NAME"),
            "ada": os.getenv("AML_ADA_DEPLOYMENT_NAME", "embedding-ada-v1"),
        }
        self.foundry_resource_group = required("FOUNDRY_RESOURCE_GROUP")
        self.foundry_account = required("FOUNDRY_ACCOUNT_NAME")
        self.foundry_project = required("FOUNDRY_PROJECT_NAME")
        self.foundry_project_endpoint = required("FOUNDRY_PROJECT_ENDPOINT")
        self.openai_endpoint = required("AZURE_OPENAI_ENDPOINT")
        self.openai_deployments = {
            "small": required("AZURE_OPENAI_SMALL_DEPLOYMENT"),
            "large": required("AZURE_OPENAI_LARGE_DEPLOYMENT"),
            "ada": os.getenv(
                "AZURE_OPENAI_ADA_DEPLOYMENT", "text-embedding-ada-002-test"
            ),
        }
        self.openai_models = {
            "small": required("AZURE_OPENAI_SMALL_MODEL"),
            "large": required("AZURE_OPENAI_LARGE_MODEL"),
            "ada": os.getenv("AZURE_OPENAI_ADA_MODEL", "text-embedding-ada-002"),
        }
        embedding_3_version = required("AZURE_OPENAI_MODEL_VERSION")
        self.openai_model_versions = {
            "small": embedding_3_version,
            "large": embedding_3_version,
            "ada": os.getenv("AZURE_OPENAI_ADA_MODEL_VERSION", "2"),
        }
        self.openai_sku = required("AZURE_OPENAI_SKU")
        self.openai_capacity = int(required("AZURE_OPENAI_CAPACITY"))

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
        print(
            f"  {model_key}:      {settings.openai_deployments[model_key]} "
            f"({settings.openai_models[model_key]}:{settings.openai_model_versions[model_key]})"
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
                size="Standard_DS3_v2",
                min_instances=0,
                max_instances=2,
                idle_time_before_scale_down=120,
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
):
    environment = Environment(
        image="mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu22.04:latest",
        conda_file=ROOT / "component" / "conda.yaml",
    )
    embed = command(
        name=f"embed_documents_{model_key}_openai_v1",
        display_name=f"Embed documents with {settings.openai_models[model_key]}",
        code=code_path,
        command=(
            "PYTHONPATH=. python component/embed.py --input-dir ${{inputs.documents}} "
            "--output-dir ${{outputs.embeddings}} "
            f"--endpoint {settings.openai_endpoint} "
            f"--deployment {settings.openai_deployments[model_key]} "
            f"--model {settings.openai_models[model_key]} "
            "--packing ${{inputs.packing}} "
            "--max-inputs-per-request ${{inputs.max_inputs_per_request}} "
            "--max-retries ${{inputs.max_retries}} "
            "--request-concurrency ${{inputs.request_concurrency}}"
        ),
        inputs={
            "documents": Input(type=AssetTypes.URI_FOLDER),
            "packing": Input(type="string", default="batch"),
            "max_inputs_per_request": Input(type="integer", default=128),
            "max_retries": Input(type="integer", default=8),
            "request_concurrency": Input(type="integer", default=1),
        },
        outputs={"embeddings": Output(type=AssetTypes.URI_FOLDER)},
        environment=environment,
        is_deterministic=False,
    )

    @dsl.pipeline(name=f"batch_embedding_pipeline_{model_key}")
    def pipeline(
        documents: Input,
        packing: str = "batch",
        max_inputs_per_request: int = 128,
        max_retries: int = 8,
        request_concurrency: int = 1,
    ):
        step = embed(
            documents=documents,
            packing=packing,
            max_inputs_per_request=max_inputs_per_request,
            max_retries=max_retries,
            request_concurrency=request_concurrency,
        )
        return {"embeddings": step.outputs.embeddings}

    return pipeline(
        documents=Input(type=AssetTypes.URI_FOLDER),
        packing="batch",
        max_inputs_per_request=128,
        max_retries=8,
        request_concurrency=1,
    ).component


def ensure_batch_endpoint(settings: Settings, ml_client: MLClient) -> None:
    with tempfile.TemporaryDirectory(prefix="aml-batch-embedding-dual-") as temp_dir:
        stage = Path(temp_dir)
        (stage / "component").mkdir()
        (stage / "utils").mkdir()
        shutil.copy2(ROOT / "component" / "embed.py", stage / "component" / "embed.py")
        shutil.copy2(ROOT / "utils" / "__init__.py", stage / "utils" / "__init__.py")
        shutil.copy2(ROOT / "utils" / "fdyauth.py", stage / "utils" / "fdyauth.py")
        endpoint = ml_client.batch_endpoints.begin_create_or_update(
            BatchEndpoint(
                name=settings.endpoint_name,
                description="Selectable small/large OpenAI embeddings from Foundry new",
            )
        ).result()
        for model_key in MODEL_KEYS:
            component = ml_client.components.create_or_update(
                create_pipeline_component(settings, model_key=model_key, code_path=stage)
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
                f"{settings.openai_deployments[model_key]}"
            )
        print("Ready: minimal component code uploaded (3 files)")
        endpoint.defaults.deployment_name = settings.batch_deployments["small"]
        ml_client.batch_endpoints.begin_create_or_update(endpoint).result()
        print(f"Ready: AML batch endpoint {endpoint.name}")


def provision(settings: Settings) -> None:
    ml_client, cognitive_client, authorization_client = clients(settings)
    for model_key in MODEL_KEYS:
        ensure_model_deployment(settings, cognitive_client, model_key)
    ensure_compute(settings, ml_client)
    configure_permissions(settings, ml_client, authorization_client)
    ensure_batch_endpoint(settings, ml_client)


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
            output_name="embeddings",
            download_path=destination,
        )
        output_dir = destination / "named-outputs" / "embeddings"
        results_path = output_dir / "embeddings.jsonl"
        trace_path = output_dir / "trace.jsonl"
        if not results_path.exists() or not trace_path.exists():
            print("Output summary unavailable; download the named output for details.")
            return

        results = [json.loads(line) for line in results_path.read_text().splitlines()]
        errors = [result for result in results if "error" in result]
        spans = [json.loads(line) for line in trace_path.read_text().splitlines()]
        batch_span = next((span for span in spans if span["name"] == "batch.embed"), None)
        if not batch_span:
            print("Output summary unavailable; batch trace span was not found.")
            return

        attributes = batch_span["attributes"]
        request_spans = [span for span in spans if span["name"] == "embeddings.create"]
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


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    position = (len(values) - 1) * percentile_value / 100
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(values) - 1)
    fraction = position - lower_index
    return values[lower_index] + (values[upper_index] - values[lower_index]) * fraction


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
    max_retries: int,
    request_concurrency: int,
    repeat_inputs: int,
) -> None:
    if repeat_inputs < 1 or repeat_inputs > 100:
        raise ValueError("repeat_inputs must be between 1 and 100")
    ml_client, _, _ = clients(settings)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_label = (
        f"embed-{model_key}-{packing}-x{repeat_inputs}-r{max_retries}-"
        f"c{request_concurrency}-{timestamp}"
    )
    with tempfile.TemporaryDirectory(prefix="embedding-load-test-") as temp_dir:
        upload_path = input_path
        if repeat_inputs > 1:
            upload_path = Path(temp_dir) / "input"
            repeated_count = repeat_jsonl_inputs(
                input_path,
                upload_path,
                repeat_inputs,
                settings.openai_models[model_key],
            )
            print(
                f"Prepared {repeated_count} load-test records from "
                f"{repeat_inputs} repetitions"
            )
        job = ml_client.batch_endpoints.invoke(
            endpoint_name=settings.endpoint_name,
            deployment_name=settings.batch_deployments[model_key],
            experiment_name=run_label,
            inputs={
                "documents": Input(type=AssetTypes.URI_FOLDER, path=str(upload_path.resolve())),
                "packing": Input(type="string", default=packing),
                "max_inputs_per_request": Input(
                    type="integer", default=max_inputs_per_request
                ),
                "max_retries": Input(type="integer", default=max_retries),
                "request_concurrency": Input(
                    type="integer", default=request_concurrency
                ),
            },
        )
        print(f"Run label: {run_label}")
        print(
            f"Submitted job ID: {job.name} "
            f"({model_key} -> {settings.openai_deployments[model_key]}, "
            f"packing={packing}, repeats={repeat_inputs}, max_retries={max_retries}, "
            f"concurrency={request_concurrency})"
        )
        monitor(settings, job.name)


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
                    suffix = f"-repeat-{repetition:02d}"
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
        output_name="embeddings",
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
    max_retries: int,
    request_concurrency: int,
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
        max_retries=max_retries,
        request_concurrency=request_concurrency,
        dry_run=True,
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
    test_parser = subparsers.add_parser("test", help="Test parsing, outputs, and traces locally")
    test_parser.add_argument("--input", type=Path, default=ROOT / "data")
    test_parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "local-test")
    test_parser.add_argument("--model", choices=MODEL_KEYS, default="small")
    test_parser.add_argument("--packing", choices=("none", "batch"), default="batch")
    test_parser.add_argument("--max-inputs-per-request", type=int, default=128)
    test_parser.add_argument("--max-retries", type=int, default=8)
    test_parser.add_argument("--request-concurrency", type=int, default=1)
    invoke_parser = subparsers.add_parser("invoke", help="Submit an AML batch job")
    invoke_parser.add_argument("--input", type=Path, default=ROOT / "data")
    invoke_parser.add_argument("--model", choices=MODEL_KEYS, required=True)
    invoke_parser.add_argument(
        "--packing", choices=("none", "batch"), default="batch"
    )
    invoke_parser.add_argument("--max-inputs-per-request", type=int, default=128)
    invoke_parser.add_argument("--max-retries", type=int, default=8)
    invoke_parser.add_argument("--request-concurrency", type=int, default=1)
    invoke_parser.add_argument("--repeat-inputs", type=int, default=1)
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
    elif args.command == "test":
        test_local(
            settings,
            args.input,
            args.output,
            args.model,
            args.packing,
            args.max_inputs_per_request,
            args.max_retries,
            args.request_concurrency,
        )
    elif args.command == "invoke":
        invoke(
            settings,
            args.input,
            args.model,
            args.packing,
            args.max_inputs_per_request,
            args.max_retries,
            args.request_concurrency,
            args.repeat_inputs,
        )
    elif args.command == "monitor":
        monitor(settings, args.job_name)
    elif args.command == "download":
        download(settings, args.job_name, args.output)


if __name__ == "__main__":
    main()
