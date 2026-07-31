"""Helper to create an Azure OpenAI / Foundry custom blocklist + content-filter
policy and bind it to a model deployment.

Prereqs:
    uv add azure-mgmt-cognitiveservices azure-mgmt-resourcegraph

Required role on the Cognitive Services / Foundry account:
    "Cognitive Services Contributor" (or "Cognitive Services OpenAI Contributor").
Reader is not enough.

Usage:
    from utils.blocklist_helper import setup_blocklist_policy

    setup_blocklist_policy(
        credential=credential,
        project_endpoint=settings.project_endpoint,
        deployment_name=settings.model_deployment_name,
        blocklist_name="competitor-cloud",
        policy_name="strict-with-competitor-blocklist",
        patterns=[
            ("gcp",          r"\\bgcp\\b",                              True),
            ("google-cloud", r"\\bgoogle\\s+cloud(?:\\s+platform)?\\b",  True),
            ("vertex-ai",    r"\\bvertex\\s+ai\\b",                      True),
            ("bigquery",     r"\\bbigquery\\b",                          True),
        ],
    )
"""

from __future__ import annotations

from typing import Iterable, Tuple
from urllib.parse import urlparse

from azure.core.credentials import TokenCredential
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.cognitiveservices.models import (
    RaiBlocklist,
    RaiBlocklistItem,
    RaiBlocklistItemProperties,
    RaiBlocklistProperties,
    RaiPolicy,
    RaiPolicyContentFilter,
    RaiPolicyProperties,
)
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest

# (item_name, pattern, is_regex)
PatternSpec = Tuple[str, str, bool]


def resolve_account_location(
    credential: TokenCredential,
    project_endpoint: str,
) -> tuple[str, str, str]:
    """Resolve (subscription_id, resource_group, account_name) from a Foundry
    project endpoint, using Azure Resource Graph.

    Endpoint format:
        https://<account>.services.ai.azure.com/api/projects/<project>
    """
    host = urlparse(project_endpoint).hostname or ""
    account = host.split(".", 1)[0]
    if not account:
        raise ValueError(f"Could not parse account name from endpoint: {project_endpoint}")

    rg_client = ResourceGraphClient(credential)
    query = (
        f"Resources "
        f"| where type =~ 'microsoft.cognitiveservices/accounts' "
        f"   and name == '{account}' "
        f"| project subscriptionId, resourceGroup, name, id "
        f"| take 5"
    )
    result = rg_client.resources(QueryRequest(query=query))
    matches = result.data or []
    if not matches:
        raise RuntimeError(
            f"Could not find Cognitive Services account '{account}' in any "
            f"subscription accessible to your credential. Run `az login` "
            f"with the correct tenant or pass sub_id/rg explicitly."
        )
    if len(matches) > 1:
        ids = "\n  - ".join(m["id"] for m in matches)
        print(f"warn > multiple accounts named '{account}' found, using first:\n  - {ids}")

    return matches[0]["subscriptionId"], matches[0]["resourceGroup"], account


def upsert_blocklist(
    cs_mgmt: CognitiveServicesManagementClient,
    rg: str,
    account: str,
    blocklist_name: str,
    description: str,
    patterns: Iterable[PatternSpec],
) -> None:
    """Create / update a custom blocklist and its items."""
    cs_mgmt.rai_blocklists.create_or_update(
        resource_group_name=rg,
        account_name=account,
        rai_blocklist_name=blocklist_name,
        rai_blocklist=RaiBlocklist(
            properties=RaiBlocklistProperties(description=description)
        ),
    )
    print(f"blocklist > {blocklist_name} ready")

    for item_name, pattern, is_regex in patterns:
        cs_mgmt.rai_blocklist_items.create_or_update(
            resource_group_name=rg,
            account_name=account,
            rai_blocklist_name=blocklist_name,
            rai_blocklist_item_name=item_name,
            rai_blocklist_item=RaiBlocklistItem(
                properties=RaiBlocklistItemProperties(pattern=pattern, is_regex=is_regex)
            ),
        )
        print(f"  + {item_name}: {pattern} (regex={is_regex})")


def upsert_policy(
    cs_mgmt: CognitiveServicesManagementClient,
    credential: TokenCredential,
    sub_id: str,
    rg: str,
    account: str,
    policy_name: str,
    blocklist_name: str,
    base_policy: str = "Microsoft.DefaultV2",
    severity: str = "Medium",
) -> None:
    """Create / update a content-filter policy that references the blocklist on
    both Prompt and Completion sources, plus standard severity filters.

    Falls back to a raw REST call if the installed
    `azure-mgmt-cognitiveservices` doesn't yet expose `custom_blocklists` on
    `RaiPolicyProperties`.
    """
    content_filters = [
        RaiPolicyContentFilter(
            name=cat, blocking=True, enabled=True,
            severity_threshold=severity, source=src,
        )
        for src in ("Prompt", "Completion")
        for cat in ("Hate", "Sexual", "Selfharm", "Violence")
    ]
    custom_blocklists = [
        {"blocklistName": blocklist_name, "blocking": True, "source": "Prompt"},
        {"blocklistName": blocklist_name, "blocking": True, "source": "Completion"},
    ]

    try:
        cs_mgmt.rai_policies.create_or_update(
            resource_group_name=rg,
            account_name=account,
            rai_policy_name=policy_name,
            rai_policy=RaiPolicy(properties=RaiPolicyProperties(
                base_policy_name=base_policy,
                mode="Default",
                content_filters=content_filters,
                custom_blocklists=custom_blocklists,
            )),
        )
    except TypeError:
        import requests
        token = credential.get_token("https://management.azure.com/.default").token
        url = (
            f"https://management.azure.com/subscriptions/{sub_id}/resourceGroups/{rg}"
            f"/providers/Microsoft.CognitiveServices/accounts/{account}"
            f"/raiPolicies/{policy_name}?api-version=2024-10-01"
        )
        body = {
            "properties": {
                "basePolicyName": base_policy,
                "mode": "Default",
                "contentFilters": [
                    {"name": cf.name, "blocking": cf.blocking, "enabled": cf.enabled,
                     "severityThreshold": cf.severity_threshold, "source": cf.source}
                    for cf in content_filters
                ],
                "customBlocklists": custom_blocklists,
            }
        }
        r = requests.put(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        r.raise_for_status()
    print(f"policy > {policy_name} ready")


def bind_policy_to_deployment(
    cs_mgmt: CognitiveServicesManagementClient,
    rg: str,
    account: str,
    deployment_name: str,
    policy_name: str,
) -> None:
    """Attach a content-filter policy to a model deployment.

    Note: this updates the deployment definition and may briefly disrupt traffic.
    """
    deployment = cs_mgmt.deployments.get(rg, account, deployment_name)
    deployment.properties.rai_policy_name = policy_name
    cs_mgmt.deployments.begin_create_or_update(rg, account, deployment_name, deployment).result()
    print(f"deployment > {deployment_name} now uses raiPolicyName={policy_name}")


def setup_blocklist_policy(
    *,
    credential: TokenCredential,
    project_endpoint: str,
    deployment_name: str,
    blocklist_name: str,
    policy_name: str,
    patterns: Iterable[PatternSpec],
    description: str = "Custom blocklist managed by blocklist_helper",
    base_policy: str = "Microsoft.DefaultV2",
    severity: str = "Medium",
) -> None:
    """End-to-end: resolve sub/rg/account, create blocklist + policy, bind to deployment."""
    sub_id, rg, account = resolve_account_location(credential, project_endpoint)
    print(f"resolved > sub={sub_id} rg={rg} account={account} deployment={deployment_name}")

    cs_mgmt = CognitiveServicesManagementClient(credential, sub_id)

    upsert_blocklist(cs_mgmt, rg, account, blocklist_name, description, patterns)
    upsert_policy(
        cs_mgmt, credential, sub_id, rg, account,
        policy_name=policy_name,
        blocklist_name=blocklist_name,
        base_policy=base_policy,
        severity=severity,
    )
    bind_policy_to_deployment(cs_mgmt, rg, account, deployment_name, policy_name)
