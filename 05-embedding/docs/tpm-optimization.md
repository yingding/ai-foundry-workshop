# TPM optimization plan

This plan increases embedding throughput after request batching has reduced RPM
pressure. It distinguishes deployment allocation, regional model quota, and
capacity aggregated through multiple independently allocated backends.

See [RPM optimization plan](rpm-optimization.md) for batching, token-aware array
sizing, pacing, and utilization measurements.

## Current baseline

The `text-embedding-ada-002-test` deployment currently has:

- 15,000 assigned TPM;
- ARM SKU capacity 15;
- `x-ratelimit-limit-tokens: 15000` confirmed by live responses;
- 3,000,000 Global Standard ADA TPM approved in East US 2;
- 422,000 Global Standard ADA TPM assigned across East US 2 deployments at
        inventory time.

The regional quota pool is not automatically available to one deployment. A
deployment can use only the TPM assigned to it.

Microsoft defines quota per subscription, region, model, and deployment type.
All deployments in that scope consume allocations from the same quota pool.

Source: [Manage Azure OpenAI quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#introduction-to-quota)

## 1. Increase the deployment TPM allocation

First, increase the ADA deployment from 15,000 TPM using unused quota already
available in the same regional model pool. Foundry allows TPM to be moved from
underused deployments to high-traffic deployments.

The total assigned across deployments cannot exceed the approved regional pool:

$$
\sum_{d=1}^{n}\mathrm{TPM}_d\leq\mathrm{regional\ model\ quota}
$$

Increasing deployment TPM also increases its paired RPM according to the
model-specific capacity ratio. TPM and RPM cannot be allocated independently.

Before changing the allocation:

1. Confirm that the 3,000,000 limit applies to the same subscription, region,
   model, and deployment type as the ADA deployment.
2. List affiliated deployments using that quota pool.
3. Identify quota assigned to deployments that can be reduced safely.
4. Select a new ADA TPM target based on measured demand, not the maximum
   available value.
5. Apply the allocation and allow up to 15 minutes for propagation.
6. Confirm the new value through deployment metadata and
   `x-ratelimit-limit-tokens` when returned.

Microsoft documents that deployment TPM can be edited from the deployment or
model quota page and recommends rebalancing quota based on observed usage.

Sources:

- [Assign quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#assign-quota)
- [View and request quotas](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#view-and-request-quotas-in-foundry-portal)
- [Understanding rate limits](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits)

## 2. Request more regional model quota

Request a quota increase when the regional model pool cannot satisfy measured
production demand after existing quota has been rebalanced.

The request should specify:

- subscription;
- model and version;
- deployment type;
- region;
- current approved and assigned TPM;
- requested TPM;
- measured utilization, throttling, and projected demand;
- batching and pacing controls already in use.

Microsoft states that requests are prioritized for customers actively using
their existing allocation. Evidence should therefore include sustained TPM
utilization, logical inputs per minute, HTTP 429 rate, and rejected demand.

Submit through Foundry's **Request quota** action or the official quota request
form. Approval is capacity-dependent and isn't guaranteed.

Sources:

- [Request more quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#request-more-quota)
- [Azure OpenAI quota request](https://aka.ms/oai/stuquotarequest)

## 3. Add independently allocated regional deployments

If the current region cannot provide enough additional quota, deploy the same
model and version in another supported region where the subscription has
separate approved quota.

Creating another deployment in the same quota scope does not increase total
TPM. It only divides the existing pool:

$$
\mathrm{same\ pool:}\quad
\mathrm{TPM}_{total}=\mathrm{TPM}_1+\mathrm{TPM}_2\leq Q_{region}
$$

For example, two 100,000-TPM deployments backed by one 200,000-TPM regional pool
still provide only 200,000 TPM in total.

Multiple regions can add throughput when each region has an independent
allocation:

$$
\mathrm{independent\ pools:}\quad
\mathrm{TPM}_{aggregate}=\sum_{r=1}^{m}Q_r
$$

Two regions with independently assigned 200,000 TPM can therefore provide up to
approximately 400,000 aggregate TPM before accounting for safety margins,
failures, and traffic imbalance.

Use the same model and version across backends when embedding vectors must share
one vector index. Different embedding models or versions can produce vectors
that aren't interchangeable.

Microsoft recommends spreading requests across multiple deployments or regions
when one deployment cannot provide the required throughput.

Source: [Understanding rate limits](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits)

## 4. Route independent capacity through APIM

Use Azure API Management only after the backend deployments have independent
usable TPM. APIM doesn't create model quota. It exposes one application endpoint
and distributes requests across capacity already assigned to its backends.

Recommended APIM capabilities:

- backend pool containing the regional embedding endpoints;
- weighted load balancing based on each backend's assigned TPM;
- circuit breakers that react to HTTP 429 and `Retry-After`;
- health-based failover;
- managed identity authentication;
- token-limit policies for fair use across API consumers;
- per-backend telemetry for tokens, requests, latency, and throttling.

For backend TPM values $T_1, T_2, \ldots, T_n$, initial routing weights should be
proportional to capacity:

$$
w_i=\frac{T_i}{\sum_{j=1}^{n}T_j}
$$

A 100,000-TPM backend and a 200,000-TPM backend should begin near weights
$1/3$ and $2/3$, then be validated under representative traffic.

APIM backend pools support round-robin, weighted, and priority-based load
balancing. Circuit breakers can temporarily stop traffic to a throttled backend.
Because APIM load balancing is distributed and approximate, every backend still
needs its own retry, pacing, and monitoring controls.

Sources:

- [Use a gateway with multiple model deployments](https://learn.microsoft.com/azure/architecture/ai-ml/guide/azure-openai-gateway-multi-backend#gateway-implementations)
- [APIM load-balanced backend pools](https://learn.microsoft.com/azure/api-management/backends#load-balanced-pool)
- [APIM backend circuit breakers](https://learn.microsoft.com/azure/api-management/backends#circuit-breaker)
- [AI gateway scalability and token limits](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities#scalability-and-performance)

## Verified APIM proof-of-concept configuration

The subscription contains two matching ADA deployments backed by independent
regional Global Standard quota pools. Both use the same deployment name, model,
version, and assigned capacity.

| Backend | Account | Region | Deployment | Model version | SKU | Assigned TPM |
| --- | --- | --- | --- | ---: | --- | ---: |
| East | `foundry-proj-yw-uno-resource` | East US 2 | `text-embedding-ada-002-test` | 2 | Global Standard | 15,000 |
| West | `foundry-agent-fdl-resource` | West US | `text-embedding-ada-002-test` | 2 | Global Standard | 15,000 |

Both deployments were in `Succeeded` state during inventory. Both accounts
allow public network access, and both expose Azure OpenAI endpoints:

- `https://foundry-proj-yw-uno-resource.openai.azure.com/`
- `https://foundry-agent-fdl-resource.openai.azure.com/`

Because the deployments are in different regions, they draw from independent
regional quota pools. The proof of concept can therefore expose 30,000 assigned
TPM through one gateway:

$$
\mathrm{assigned\ aggregate\ TPM}=15{,}000+15{,}000=30{,}000
$$

This is assigned backend capacity, not a guarantee of exactly 30,000 accepted
tokens in every observed minute. Azure token estimation, APIM distribution,
latency, retries, and safety margins affect usable throughput.

### APIM instance

Use the existing APIM instance with the strongest AI-gateway prerequisites:

| Setting | Value |
| --- | --- |
| APIM | `svc-apim-mcp-yw-dos` |
| Resource group | `rg-yingdingwang-6548` |
| Region | East US |
| SKU | BasicV2, capacity 1 |
| Gateway | `https://svc-apim-mcp-yw-dos.azure-api.net` |
| Managed identity | System-assigned, enabled |
| Public network access | Enabled |
| Existing AI pattern | Backend routing plus `llm-token-limit` policy |

The APIM managed identity currently has no role assignment on either ADA
account. Before the proof of concept, grant it `Cognitive Services OpenAI User`
at each account scope. Do not use account keys when managed identity is
available.

### Gateway shape

Create an isolated API so the test doesn't alter the existing Foundry workflow
API:

| Setting | Planned value |
| --- | --- |
| API path | `/ada-embeddings-test` |
| Backend deployment path | `/openai/deployments/text-embedding-ada-002-test/embeddings` |
| API version | Use the same supported API version for both backends |
| Authentication | APIM managed identity to Cognitive Services |
| Backend pool | East and West ADA endpoints |
| Initial strategy | Weighted load balancing, 50:50 |
| Failure handling | Circuit break on 429 and honor `Retry-After` |
| Client contract | Existing OpenAI embeddings request and response shape |

Equal weights are appropriate because each backend has 15,000 assigned TPM:

$$
w_{East}=w_{West}=\frac{15{,}000}{30{,}000}=0.5
$$

Keep the deployment segment in the request path unchanged. Both backends use
`text-embedding-ada-002-test`, so APIM doesn't need deployment-name rewriting.

### Proof-of-concept sequence

1. Record direct baseline results for each backend independently with identical
        batch-mode inputs.
2. Confirm both responses contain 1,536-dimensional vectors and preserve every
        input index and ID.
3. Compare vectors for identical text. They should be compatible because both
        deployments use ADA version 2; record exact equality or numeric tolerance
        rather than assuming it.
4. Grant the APIM identity access to both accounts.
5. Add the two APIM backends, backend pool, isolated embeddings API, managed
        identity policy, and 429 circuit breakers.
6. Run a low-rate smoke test through APIM and confirm successful responses from
        both backends using gateway diagnostics or backend telemetry.
7. Run the same batch workload directly and through APIM. Compare logical
        inputs, prompt tokens, latency, HTTP status, and input-ID correlation.
8. Increase load over clean minute windows until the direct 15,000-TPM backend
        and the APIM pool show distinct boundaries.
9. Accept the configuration only if APIM approaches the expected two-backend
        gain without corrupting vectors, losing IDs, or materially increasing 429s.

### Acceptance criteria

| Criterion | Required outcome |
| --- | --- |
| Backend compatibility | Same model, version 2, dimensions, and request contract |
| Routing | Both backends receive traffic near 50:50 under a long enough run |
| Correlation | Every logical input returns exactly one matching `input_id` |
| Aggregate utilization | Sustained token throughput exceeds one 15,000-TPM backend |
| Maximum target | Approach 24,000-27,000 TPM, or 80-90% of 30,000 assigned TPM |
| Throttling | HTTP 429 below 1% at the selected steady-state rate |
| Resiliency | A throttled backend is removed temporarily and recovers after its retry interval |
| Latency | Report p50/p95/p99 separately for East, West, and gateway traffic |

West US can have higher network latency from the East US APIM instance. Keep
50:50 as the capacity-derived starting point, then adjust weights only from
measured throughput and latency. Do not interpret a lower-latency backend as
having more TPM.

## Decision sequence

```text
Measured TPM demand
        |
        v
Increase TPM assigned to current deployment
        |
        | insufficient regional pool
        v
Request additional model quota in current region
        |
        | unavailable or insufficient
        v
Allocate same model/version in regions with independent quota
        |
        v
Use APIM to route across independently allocated backends
```

Do not add APIM or duplicate deployments merely to avoid measuring demand. The
next stage should activate only when the previous stage is demonstrably
insufficient.

## Validation criteria

Validate each stage with clean minute windows and representative inputs:

| Metric | Purpose |
| --- | --- |
| Assigned TPM per backend | Confirms effective deployment allocation |
| Prompt tokens per minute | Measures actual capacity use |
| TPM utilization | Shows whether more quota is justified |
| Logical inputs per minute | Measures useful throughput |
| HTTP 429 rate | Detects token or request throttling |
| `Retry-After` distribution | Measures recovery behavior |
| Traffic share per backend | Validates APIM weights |
| p50/p95/p99 latency per backend | Detects regional or routing imbalance |
| Failed and retried batches | Measures reliability cost |

Use a steady-state target below the hard allocation, initially 80-90%, and
retain headroom for token estimation differences and traffic variation.

The final aggregate capacity claim must be based on independently confirmed
backend allocations:

$$
\mathrm{usable\ aggregate\ TPM}
\leq\sum_{i=1}^{n}\mathrm{confirmed\ backend\ TPM}_i
$$
