# Foundry new dual-model batch embeddings

This project uses the new Microsoft Foundry project `foundry-proj-yw-uno` and exposes two OpenAI embedding models through one Azure Machine Learning batch endpoint.

| CLI choice | AML deployment | Foundry deployment | Dimensions |
| --- | --- | --- | --- |
| `small` | `embedding-small-v1` | `text-embedding-3-small` | 1,536 |
| `large` | `embedding-large-v1` | `text-embedding-3-large` | 3,072 |

Select the model per invocation with `--model small` or `--model large`.

## Architecture

```text
                         +-> embedding-small-v1 -> text-embedding-3-small
input folder -> one AML batch endpoint
                         +-> embedding-large-v1 -> text-embedding-3-large
```

An AML batch endpoint can contain multiple deployments. The Python SDK selects one with `batch_endpoints.invoke(..., deployment_name=...)`. Two deployments provide an explicit model selector while sharing the endpoint, compute, input contract, monitoring, and output format.

Foundry resource:

- Account: `foundry-proj-yw-uno-resource`
- Project: `foundry-proj-yw-uno`
- Region: East US 2
- Project endpoint: `https://foundry-proj-yw-uno-resource.services.ai.azure.com/api/projects/foundry-proj-yw-uno`

The AML endpoint uses the proven private-networked workspace `aml-workspace-yw-uno`. Local uploads use additive NSP/storage firewall access; AML compute uses workspace-managed Blob/File private endpoints. Existing NSP and storage rules remain intact.

## Setup

```bash
cd 05-embedding
cp config/.env.example config/.env
uv sync
uv run aml-batch-embeddings plan
```

Both local orchestration and the AML component use Python 3.14.

## Private networking

```bash
uv run aml-batch-embeddings network --cidr-prefix 24
```

This SDK-only command verifies managed private endpoints, reuses the existing NSP/profile, adds only missing access, preserves prior rules and association mode, and smoke-tests Blob access.

## Provision one endpoint with two deployments

Set and verify runtime permissions independently:

```bash
uv run aml-batch-embeddings permissions
```

The same setup is available as a dedicated script entry point:

```bash
uv run setup-embedding-permissions
```

The idempotent setup preserves existing assignments and ensures:

- AML compute identity: `Cognitive Services OpenAI User` on the Foundry account.
- AML compute identity: `Storage Blob Data Contributor` on workspace storage.
- AML workspace identity: `Storage Blob Data Contributor` on workspace storage.
- AML workspace identity: `Storage File Data Privileged Contributor` on workspace storage.

The caller running this command must already be allowed to create role assignments, such as through `Owner` or `User Access Administrator`; a process cannot grant that permission to itself.

```bash
uv run aml-batch-embeddings provision
```

The command validates the existing Foundry deployments, grants the AML compute identity access to Foundry and workspace Blob storage, and creates both AML batch deployments. The small deployment is the endpoint default, but invocation always supports explicit selection.

## Invoke the small model

```bash
uv run aml-batch-embeddings invoke --model small --input data
```

## Invoke the large model

```bash
uv run aml-batch-embeddings invoke --model large --input data
```

Each invocation submits an asynchronous parent pipeline job and monitors its child command job.

```bash
uv run aml-batch-embeddings monitor <parent-job-name>
```

On failure, monitoring downloads child artifacts under `outputs/jobs/<child-job-name>/` and prints the relevant user or authentication trace.

## Download results

```bash
uv run aml-batch-embeddings download <parent-job-name> --output outputs/<run-name>
```

The named output contains `embeddings.json`, `embeddings.jsonl`, and `trace.jsonl`. `embeddings.json` is a standard JSON array; JSONL contains the same records one per line for streaming and large-file processing. Trace records include deployment, counts, duration, trace/span IDs, and status without storing source text, vectors, credentials, or tokens.
