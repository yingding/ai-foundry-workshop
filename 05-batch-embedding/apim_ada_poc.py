import argparse
import os
from dataclasses import dataclass

from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from dotenv import load_dotenv

from permissions_setup import RbacRole, RequiredAssignment, ensure_assignment
from utils.embedding_optimization import (
    capacity_units_to_tpm,
    target_tokens_per_request,
    utilization_target_tpm,
)


ROOT = os.path.dirname(__file__)


@dataclass(frozen=True)
class EnvironmentKeys:
    subscription_id: str = "AZURE_SUBSCRIPTION_ID"
    apim_resource_group: str = "APIM_RESOURCE_GROUP"
    apim_name: str = "APIM_NAME"
    foundry_resource_group: str = "APIM_ADA_FOUNDRY_RESOURCE_GROUP"
    primary_account: str = "APIM_ADA_PRIMARY_ACCOUNT"
    secondary_account: str = "APIM_ADA_SECONDARY_ACCOUNT"
    deployment_name: str = "AZURE_OPENAI_ADA_DEPLOYMENT"
    api_version: str = "APIM_ADA_API_VERSION"
    aml_resource_group: str = "AML_RESOURCE_GROUP"
    aml_workspace: str = "AML_WORKSPACE_NAME"
    aml_compute: str = "AML_COMPUTE_NAME"
    primary_tpm: str = "APIM_ADA_PRIMARY_TPM"
    secondary_tpm: str = "APIM_ADA_SECONDARY_TPM"
    target_utilization: str = "APIM_ADA_TARGET_UTILIZATION"
    requests_per_minute: str = "APIM_ADA_REQUESTS_PER_MINUTE"


@dataclass(frozen=True)
class ApimPocContract:
    api_id: str = "ada-embeddings-poc"
    api_path: str = "ada-embeddings-test"
    operation_id: str = "create-embeddings"
    aml_api_id: str = "ada-embeddings-aml-poc"
    aml_api_path: str = "ada-embeddings-aml"
    primary_backend_id: str = "ada-primary-poc"
    secondary_backend_id: str = "ada-secondary-poc"
    pool_backend_id: str = "ada-regional-pool-poc"
    subscription_id: str = "ada-embeddings-poc-client"
    subscription_display_name: str = "ADA embeddings PoC client"
    required_apim_sku: str = "BasicV2"
    expected_model: str = "text-embedding-ada-002"
    expected_model_version: str = "2"
    backend_priority: int = 1
    default_target_utilization: float = 0.6
    default_requests_per_minute: float = 15.0
    breaker_name: str = "ada-rate-limit"
    breaker_failure_count: int = 3
    breaker_failure_interval: str = "PT30S"
    breaker_trip_duration: str = "PT1M"
    throttled_status_code: int = 429
    managed_identity_resource: str = "https://cognitiveservices.azure.com"
    aml_client_token_audience: str = "https://cognitiveservices.azure.com"
    backend_id_header: str = "X-Poc-Backend-Id"
    backend_type_header: str = "X-Poc-Backend-Type"
    backend_region_header: str = "X-Poc-Backend-Region"
    default_api_version: str = "2024-02-01"


ENV = EnvironmentKeys()
POC = ApimPocContract()


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(f"Missing required environment key: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    subscription_id: str
    apim_resource_group: str
    apim_name: str
    foundry_resource_group: str
    primary_account_name: str
    secondary_account_name: str
    deployment_name: str
    api_version: str
    aml_resource_group: str
    aml_workspace: str
    aml_compute: str
    primary_tpm_override: int | None
    secondary_tpm_override: int | None
    target_utilization: float
    requests_per_minute: float

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            subscription_id=required_environment(ENV.subscription_id),
            apim_resource_group=required_environment(ENV.apim_resource_group),
            apim_name=required_environment(ENV.apim_name),
            foundry_resource_group=required_environment(ENV.foundry_resource_group),
            primary_account_name=required_environment(ENV.primary_account),
            secondary_account_name=required_environment(ENV.secondary_account),
            deployment_name=required_environment(ENV.deployment_name),
            api_version=os.getenv(ENV.api_version, POC.default_api_version),
            aml_resource_group=required_environment(ENV.aml_resource_group),
            aml_workspace=required_environment(ENV.aml_workspace),
            aml_compute=required_environment(ENV.aml_compute),
            primary_tpm_override=optional_int_environment(ENV.primary_tpm),
            secondary_tpm_override=optional_int_environment(ENV.secondary_tpm),
            target_utilization=float(
                os.getenv(
                    ENV.target_utilization,
                    str(POC.default_target_utilization),
                )
            ),
            requests_per_minute=float(
                os.getenv(
                    ENV.requests_per_minute,
                    str(POC.default_requests_per_minute),
                )
            ),
        )


def optional_int_environment(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None


@dataclass(frozen=True)
class CapacityPlan:
    primary_tpm: int
    secondary_tpm: int
    aggregate_tpm: int
    target_tpm: int
    primary_weight: int
    secondary_weight: int
    requests_per_minute: float
    target_tokens_per_request: int


def build_capacity_plan(
    primary_tpm: int,
    secondary_tpm: int,
    utilization: float,
    requests_per_minute: float,
) -> CapacityPlan:
    if primary_tpm <= 0 or secondary_tpm <= 0:
        raise ValueError("Backend TPM values must be positive")
    aggregate_tpm = primary_tpm + secondary_tpm
    target_tpm = utilization_target_tpm(aggregate_tpm, utilization)
    primary_weight = round(primary_tpm / aggregate_tpm * 100)
    secondary_weight = 100 - primary_weight
    return CapacityPlan(
        primary_tpm=primary_tpm,
        secondary_tpm=secondary_tpm,
        aggregate_tpm=aggregate_tpm,
        target_tpm=target_tpm,
        primary_weight=primary_weight,
        secondary_weight=secondary_weight,
        requests_per_minute=requests_per_minute,
        target_tokens_per_request=target_tokens_per_request(
            target_tpm,
            requests_per_minute,
        ),
    )


@dataclass(frozen=True)
class Context:
    settings: Settings
    apim_client: ApiManagementClient
    authorization_client: AuthorizationManagementClient
    apim_principal_id: str
    gateway_url: str
    primary_account_id: str
    secondary_account_id: str
    primary_region: str
    secondary_region: str
    tenant_id: str
    aml_compute_principal_id: str
    capacity: CapacityPlan


def build_context(settings: Settings) -> Context:
    credential = DefaultAzureCredential()
    apim_client = ApiManagementClient(credential, settings.subscription_id)
    authorization_client = AuthorizationManagementClient(
        credential,
        settings.subscription_id,
    )
    cognitive_client = CognitiveServicesManagementClient(
        credential,
        settings.subscription_id,
    )
    ml_client = MLClient(
        credential,
        settings.subscription_id,
        settings.aml_resource_group,
        settings.aml_workspace,
    )

    service = apim_client.api_management_service.get(
        settings.apim_resource_group,
        settings.apim_name,
    )
    if not service.sku or service.sku.name.casefold() != POC.required_apim_sku.casefold():
        actual_sku = service.sku.name if service.sku else "none"
        raise RuntimeError(
            f"Expected existing APIM {settings.apim_name} to use "
            f"{POC.required_apim_sku}, got {actual_sku}"
        )
    if not service.identity or not service.identity.principal_id:
        raise RuntimeError(
            f"Existing APIM {settings.apim_name} has no system-assigned identity"
        )
    if not service.identity.tenant_id:
        raise RuntimeError(f"Existing APIM {settings.apim_name} has no tenant ID")
    if service.properties.provisioning_state != "Succeeded":
        raise RuntimeError(
            f"Existing APIM {settings.apim_name} is not ready: "
            f"{service.properties.provisioning_state}"
        )

    primary_account = cognitive_client.accounts.get(
        settings.foundry_resource_group,
        settings.primary_account_name,
    )
    secondary_account = cognitive_client.accounts.get(
        settings.foundry_resource_group,
        settings.secondary_account_name,
    )
    deployments = []
    for account in (primary_account, secondary_account):
        deployment = cognitive_client.deployments.get(
            settings.foundry_resource_group,
            account.name,
            settings.deployment_name,
        )
        model = deployment.properties.model
        if (
            not model
            or model.name != POC.expected_model
            or model.version != POC.expected_model_version
        ):
            raise RuntimeError(
                f"Deployment {account.name}/{settings.deployment_name} is not ADA version 2"
            )
        if not deployment.sku or deployment.sku.capacity is None:
            raise RuntimeError(
                f"Deployment {account.name}/{settings.deployment_name} has no capacity"
            )
        deployments.append(deployment)

    primary_tpm = settings.primary_tpm_override or capacity_units_to_tpm(
        deployments[0].sku.capacity
    )
    secondary_tpm = settings.secondary_tpm_override or capacity_units_to_tpm(
        deployments[1].sku.capacity
    )
    capacity = build_capacity_plan(
        primary_tpm,
        secondary_tpm,
        settings.target_utilization,
        settings.requests_per_minute,
    )

    compute = ml_client.compute.get(settings.aml_compute)
    if not compute.identity or not compute.identity.principal_id:
        raise RuntimeError(
            f"AML compute {settings.aml_compute} has no system-assigned identity"
        )

    return Context(
        settings=settings,
        apim_client=apim_client,
        authorization_client=authorization_client,
        apim_principal_id=service.identity.principal_id,
        gateway_url=service.properties.gateway_url,
        primary_account_id=primary_account.id,
        secondary_account_id=secondary_account.id,
        primary_region=primary_account.location,
        secondary_region=secondary_account.location,
        tenant_id=service.identity.tenant_id,
        aml_compute_principal_id=compute.identity.principal_id,
        capacity=capacity,
    )


def backend_resource_id(context: Context, backend_id: str) -> str:
    settings = context.settings
    return (
        f"/subscriptions/{settings.subscription_id}"
        f"/resourceGroups/{settings.apim_resource_group}"
        "/providers/Microsoft.ApiManagement/service/"
        f"{settings.apim_name}/backends/{backend_id}"
    )


def single_backend(account_name: str, region: str) -> dict:
    return {
        "properties": {
            "description": f"ADA v2 {region} proof-of-concept backend",
            "url": f"https://{account_name}.openai.azure.com/openai",
            "protocol": "http",
            "type": "Single",
            "azureRegion": region,
            "circuitBreaker": {
                "rules": [
                    {
                        "name": POC.breaker_name,
                        "failureCondition": {
                            "count": POC.breaker_failure_count,
                            "interval": POC.breaker_failure_interval,
                            "statusCodeRanges": [
                                {
                                    "min": POC.throttled_status_code,
                                    "max": POC.throttled_status_code,
                                }
                            ],
                        },
                        "tripDuration": POC.breaker_trip_duration,
                        "acceptRetryAfter": True,
                    }
                ]
            },
        }
    }


def pool_backend(context: Context) -> dict:
    return {
        "properties": {
            "description": "Capacity-weighted ADA v2 regional proof-of-concept pool",
            "type": "Pool",
            "pool": {
                "services": [
                    {
                        "id": backend_resource_id(context, POC.primary_backend_id),
                        "priority": POC.backend_priority,
                        "weight": context.capacity.primary_weight,
                    },
                    {
                        "id": backend_resource_id(context, POC.secondary_backend_id),
                        "priority": POC.backend_priority,
                        "weight": context.capacity.secondary_weight,
                    },
                ]
            },
        }
    }


def api_policy(context: Context, validate_aml_compute: bool = False) -> str:
        client_validation = ""
        if validate_aml_compute:
                client_validation = f"""
        <validate-azure-ad-token tenant-id=\"{context.tenant_id}\">
            <audiences>
                <audience>{POC.aml_client_token_audience}</audience>
            </audiences>
            <required-claims>
                <claim name=\"oid\" match=\"all\">
                    <value>{context.aml_compute_principal_id}</value>
                </claim>
            </required-claims>
        </validate-azure-ad-token>"""
        return f"""<policies>
    <inbound>
        <base />{client_validation}
        <authentication-managed-identity resource=\"{POC.managed_identity_resource}\" />
        <set-backend-service backend-id=\"{POC.pool_backend_id}\" />
        <set-query-parameter name=\"api-version\" exists-action=\"override\">
            <value>{context.settings.api_version}</value>
        </set-query-parameter>
    </inbound>
    <backend>
        <forward-request buffer-request-body=\"true\" />
    </backend>
    <outbound>
        <base />
        <set-header name=\"{POC.backend_id_header}\" exists-action=\"override\">
            <value>@(context.Backend?.Id ?? \"n/a\")</value>
        </set-header>
        <set-header name=\"{POC.backend_type_header}\" exists-action=\"override\">
            <value>@(context.Backend?.Type.ToString() ?? \"n/a\")</value>
        </set-header>
        <set-header name=\"{POC.backend_region_header}\" exists-action=\"override\">
            <value>@(context.Backend?.AzureRegion ?? \"n/a\")</value>
        </set-header>
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>"""


def print_plan(context: Context) -> None:
    settings = context.settings
    print(f"Existing APIM: {settings.apim_name} ({POC.required_apim_sku})")
    print(f"Gateway:       {context.gateway_url}")
    print(f"Identity:      {context.apim_principal_id}")
    print(
        f"Primary:       {settings.primary_account_name} "
        f"({context.primary_region}, {context.capacity.primary_tpm:,} TPM, "
        f"weight {context.capacity.primary_weight})"
    )
    print(
        f"Secondary:     {settings.secondary_account_name} "
        f"({context.secondary_region}, {context.capacity.secondary_tpm:,} TPM, "
        f"weight {context.capacity.secondary_weight})"
    )
    print(
        f"Pool:          {POC.pool_backend_id} "
        f"({context.capacity.aggregate_tpm:,} assigned TPM)"
    )
    print(
        f"Optimization:  {context.capacity.target_tpm:,} target TPM, "
        f"{context.capacity.requests_per_minute:g} requests/min, "
        f"{context.capacity.target_tokens_per_request:,} tokens/request"
    )
    print(f"API:           {context.gateway_url}/{POC.api_path}")
    print(f"AML API:       {context.gateway_url}/{POC.aml_api_path}")
    print(f"AML principal: {context.aml_compute_principal_id}")
    print("No Azure resources were changed.")


def apply(context: Context) -> None:
    settings = context.settings
    for account_name, account_id in (
        (settings.primary_account_name, context.primary_account_id),
        (settings.secondary_account_name, context.secondary_account_id),
    ):
        ensure_assignment(
            context.authorization_client,
            RequiredAssignment(
                principal_name=f"apim:{settings.apim_name}",
                principal_id=context.apim_principal_id,
                role_name=RbacRole.COGNITIVE_SERVICES_OPENAI_USER,
                scope=account_id,
            ),
        )
        print(f"  verified backend access: {account_name}")

    context.apim_client.backend.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.primary_backend_id,
        single_backend(settings.primary_account_name, context.primary_region),
    )
    context.apim_client.backend.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.secondary_backend_id,
        single_backend(settings.secondary_account_name, context.secondary_region),
    )
    context.apim_client.backend.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.pool_backend_id,
        pool_backend(context),
    )

    context.apim_client.api.begin_create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.api_id,
        {
            "properties": {
                "displayName": "ADA embeddings regional pool PoC",
                "description": "Isolated API for validating aggregate ADA capacity.",
                "path": POC.api_path,
                "protocols": ["https"],
                "subscriptionRequired": True,
                "type": "http",
            }
        },
    ).result()
    context.apim_client.api_operation.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.api_id,
        POC.operation_id,
        {
            "properties": {
                "displayName": "Create embeddings",
                "method": "POST",
                "urlTemplate": f"/deployments/{settings.deployment_name}/embeddings",
                "templateParameters": [],
                "responses": [],
            }
        },
    )
    context.apim_client.api_policy.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.api_id,
        "policy",
        {"properties": {"format": "rawxml", "value": api_policy(context)}},
    )

    context.apim_client.api.begin_create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.aml_api_id,
        {
            "properties": {
                "displayName": "ADA embeddings AML regional pool PoC",
                "description": "AML managed-identity API for pooled ADA capacity.",
                "path": POC.aml_api_path,
                "protocols": ["https"],
                "subscriptionRequired": False,
                "type": "http",
            }
        },
    ).result()
    context.apim_client.api_operation.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.aml_api_id,
        POC.operation_id,
        {
            "properties": {
                "displayName": "Create embeddings",
                "method": "POST",
                "urlTemplate": f"/deployments/{settings.deployment_name}/embeddings",
                "templateParameters": [],
                "responses": [],
            }
        },
    )
    context.apim_client.api_policy.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.aml_api_id,
        "policy",
        {
            "properties": {
                "format": "rawxml",
                "value": api_policy(context, validate_aml_compute=True),
            }
        },
    )
    context.apim_client.subscription.create_or_update(
        settings.apim_resource_group,
        settings.apim_name,
        POC.subscription_id,
        {
            "properties": {
                "displayName": POC.subscription_display_name,
                "scope": f"/apis/{POC.api_id}",
                "state": "active",
                "allowTracing": False,
            }
        },
    )
    print(f"Configured {context.gateway_url}/{POC.api_path}")


def main() -> None:
    load_dotenv(os.path.join(ROOT, "config", ".env"))
    parser = argparse.ArgumentParser(
        description=(
            "Configure the multi-region ADA proof of concept in an existing "
            f"{POC.required_apim_sku} APIM."
        )
    )
    parser.add_argument("action", choices=("plan", "apply"))
    args = parser.parse_args()
    context = build_context(Settings.from_environment())
    if args.action == "plan":
        print_plan(context)
    else:
        apply(context)


if __name__ == "__main__":
    main()