# Identity and RBAC concept

This project uses Microsoft Entra identities and Azure RBAC for runtime access.
Configuration identifies resources, but it does not contain service credentials.
Account keys and bearer tokens are not stored in `config/.env`.

## Trust model

```text
deployment operator
    | Azure management-plane permissions
    +-> configures AML, APIM, Foundry, Storage, and role assignments

AML compute managed identity
    +-> Foundry account: Cognitive Services OpenAI User
    +-> AML workspace storage: Storage Blob Data Contributor
    +-> AML-facing APIM API: Entra token validated by tenant, audience, and oid

AML workspace managed identity
    +-> AML workspace storage: Storage Blob Data Contributor
    +-> AML workspace storage: Storage File Data Privileged Contributor

APIM system-assigned managed identity
    +-> primary Foundry account: Cognitive Services OpenAI User
    +-> secondary Foundry account: Cognitive Services OpenAI User

APIM client
    +-> APIM subscription or OAuth authorization
    +-> no direct Foundry permission
```

The AML and APIM runtime identities are independent. Granting one identity
access does not grant access to the other.

## Runtime assignments

| Principal | Scope | Role | Purpose |
| --- | --- | --- | --- |
| AML compute managed identity | Foundry account used by the AML deployment | `Cognitive Services OpenAI User` | Call the selected embedding deployment |
| AML compute managed identity | AML workspace storage account | `Storage Blob Data Contributor` | Read batch inputs and write outputs |
| AML compute managed identity | AML-facing APIM API | `validate-azure-ad-token` policy | Call the pooled route without an APIM subscription key |
| AML workspace managed identity | AML workspace storage account | `Storage Blob Data Contributor` | Manage workspace Blob artifacts |
| AML workspace managed identity | AML workspace storage account | `Storage File Data Privileged Contributor` | Manage workspace file-share artifacts |
| APIM system-assigned managed identity | Primary Foundry account | `Cognitive Services OpenAI User` | Call the primary ADA backend |
| APIM system-assigned managed identity | Secondary Foundry account | `Cognitive Services OpenAI User` | Call the secondary ADA backend |

Assignments are made at account or storage-account scope rather than resource
group or subscription scope. This limits each runtime identity to the resources
it must call.

`permissions_setup.py` owns the shared `RbacRole` and `RequiredAssignment`
contracts. Both the AML permission setup and APIM PoC reuse these contracts so
role names and deterministic assignment behavior have one source of truth.

The implementation derives each role-assignment name deterministically from the
scope, principal ID, and role definition ID. Reapplying configuration therefore
converges on the same assignment. `ensure_assignment` uses Azure SDK
`ResourceNotFoundError` and `ResourceExistsError` types rather than matching
error-message text.

## Authentication flows

### AML batch to Foundry

The AML command job runs as the compute managed identity. The embedding client
obtains an Entra token and calls the Foundry data-plane endpoint. The
`Cognitive Services OpenAI User` role authorizes inference; it does not permit
deployment or quota administration.

### AML batch to APIM to Foundry

The parallel `ada-apim` AML deployment obtains a token for
`https://cognitiveservices.azure.com/.default` and sends it to the trusted
AML-facing APIM endpoint. That API does not require an APIM subscription key.
Its `validate-azure-ad-token` policy verifies the tenant, Cognitive Services
audience, and AML compute `oid` claim.

After terminating the caller credential, APIM obtains a new token for its own
system-assigned identity and calls the selected Foundry backend. The AML compute
does not need an APIM key, Foundry key, or Key Vault secret for this route. Its
existing Foundry role remains necessary only for the preserved direct `ada`
deployment.

### AML batch to Storage

AML uses two identities for different operations. Compute reads inputs and
writes job outputs. The workspace identity manages workspace artifacts. Blob
and File data roles are data-plane roles; management-plane Contributor access
alone does not replace them.

### APIM to Foundry

The API policy uses `authentication-managed-identity` with resource
`https://cognitiveservices.azure.com`. APIM requests a token for its own
system-assigned identity and sends it to whichever Foundry backend the pool
selects. The same APIM principal therefore needs `Cognitive Services OpenAI
User` on every account in the pool.

Backend priority and weight affect routing only. They do not combine identities
or transfer role assignments between Foundry accounts.

An optional backend-key design can replace this hop. In that design, each
single APIM backend supplies the `api-key` header for its own Foundry account,
using a separate secret named value or Key Vault reference. The API-level
`authentication-managed-identity` policy is removed, and APIM's Foundry
inference role assignments are no longer used. Client-to-APIM authentication
remains a separate concern and can continue using the APIM subscription key.

Managed identity remains preferred because it avoids account-key storage and
rotation. See [API key authentication options](apim-ada-poc.md#api-key-authentication-options)
for the exact portal procedure and security boundaries.

### Client to APIM

The PoC API uses an active API-scoped APIM subscription. Its generated key is
retrieved in memory by the load runner and is not written to `.env`, logs, or
result files. This is gateway authorization, not Azure RBAC. The client does not
receive Foundry credentials and should not have direct Foundry access for the
gateway test. A future OAuth client policy can replace or complement the APIM
subscription without changing APIM-to-Foundry RBAC.

## Control-plane operator

`DefaultAzureCredential` represents the operator running setup commands. The
operator needs enough management-plane access to:

- read the existing AML, APIM, Foundry, deployment, and Storage resources;
- create or update AML and APIM child resources used by the PoC;
- create role assignments at the target Foundry and Storage scopes.

Creating role assignments generally requires `Owner`, `User Access
Administrator`, or an equivalent custom role. Runtime roles must not be used as
a substitute for operator permissions.

## Configuration contract

`config/.env` is local and ignored. `config/.env.example` contains placeholders
only. These variables select resources and API behavior:

| Variable | Meaning | Security effect |
| --- | --- | --- |
| `AZURE_SUBSCRIPTION_ID` | Subscription containing the managed resources | Management-plane client scope |
| `AML_RESOURCE_GROUP`, `AML_WORKSPACE_NAME`, `AML_COMPUTE_NAME` | AML runtime identity owners | Resolve compute and workspace principals |
| `FOUNDRY_RESOURCE_GROUP`, `FOUNDRY_ACCOUNT_NAME` | Foundry account called by AML | AML OpenAI User assignment scope |
| `AZURE_OPENAI_ADA_DEPLOYMENT` | Matching ADA deployment name | Data-plane route, not a credential |
| `AML_ADA_APIM_DEPLOYMENT_NAME` | Parallel AML deployment using APIM | Preserves direct ADA as an independent fallback |
| `APIM_RESOURCE_GROUP`, `APIM_NAME` | Existing BasicV2 gateway | Resolve the APIM system identity |
| `APIM_ADA_FOUNDRY_RESOURCE_GROUP` | Resource group containing PoC backends | Resolve both Foundry account scopes |
| `APIM_ADA_PRIMARY_ACCOUNT` | First independently allocated backend | APIM OpenAI User assignment scope |
| `APIM_ADA_SECONDARY_ACCOUNT` | Second independently allocated backend | APIM OpenAI User assignment scope |
| `APIM_ADA_API_VERSION` | Backend request API version | Request contract only |
| `APIM_AML_ENDPOINT` | AML-facing APIM API base URL | Trusted endpoint receiving the compute Entra token |

Primary and secondary are routing roles, not fixed Azure regions. The
provisioner discovers each account's actual region and can use a different
supported region pair without changing the configuration contract.

## Setup and verification

Configure and verify AML runtime assignments:

```bash
uv run setup-embedding-permissions
```

Inspect the APIM and Foundry contract without mutation:

```bash
uv run apim-ada-poc plan
```

Apply the APIM child resources and its two Foundry assignments idempotently:

```bash
uv run apim-ada-poc apply
```

Neither command prints access tokens, account keys, nor APIM subscription keys.

The APIM provisioner obtains its management-plane clients through
`DefaultAzureCredential`. The local load runner uses separate data-plane
credentials by target: an Entra token for direct Foundry calls and an API-scoped
APIM subscription key for gateway calls. It does not acquire the unused
credential when a load run selects only one target type.

## Verify RBAC in Azure Portal

Verify the APIM principal from the API Management service under **Security →
Managed identities**. Copy the system-assigned principal/object ID when a
principal filter is needed.

For each Foundry account used by the pool:

1. Open the Foundry/Cognitive Services account resource in Azure Portal.
2. Select **Access control (IAM) → Role assignments**.
3. Filter for the APIM service name or its principal ID.
4. Confirm `Cognitive Services OpenAI User` is assigned at the account scope.

For AML runtime access, repeat the IAM check with the AML compute managed
identity on the Foundry account. On the AML workspace storage account, verify
the Blob and File data roles listed in the runtime-assignment table above for
the compute and workspace identities.

An APIM subscription under **APIs → Subscriptions** is not Azure RBAC. It
authorizes the client at the gateway. The APIM managed identity plus Foundry IAM
assignment authorizes the separate APIM-to-Foundry hop.

For the AML-facing API, open **APIs → APIs → ADA embeddings AML regional pool
PoC → All operations → Inbound processing**. Verify
`validate-azure-ad-token` contains the expected tenant, Cognitive Services
audience, and current AML compute object ID. This API intentionally has
**Subscription required** disabled because the managed identity token is the
client credential.

With the optional backend-key design, the APIM subscription still authorizes
the client-facing hop, while per-backend Foundry keys authorize the backend hop.
Do not use or expose a Foundry key as the APIM client subscription key.

## Boundaries

- RBAC does not allocate TPM or RPM quota.
- APIM does not inherit the AML compute identity.
- APIM subscription keys do not authorize direct Foundry calls.
- Foundry inference roles do not authorize role-assignment creation.
- Network access controls and private endpoints remain separate from RBAC.