import argparse
import os
import re
import shutil
import tempfile
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
        }
        self.foundry_resource_group = required("FOUNDRY_RESOURCE_GROUP")
        self.foundry_account = required("FOUNDRY_ACCOUNT_NAME")
        self.foundry_project = required("FOUNDRY_PROJECT_NAME")
        self.foundry_project_endpoint = required("FOUNDRY_PROJECT_ENDPOINT")
        self.openai_endpoint = required("AZURE_OPENAI_ENDPOINT")
        self.openai_deployments = {
            "small": required("AZURE_OPENAI_SMALL_DEPLOYMENT"),
            "large": required("AZURE_OPENAI_LARGE_DEPLOYMENT"),
        }
        self.openai_models = {
            "small": required("AZURE_OPENAI_SMALL_MODEL"),
            "large": required("AZURE_OPENAI_LARGE_MODEL"),
        }
        self.openai_model_version = required("AZURE_OPENAI_MODEL_VERSION")
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
    for model_key in ("small", "large"):
        print(
            f"  {model_key}:      {settings.openai_deployments[model_key]} "
            f"({settings.openai_models[model_key]}:{settings.openai_model_version})"
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
                version=settings.openai_model_version,
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
            f"--deployment {settings.openai_deployments[model_key]}"
        ),
        inputs={"documents": Input(type=AssetTypes.URI_FOLDER)},
        outputs={"embeddings": Output(type=AssetTypes.URI_FOLDER)},
        environment=environment,
    )

    @dsl.pipeline(name=f"batch_embedding_pipeline_{model_key}")
    def pipeline(documents: Input):
        step = embed(documents=documents)
        return {"embeddings": step.outputs.embeddings}

    return pipeline(documents=Input(type=AssetTypes.URI_FOLDER)).component


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
        for model_key in ("small", "large"):
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
    for model_key in ("small", "large"):
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


def invoke(settings: Settings, input_path: Path, model_key: str) -> None:
    ml_client, _, _ = clients(settings)
    job = ml_client.batch_endpoints.invoke(
        endpoint_name=settings.endpoint_name,
        deployment_name=settings.batch_deployments[model_key],
        inputs={
            "documents": Input(type=AssetTypes.URI_FOLDER, path=str(input_path.resolve()))
        },
    )
    print(
        f"Submitted job: {job.name} "
        f"({model_key} -> {settings.openai_deployments[model_key]})"
    )
    monitor(settings, job.name)


def download(settings: Settings, job_name: str, output_path: Path) -> None:
    ml_client, _, _ = clients(settings)
    ml_client.jobs.download(
        name=job_name,
        output_name="embeddings",
        download_path=output_path,
    )
    print(f"Downloaded output to {output_path}")


def test_local(settings: Settings, input_path: Path, output_path: Path, model_key: str) -> None:
    from component.embed import run

    run(
        input_dir=input_path,
        output_dir=output_path,
        endpoint=settings.openai_endpoint,
        deployment=settings.openai_deployments[model_key],
        batch_size=2,
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
    test_parser.add_argument("--model", choices=("small", "large"), default="small")
    invoke_parser = subparsers.add_parser("invoke", help="Submit an AML batch job")
    invoke_parser.add_argument("--input", type=Path, default=ROOT / "data")
    invoke_parser.add_argument("--model", choices=("small", "large"), required=True)
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
        test_local(settings, args.input, args.output, args.model)
    elif args.command == "invoke":
        invoke(settings, args.input, args.model)
    elif args.command == "monitor":
        monitor(settings, args.job_name)
    elif args.command == "download":
        download(settings, args.job_name, args.output)


if __name__ == "__main__":
    main()
