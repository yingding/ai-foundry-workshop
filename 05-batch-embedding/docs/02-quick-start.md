# 02 — Quick Start

**Time:** 10 minutes  
**Goal:** Validate a pre-provisioned workshop environment and submit one successful APIM-pooled AML batch job.

> Disclaimer: This is a learning/sample artifact — not production hardened.

---

## Pre-work Required

The one-hour lab assumes the instructor has already provisioned:

- one Azure Machine Learning workspace and CPU compute cluster;
- one AML batch endpoint with direct and APIM-pooled deployments;
- two independent ADA Embedding Model deployments with matching model version;
- one APIM instance with managed identity, backend pool, and AML-facing API;
- required Foundry and Storage role assignments.

Participants need Python 3.14, `uv`, Azure CLI authentication, and access to the workshop resources.

## Install and Configure

```bash
cd 05-batch-embedding
cp config/.env.example config/.env
uv sync
az login
```

Edit `config/.env` with the workshop resource names. Never commit this file.

## Understand the Two Corpora

| Folder | Shape | Purpose |
|---|---|---|
| `data/workshop-rpm/` | 100 short chunks | Show request amplification and packing |
| `data/workshop-tpm/` | 10 longer chunks | Repeat to create token volume with fewer logical inputs |

The CLI normalizes each JSONL row's `model` field to the selected deployment while preserving `input_id`.

## Validate the Route Plan

```bash
uv run aml-batch-embeddings plan
uv run apim-ada-poc plan
```

Require the plan to show:

- `ada`: **Foundry direct**;
- `ada-apim`: **APIM pooled**;
- both backends' assigned TPM;
- aggregate and target TPM;
- derived target tokens/request.

For the verified environment:

| Item | Value |
|---|---:|
| Primary backend | 15,000 TPM |
| Secondary backend | 15,000 TPM |
| Aggregate assigned capacity | 30,000 TPM |
| Utilization target | 60% |
| APIM plan target | 18,000 TPM |
| Planned requests/minute | 15 |
| Target tokens/request | 1,200 |

Your values can differ. Use the plan output rather than copying this table.
The 60% default preserves headroom for the ADA Embedding Model's measured
call-rate behavior while retaining the verified 1,200-token request ceiling.

## Submit a Smoke Job

```bash
uv run aml-batch-embeddings invoke \
  --model ada-apim \
  --experiment-kind smoke \
  --input data/workshop-rpm \
  --packing batch \
  --max-inputs-per-request 100 \
  --max-tokens-per-request 1200 \
  --max-retries 0 \
  --request-concurrency 1 \
  --metric-logging mlflow \
  --metric-prefix workshop_smoke
```

Pacing is omitted, so both pacing controls remain disabled. This smoke job validates the route, not sustained capacity.

## Export Metrics and Outputs

Replace `<parent-job-id>` with the printed `pipelinejob-...` value:

```bash
uv run aml-batch-embeddings metrics <parent-job-id> \
  --prefix workshop_smoke \
  --output outputs/workshop/smoke-metrics.json

uv run aml-batch-embeddings download <parent-job-id> \
  --output outputs/workshop/smoke-output
```

Require:

- parent and child jobs complete;
- 100 unique `input_id` values;
- every vector contains 1,536 finite values;
- zero error records;
- MLflow metrics appear on the child command job.

Microsoft references:

- [Run Azure OpenAI models in AML batch endpoints](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-use-batch-model-openai-embeddings?view=azureml-api-2&tabs=cli%2Cad)
- [Create jobs and input data for batch endpoints](https://learn.microsoft.com/azure/machine-learning/how-to-access-data-batch-endpoints-jobs?view=azureml-api-2#create-basic-jobs)
- [Log and view MLflow metrics](https://learn.microsoft.com/azure/machine-learning/how-to-log-view-metrics?view=azureml-api-2)

---

Next: [03 — RPM and Packing Experiment](03-rpm-packing-experiment.md)
