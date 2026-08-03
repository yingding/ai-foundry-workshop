# 01 — Problem and Solution Architecture

**Time:** 10 minutes  
**Goal:** Explain why document embeddings can hit request and token limits, and assign one responsibility to packing, AML, APIM, and Foundry.

> Disclaimer: This is a learning/sample artifact — not production hardened.

---

## The Document-Chunk Problem

Documents are split into smaller chunks before embedding. Smaller chunks can improve retrieval precision, but one document can become hundreds or thousands of logical embedding inputs.

| Workload | Logical inputs | Client HTTP requests | Pressure |
|---|---:|---:|---|
| One input per request | 100 | 100 | High client request rate |
| Packed input array | 100 | 1–2 | Similar tokens, fewer HTTP requests |

The model used here is `text-embedding-ada-002`, called the **ADA Embedding Model** throughout the workshop. The literal CLI selectors remain `ada` for the direct route and `ada-apim` for the pooled route.

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
+=\frac{\mathrm{assigned\ TPM}}{1{,}000}\times r_{model}
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
