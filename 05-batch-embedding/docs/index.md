# 01 — Problem and Solution Architecture

**Time:** 10 minutes  
**Goal:** Explain why document embeddings can hit request and token limits, and assign one responsibility to packing, AML, APIM, and Foundry.

> Disclaimer: This is a learning/sample artifact — not production hardened.

---

## RPM and TPM in One Minute

Azure OpenAI embedding deployments in **Microsoft Foundry** enforce two
separate rate limits:

| Limit | Measures | Typical pressure |
|---|---|---|
| [**RPM — Requests Per Minute**](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits) | How many embedding operations the deployment admits over time | Many small chunks or request bursts |
| [**TPM — Tokens Per Minute**](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits) | How many estimated input tokens the deployment admits over time | Large chunks or token-heavy batches |

Azure assigns an RPM/TPM pair when capacity is allocated to a deployment, but
evaluates request pressure and token pressure separately during inference.
Exceeding either limit can return **HTTP 429 Too Many Requests**. The status code
alone does not identify which limit was reached; use explicit error wording and
available rate-limit headers.

Microsoft Learn references:

- [Manage Azure OpenAI quota — RPM/TPM allocation](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#introduction-to-quota)
- [Understand Azure OpenAI RPM and TPM enforcement](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits)

Packing can reduce client HTTP requests, but it does not remove the tokens in
the input. That is why the workshop tests RPM behavior first and TPM capacity
second.

## From Documents to Embedding Inputs

Documents are split into chunks before embedding. Chunk size changes which
deployment limit becomes visible first:

- many **tiny chunks** create many embedding operations and request-rate
  pressure;
- fewer **large chunks** can consume the deployment's token budget quickly.

The model used here is `text-embedding-ada-002`, called the **ADA Embedding
Model** throughout the workshop. The literal CLI selectors remain `ada` for the
direct route and `ada-apim` for the pooled route.

## Problem 1 — Tiny Chunks Amplify RPM

Assume one document is split into 1,000 small retrieval chunks. In
one-input-per-request mode, the client creates 1,000 HTTP requests even when
each chunk contains only a few tokens. The request or call-rate boundary can be
reached while much of the assigned TPM remains unused.

<div class="mermaid">
flowchart LR
    A["One document"] --> B["Split into 1,000 tiny chunks"]
    B --> C["1 chunk per request"]
    C --> D["1,000 HTTP requests"]
    D --> E["Request burst"]
    E --> F["RPM or call-rate pressure"]
    F --> G["HTTP 429"]
</div>

Packing uses the embeddings API's input array to reduce client requests:

$$
\mathrm{HTTP\ requests}=\left\lceil\frac{N}{B}\right\rceil
$$

For 1,000 logical inputs packed 100 at a time:

$$
\left\lceil\frac{1{,}000}{100}\right\rceil=10\ \mathrm{HTTP\ requests}
$$

| Mode | Logical inputs | Client HTTP requests | Prompt tokens |
|---|---:|---:|---:|
| One input/request | 1,000 | 1,000 | Approximately unchanged |
| 100-input arrays | 1,000 | 10 | Approximately unchanged |

!!! important "What packing proves"
    Packing proves lower **client HTTP request consumption**. The ADA Embedding
    Model can still apply model-side call-rate accounting to logical inputs in
    packed arrays. The workshop therefore measures service feedback instead of
    assuming a 100× model-side RPM improvement.

## Problem 2 — Large Chunks Consume TPM

Now assume several documents produce larger chunks. The client might send only
a few requests, but each request carries many tokens. Azure accumulates the
estimated processed tokens against the deployment's assigned TPM. Once the
token budget is exhausted, further requests can receive HTTP 429.

<div class="mermaid">
flowchart LR
    A["Several documents"] --> B["Split into larger chunks"]
    B --> C["Pack compatible chunks"]
    C --> D["Few token-heavy requests"]
    D --> E["Estimated tokens accumulate"]
    E --> F["Assigned TPM reached"]
    F --> G["HTTP 429"]
</div>

For example, ten requests containing 1,500 tokens each consume approximately:

$$
10\times1{,}500=15{,}000\ \mathrm{tokens}
$$

That is one minute of assigned capacity for a 15,000-TPM deployment, even
though the client used only ten requests. Packing alone does not reduce these
tokens. Token-aware ceilings shape each request, and optional pacing controls
how quickly the requests are offered.

## Azure Embedding Request Limits

Azure accepts one input or an array of inputs. These are hard request-shape limits, not recommended operating targets:

| Limit | Maximum |
|---|---:|
| Inputs in one array | 2,048 |
| Tokens in each input | 8,192 |
| Aggregate input tokens in one request | 300,000 |

References:

- [Generate embeddings with Azure OpenAI](https://learn.microsoft.com/azure/foundry/openai/how-to/embeddings#best-practices)
- [Azure OpenAI Embeddings REST API](https://learn.microsoft.com/rest/api/microsoft-foundry/azureopenai/embeddings#create-embedding)

## Deployment Limits Come First

Usable throughput is constrained by the TPM/RPM pair assigned to the user's deployment. Azure documents:

$$
\mathrm{assigned\ TPM}=\mathrm{SKU\ capacity}\times1{,}000
$$

For capacity 15:

$$
15\times1{,}000=15{,}000\ \mathrm{TPM}
$$

Azure assigns a model-specific RPM proportionally:

$$
\mathrm{assigned\ RPM}
=\frac{\mathrm{assigned\ TPM}}{1{,}000}\times r_{model}
$$

Microsoft does not currently publish $r_{model}$ for the ADA Embedding Model. The approximately 900-RPM value in this project is empirical, not an Azure contract.

References:

- [Manage Azure OpenAI quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#introduction-to-quota)
- [Understand Azure OpenAI rate limits](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits)

!!! warning "Regional quota is not deployment capacity"
    A large unassigned regional quota does not raise one deployment's runtime limit. The experiment reads each deployment's assigned capacity from Azure metadata.

## Packing Is Not Pacing

```text
logical chunks
  -> packing chooses which inputs share one request
  -> token-aware packing closes the array at an item/token ceiling
  -> optional pacing schedules when each request starts
  -> Foundry direct or APIM pooled route executes the request
```

| Control | Purpose |
|---|---|
| Packing | Reduce client HTTP requests |
| Token-aware packing | Keep each packed request predictable and within limits |
| Token pacing | Control offered TPM during a capacity experiment |
| Input-rate pacing | Optional empirical guard for small-chunk call-rate behavior |

Pacing is optional and defaults to disabled. It is an experiment control for bursty AML jobs, not a requirement for every correctly configured endpoint.

## Why AML and APIM?

Azure Machine Learning batch endpoints provide asynchronous jobs, storage-backed inputs/outputs, managed compute, run history, and child-job MLflow metrics. See [Batch endpoints](https://learn.microsoft.com/azure/machine-learning/concept-endpoints-batch?view=azureml-api-2).

APIM exposes two independently allocated model deployments as one backend pool. APIM does not create quota; it routes across existing capacity.

<div class="mermaid">
flowchart LR
    A[Document chunks] --> B[AML batch endpoint]
    B --> C[Pack inputs]
    C --> D[Optional experiment pacing]
    D --> E[APIM backend pool]
    E --> F[ADA Embedding Model 1]
    E --> G[ADA Embedding Model 2]
    B --> H[Embeddings, traces, MLflow metrics]
</div>

## What Is a Circuit Breaker?

A circuit breaker is an official APIM backend feature. A configured rule observes failures such as HTTP 429. When it trips, APIM temporarily stops selecting that backend; if no backend remains eligible, APIM can return HTTP 503. After the trip duration, selection can resume.

APIM states that load balancing and breaker behavior are distributed and approximate. See [APIM backends — circuit breaker](https://learn.microsoft.com/azure/api-management/backends#circuit-breaker).

## Claims the Workshop Tests

1. Packing materially reduces client HTTP requests for identical logical inputs.
2. Token-aware packing preserves IDs and stays within configured request ceilings.
3. A pooled APIM route can exceed one deployment's measured sustained TPM when capacity is independent.
4. AML child metrics and output artifacts make the comparison auditable.
5. A resilience extension can test breaker withdrawal and recovery separately.

!!! success "Key takeaway"
    AML provides the batch job. Packing reduces client request amplification. APIM exposes independent backend capacity. Pacing is optional admission control used to make experiments comparable.

---

Next: [02 — Quick Start](02-quick-start.md)
