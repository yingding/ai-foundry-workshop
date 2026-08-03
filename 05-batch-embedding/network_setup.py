import ipaddress
import re
from dataclasses import dataclass
from typing import Any

import requests
from azure.ai.ml import MLClient
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import (
    NetworkSecurityPerimeter,
    NspAccessRule,
    NspAssociation,
    NspProfile,
    SubResource,
    SubscriptionId,
)
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import IPRule, NetworkRuleSet, StorageAccountUpdateParameters
from azure.storage.blob import BlobServiceClient


IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


@dataclass(frozen=True)
class NetworkResult:
    storage_account: str
    nsp_name: str
    profile_name: str
    client_cidr: str
    managed_private_endpoints_active: bool


def _client_ipv4() -> str:
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            value = requests.get(url, timeout=5).text.strip()
            if IPV4_RE.match(value):
                return value
        except requests.RequestException:
            continue
    raise RuntimeError(
        "Could not determine a public IPv4 address. Disable IPv6-only/GSA tunneling and retry."
    )


def _mask_ip(value: str) -> str:
    parts = value.split(".")
    return ".".join([*parts[:2], "x", "x"]) if len(parts) == 4 else "<redacted>"


def _same_id(left: str | None, right: str | None) -> bool:
    return (left or "").casefold() == (right or "").casefold()


def _managed_private_endpoints_active(workspace: Any, storage_id: str) -> bool:
    managed_network = getattr(workspace, "managed_network", None)
    rules = getattr(managed_network, "outbound_rules", None) or []
    active_targets: set[str] = set()
    for rule in rules:
        destination = getattr(rule, "destination", None) or rule
        target_id = getattr(destination, "service_resource_id", None)
        subresource = getattr(destination, "subresource_target", None)
        status_value = getattr(rule, "status", "")
        status = str(getattr(status_value, "value", status_value)).casefold()
        if _same_id(target_id, storage_id) and status == "active" and subresource:
            active_targets.add(str(subresource).casefold())
    return {"blob", "file"}.issubset(active_targets)


def _find_storage_association(
    network_client: NetworkManagementClient,
    resource_group: str,
    storage_id: str,
):
    for perimeter in network_client.network_security_perimeters.list(resource_group):
        for association in network_client.network_security_perimeter_associations.list(
            resource_group, perimeter.name
        ):
            target = getattr(getattr(association, "private_link_resource", None), "id", None)
            if _same_id(target, storage_id):
                return perimeter, association
    return None, None


def _profile_name_from_id(profile_id: str | None) -> str | None:
    return profile_id.rstrip("/").rsplit("/", 1)[-1] if profile_id else None


def _ensure_additive_access_rules(
    network_client: NetworkManagementClient,
    resource_group: str,
    nsp_name: str,
    profile_name: str,
    subscription_id: str,
    client_cidr: str,
) -> None:
    before = list(
        network_client.network_security_perimeter_access_rules.list(
            resource_group, nsp_name, profile_name
        )
    )
    before_names = {rule.name for rule in before}

    has_client_rule = any(
        client_cidr in (rule.address_prefixes or [])
        and str(rule.direction or "").casefold() == "inbound"
        for rule in before
    )
    if not has_client_rule:
        network = ipaddress.ip_network(client_cidr, strict=False)
        rule_name = f"batch-client-{str(network.network_address).replace('.', '-')}-{network.prefixlen}"
        if rule_name in before_names:
            raise RuntimeError(f"Existing NSP rule {rule_name} has different contents; refusing to overwrite it")
        network_client.network_security_perimeter_access_rules.create_or_update(
            resource_group,
            nsp_name,
            profile_name,
            rule_name,
            NspAccessRule(direction="Inbound", address_prefixes=[client_cidr]),
        )
        print(f"Added NSP client rule for {_mask_ip(str(network.network_address))}/{network.prefixlen}")
    else:
        print("Preserved existing NSP client rule")

    subscription_scope = f"/subscriptions/{subscription_id}"
    has_subscription_rule = any(
        any(_same_id(item.id, subscription_scope) for item in (rule.subscriptions or []))
        and str(rule.direction or "").casefold() == "inbound"
        for rule in before
    )
    if not has_subscription_rule:
        rule_name = "allow-aml-subscription-batch-embedding"
        if rule_name in before_names:
            raise RuntimeError(f"Existing NSP rule {rule_name} has different contents; refusing to overwrite it")
        network_client.network_security_perimeter_access_rules.create_or_update(
            resource_group,
            nsp_name,
            profile_name,
            rule_name,
            NspAccessRule(
                direction="Inbound",
                subscriptions=[SubscriptionId(id=subscription_scope)],
            ),
        )
        print("Added NSP AML-subscription rule")
    else:
        print("Preserved existing NSP AML-subscription rule")

    after_names = {
        rule.name
        for rule in network_client.network_security_perimeter_access_rules.list(
            resource_group, nsp_name, profile_name
        )
    }
    if not before_names.issubset(after_names):
        raise RuntimeError("An existing NSP rule disappeared; stopping to protect other demos")


def _ensure_storage_firewall_additive(
    storage_client: StorageManagementClient,
    resource_group: str,
    storage_name: str,
    client_cidr: str,
) -> None:
    storage = storage_client.storage_accounts.get_properties(resource_group, storage_name)
    network_rules = storage.network_rule_set or NetworkRuleSet(default_action="Deny")
    existing_ip_rules = list(network_rules.ip_rules or [])
    existing_ip_values = {rule.ip_address_or_range for rule in existing_ip_rules}
    updated_ip_rules = list(existing_ip_rules)
    if client_cidr not in existing_ip_values:
        updated_ip_rules.append(IPRule(ip_address_or_range=client_cidr, action="Allow"))

    bypass_values = {
        item.strip()
        for item in str(getattr(network_rules.bypass, "value", network_rules.bypass) or "").split(",")
        if item.strip() and item.strip().casefold() != "none"
    }
    bypass_values.add("AzureServices")

    updated_rules = NetworkRuleSet(
        bypass=",".join(sorted(bypass_values)),
        default_action="Deny",
        ip_rules=updated_ip_rules,
        virtual_network_rules=list(network_rules.virtual_network_rules or []),
        resource_access_rules=list(network_rules.resource_access_rules or []),
    )
    storage_client.storage_accounts.update(
        resource_group,
        storage_name,
        StorageAccountUpdateParameters(
            allow_shared_key_access=True,
            public_network_access="Enabled",
            network_rule_set=updated_rules,
        ),
    )

    after = storage_client.storage_accounts.get_properties(resource_group, storage_name)
    after_values = {rule.ip_address_or_range for rule in (after.network_rule_set.ip_rules or [])}
    if not existing_ip_values.issubset(after_values):
        raise RuntimeError("An existing storage IP rule disappeared; stopping to protect other demos")
    if client_cidr not in after_values:
        raise RuntimeError("The current client CIDR was not persisted to the storage firewall")
    print(f"Preserved {len(existing_ip_values)} storage IP rules and allowed current /24")


def configure_private_network(
    settings: Any,
    credential: TokenCredential,
    ml_client: MLClient,
    cidr_prefix: int = 24,
) -> NetworkResult:
    workspace = ml_client.workspaces.get(settings.aml_workspace)
    storage_id = workspace.storage_account
    storage_name = storage_id.rstrip("/").rsplit("/", 1)[-1]
    storage_client = StorageManagementClient(credential, settings.subscription_id)
    storage = storage_client.storage_accounts.get_properties(
        settings.aml_resource_group, storage_name
    )
    if not storage.id or not storage.location:
        raise RuntimeError("Workspace storage resource ID or location is missing")

    managed_pe_active = _managed_private_endpoints_active(workspace, storage.id)
    if not managed_pe_active:
        raise RuntimeError("AML managed VNet blob/file private endpoints are not both Active")
    print("Verified AML managed VNet blob/file private endpoints are Active")

    client_ip = _client_ipv4()
    client_cidr = str(ipaddress.ip_network(f"{client_ip}/{cidr_prefix}", strict=False))
    network_client = NetworkManagementClient(credential, settings.subscription_id)
    perimeter, association = _find_storage_association(
        network_client, settings.aml_resource_group, storage.id
    )

    if perimeter and association:
        nsp_name = perimeter.name
        profile_name = _profile_name_from_id(getattr(association.profile, "id", None))
        if not profile_name:
            raise RuntimeError("Existing storage NSP association has no profile")
        print(f"Reusing storage NSP association {nsp_name}/{profile_name}")
    else:
        nsp_name = f"aml-build-clients-{storage.location}"
        profile_name = "clients"
        perimeter = network_client.network_security_perimeters.create_or_update(
            settings.aml_resource_group,
            nsp_name,
            NetworkSecurityPerimeter(
                location=storage.location,
                tags={"app": "aml-batch-embeddings", "source": "python-sdk"},
            ),
        )
        profile = network_client.network_security_perimeter_profiles.create_or_update(
            settings.aml_resource_group, nsp_name, profile_name, NspProfile()
        )
        association = network_client.network_security_perimeter_associations.begin_create_or_update(
            settings.aml_resource_group,
            nsp_name,
            f"{storage_name}-to-{profile_name}",
            NspAssociation(
                private_link_resource=SubResource(id=storage.id),
                profile=SubResource(id=profile.id),
                access_mode="Enforced",
            ),
        ).result()
        print(f"Created NSP association {association.name} in Enforced mode")

    _ensure_additive_access_rules(
        network_client,
        settings.aml_resource_group,
        nsp_name,
        profile_name,
        settings.subscription_id,
        client_cidr,
    )
    _ensure_storage_firewall_additive(
        storage_client,
        settings.aml_resource_group,
        storage_name,
        client_cidr,
    )

    blob_service = BlobServiceClient(
        account_url=f"https://{storage_name}.blob.core.windows.net",
        credential=credential,
    )
    next(iter(blob_service.list_containers(results_per_page=1).by_page()), None)
    print("Storage data-plane smoke test passed")
    return NetworkResult(
        storage_account=storage_name,
        nsp_name=nsp_name,
        profile_name=profile_name,
        client_cidr=client_cidr,
        managed_private_endpoints_active=managed_pe_active,
    )