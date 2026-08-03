"""Azure OpenAI / Foundry custom blocklist + content-filter policy helper.

This module wraps the Cognitive Services management plane (control-plane
REST APIs under ``Microsoft.CognitiveServices/accounts``) so a notebook can:

1. Resolve which subscription / resource group hosts a Foundry account, given
   only the data-plane project endpoint.
2. Create or reuse a **custom blocklist** (``RaiBlocklist``) and its items
   (``RaiBlocklistItem``).
3. Create or reuse a **content-filter policy** (``RaiPolicy``) that references
   that blocklist on Prompt and Completion sources, layered on top of the
   standard severity filters from ``Microsoft.DefaultV2``.
4. Bind the policy to a model deployment by setting
   ``deployment.properties.rai_policy_name``.

Once bound, any prompt or completion that matches a blocklist item triggers a
``content_filter`` error from the Responses / Chat Completions API, surfaced
as ``openai.BadRequestError`` with ``code='content_filter'`` and an
``innererror.content_filter_result.custom_blocklists`` payload.

Prerequisites
-------------
- ``uv add azure-mgmt-cognitiveservices azure-mgmt-resourcegraph``
- Sign in: ``az login --tenant <tenant-id>``
- RBAC on the Cognitive Services / Foundry account:
  ``Cognitive Services Contributor`` (or ``Cognitive Services OpenAI Contributor``).
  Reader is **not** enough.

Privacy
-------
Subscription IDs are masked in console output by default (only the first
four characters are printed, the rest is ``****-****-...``). Disable with
``mask_sub_id=False`` if you need the full ID for debugging.

Idempotence
-----------
``setup_blocklist_policy`` is idempotent and safe to re-run. The default
behavior is **non-destructive**:

- If the blocklist already exists, it is left alone and the function prints
  ``blocklist > <name> already exists (skipping; pass override=True to recreate)``.
  Same for the policy.
- The deployment binding is always re-applied (cheap, and useful if the
  policy was changed externally).

Pass ``override=True`` to upsert the blocklist and policy. This re-uploads
all blocklist items.

Usage
-----
::

    from utils.blocklist_helper import setup_blocklist_policy

    setup_blocklist_policy(
        credential=credential,
        project_endpoint=settings.project_endpoint,
        deployment_name=settings.model_deployment_name,
        blocklist_name="competitor-cloud",
        policy_name="strict-with-competitor-blocklist",
        description="Block competitor cloud mentions",
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
from azure.core.exceptions import ResourceNotFoundError
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

# Tuple shape for declaring blocklist patterns:
#   (item_name, pattern, is_regex)
#
# - item_name: ARM resource name for the blocklist item (alphanumeric + dashes).
# - pattern:   the literal string OR a regex (depends on ``is_regex``).
# - is_regex:  True  -> pattern is a regex (use ``\b`` for word boundaries).
#              False -> pattern is a case-insensitive whole-word literal match.
PatternSpec = Tuple[str, str, bool]


# ---------------------------------------------------------------------------
# Privacy helpers
# ---------------------------------------------------------------------------
def _mask_sub_id(sub_id: str, mask: bool) -> str:
    """Mask a subscription ID for safe console output.

    Returns the original string when ``mask`` is False; otherwise keeps the
    first 4 characters and replaces the rest with a fixed ``****-****-...``
    suffix so the output length doesn't leak the GUID structure either.
    """
    if not mask or not sub_id:
        return sub_id
    return f"{sub_id[:4]}****-****-...."


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def resolve_account_location(
    credential: TokenCredential,
    project_endpoint: str,
) -> tuple[str, str, str]:
    """Resolve ``(subscription_id, resource_group, account_name)`` from a
    Foundry project endpoint via Azure Resource Graph.

    The Foundry project endpoint has the form::

        https://<account>.services.ai.azure.com/api/projects/<project>

    The Cognitive Services account name is the first DNS label and is
    globally unique on that host, so a single ARG row is the norm.

    Raises:
        ValueError: the endpoint URL has no parseable host.
        RuntimeError: no account with that name is visible to the credential
            (wrong tenant, wrong subscription, or insufficient Reader rights
            at the subscription/RG scope for ARG).
    """
    host = urlparse(project_endpoint).hostname or ""
    account = host.split(".", 1)[0]
    if not account:
        raise ValueError(f"Could not parse account name from endpoint: {project_endpoint}")

    rg_client = ResourceGraphClient(credential)
    # Resource Graph KQL: a single account row across every subscription the
    # principal can see. Limit to 5 to surface (and warn about) duplicates.
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
        # ARG can return rows from multiple tenants the principal has access
        # to. We pick the first deterministically and warn so the operator
        # can verify they targeted the right account.
        ids = "\n  - ".join(m["id"] for m in matches)
        print(f"warn > multiple accounts named '{account}' found, using first:\n  - {ids}")

    return matches[0]["subscriptionId"], matches[0]["resourceGroup"], account


# ---------------------------------------------------------------------------
# Existence checks
# ---------------------------------------------------------------------------
def _blocklist_exists(
    cs_mgmt: CognitiveServicesManagementClient,
    rg: str,
    account: str,
    blocklist_name: str,
) -> bool:
    """Return True iff the blocklist already exists on the account."""
    try:
        cs_mgmt.rai_blocklists.get(rg, account, blocklist_name)
        return True
    except ResourceNotFoundError:
        return False


def _policy_exists(
    cs_mgmt: CognitiveServicesManagementClient,
    rg: str,
    account: str,
    policy_name: str,
) -> bool:
    """Return True iff the content-filter policy already exists on the account."""
    try:
        cs_mgmt.rai_policies.get(rg, account, policy_name)
        return True
    except ResourceNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Blocklist + items
# ---------------------------------------------------------------------------
def upsert_blocklist(
    cs_mgmt: CognitiveServicesManagementClient,
    rg: str,
    account: str,
    blocklist_name: str,
    description: str,
    patterns: Iterable[PatternSpec],
    *,
    override: bool = False,
) -> bool:
    """Create / update a custom blocklist and its items.

    Args:
        cs_mgmt: an authenticated ``CognitiveServicesManagementClient``.
        rg: resource group of the Foundry / Cognitive Services account.
        account: Cognitive Services account name.
        blocklist_name: ARM resource name for the blocklist.
        description: free-text description stored on the blocklist.
        patterns: iterable of ``(item_name, pattern, is_regex)`` tuples.
        override: when False (default), if the blocklist already exists this
            function prints a notice and returns False without touching the
            existing blocklist. When True, the blocklist is upserted and all
            items are (re)written.

    Returns:
        True if the blocklist + items were written; False if it already
        existed and was left intact.
    """
    if not override and _blocklist_exists(cs_mgmt, rg, account, blocklist_name):
        print(
            f"blocklist > {blocklist_name} already exists "
            f"(skipping; pass override=True to recreate)"
        )
        return False

    # PUT semantics for raiBlocklists: create_or_update is idempotent on the
    # blocklist resource itself. The same call also resets the description.
    cs_mgmt.rai_blocklists.create_or_update(
        resource_group_name=rg,
        account_name=account,
        rai_blocklist_name=blocklist_name,
        rai_blocklist=RaiBlocklist(
            properties=RaiBlocklistProperties(description=description)
        ),
    )
    print(f"blocklist > {blocklist_name} ready")

    # Each item is a separate PUT under the blocklist. create_or_update is
    # idempotent per item, so re-running with the same patterns is a no-op.
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
    return True


# ---------------------------------------------------------------------------
# Content-filter policy
# ---------------------------------------------------------------------------
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
    *,
    override: bool = False,
    mask_sub_id: bool = True,
) -> bool:
    """Create / update a content-filter policy that references the blocklist.

    The policy inherits standard severity filters from ``base_policy`` (the
    Microsoft default) and adds the custom blocklist on both the Prompt and
    Completion sides so a match in either direction triggers a 400.

    Args:
        cs_mgmt: authenticated management client.
        credential: needed for the REST fallback path; the function never
            logs tokens itself.
        sub_id, rg, account: ARM scope for the Cognitive Services account.
        policy_name: ARM resource name for the policy.
        blocklist_name: name of an existing blocklist to attach to the policy.
        base_policy: name of a built-in severity baseline.
        severity: severity threshold for the inherited filters.
        override: when False (default), if the policy already exists this
            function prints a notice and returns False without touching it.
            When True, the policy is upserted.
        mask_sub_id: when True (default), subscription IDs in any error
            message bubbling out of the REST fallback are masked; the SDK
            exception itself may still contain the full ID.

    Returns:
        True if the policy was written; False if it existed and was left intact.

    Notes:
        Some older ``azure-mgmt-cognitiveservices`` versions don't expose
        ``custom_blocklists`` on ``RaiPolicyProperties``. In that case we
        fall back to a raw REST PUT against the management plane using the
        same credential. The REST URL contains the subscription ID; we only
        construct it locally and never print it directly.
    """
    if not override and _policy_exists(cs_mgmt, rg, account, policy_name):
        print(
            f"policy > {policy_name} already exists "
            f"(skipping; pass override=True to recreate)"
        )
        return False

    # 8 inherited severity filters: 4 categories x 2 sources.
    content_filters = [
        RaiPolicyContentFilter(
            name=cat, blocking=True, enabled=True,
            severity_threshold=severity, source=src,
        )
        for src in ("Prompt", "Completion")
        for cat in ("Hate", "Sexual", "Selfharm", "Violence")
    ]
    # Custom blocklist applied symmetrically to both directions.
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
        # Older SDK: RaiPolicyProperties has no ``custom_blocklists`` field.
        # Use the raw REST endpoint to send the blocklist binding directly.
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
        try:
            r = requests.put(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body, timeout=30,
            )
            r.raise_for_status()
        except requests.HTTPError as e:
            # Re-raise with the URL stripped of the subscription ID so the
            # masked surface stays consistent.
            scrubbed = url.replace(sub_id, _mask_sub_id(sub_id, mask_sub_id))
            raise RuntimeError(f"REST PUT failed: {e}; scope: {scrubbed}") from None

    print(f"policy > {policy_name} ready")
    return True


# ---------------------------------------------------------------------------
# Deployment binding
# ---------------------------------------------------------------------------
def bind_policy_to_deployment(
    cs_mgmt: CognitiveServicesManagementClient,
    rg: str,
    account: str,
    deployment_name: str,
    policy_name: str,
) -> None:
    """Attach a content-filter policy to a model deployment.

    Sets ``deployment.properties.rai_policy_name`` and re-issues the deployment
    PUT. The control plane re-creates the deployment definition; brief
    disruption (a few seconds) is possible if traffic is in flight.

    The binding step always re-applies the policy name regardless of any
    ``override`` flag, because re-applying is cheap and lets you fix a
    deployment whose policy was changed externally without recreating the
    policy itself.
    """
    deployment = cs_mgmt.deployments.get(rg, account, deployment_name)
    deployment.properties.rai_policy_name = policy_name
    cs_mgmt.deployments.begin_create_or_update(rg, account, deployment_name, deployment).result()
    print(f"deployment > {deployment_name} now uses raiPolicyName={policy_name}")


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------
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
    override: bool = False,
    mask_sub_id: bool = True,
) -> None:
    """Resolve account scope, create blocklist + policy, bind to deployment.

    Args:
        credential: an Azure credential with control-plane rights on the
            Cognitive Services account (Contributor).
        project_endpoint: Foundry project endpoint URL (used to derive the
            account name; the subscription / RG are looked up via ARG).
        deployment_name: name of the model deployment to bind to.
        blocklist_name: ARM name of the custom blocklist.
        policy_name: ARM name of the content-filter policy.
        patterns: iterable of ``(item_name, pattern, is_regex)`` tuples.
        description: stored on the blocklist resource.
        base_policy: severity-filter baseline (default ``Microsoft.DefaultV2``).
        severity: severity threshold for the inherited filters.
        override: pass ``True`` to recreate an existing blocklist / policy
            instead of skipping it. Defaults to ``False`` (idempotent, safe).
        mask_sub_id: when ``True`` (default), the subscription ID is masked
            in any output this function emits.
    """
    sub_id, rg, account = resolve_account_location(credential, project_endpoint)
    print(
        f"resolved > sub={_mask_sub_id(sub_id, mask_sub_id)} "
        f"rg={rg} account={account} deployment={deployment_name}"
    )

    cs_mgmt = CognitiveServicesManagementClient(credential, sub_id)

    upsert_blocklist(
        cs_mgmt, rg, account,
        blocklist_name=blocklist_name,
        description=description,
        patterns=patterns,
        override=override,
    )
    upsert_policy(
        cs_mgmt, credential, sub_id, rg, account,
        policy_name=policy_name,
        blocklist_name=blocklist_name,
        base_policy=base_policy,
        severity=severity,
        override=override,
        mask_sub_id=mask_sub_id,
    )
    bind_policy_to_deployment(cs_mgmt, rg, account, deployment_name, policy_name)
