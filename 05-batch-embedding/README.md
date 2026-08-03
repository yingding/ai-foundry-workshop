# Foundry batch embeddings

> Disclaimer: This is a learning/sample artifact — not production hardened.

## Contents

- [Project Map](#project-map)
- [Workshop Tutorial](#workshop-tutorial)
- [Input and Output Format](#input-and-output-format)
- [RPM Optimization](#rpm-optimization)
- [Architecture](#architecture)
- [Setup](#setup)
- [Provisioning](#provision-one-endpoint-with-four-deployments)
- [Invoking Deployments](#invoke-the-small-model)
- [Downloading Results](#download-results)

## Project Map

| Path | Audience | Purpose |
|---|---|---|
| `batch_embeddings.py` | Operators and developers | Provision AML deployments, submit/monitor jobs, download outputs, and export MLflow metrics |
| `component/embed.py` | Component developers | Validate, pack, optionally pace, call the model endpoint, and emit outputs/traces/metrics |
| `utils/embedding_optimization.py` | Developers | Shared packing, token counting, pacing, and throughput formulas |
| `utils/aml_metrics.py` | Developers | Run-level RPM/TPM, reliability, latency, and packing metrics with optional MLflow publishing |
| `analyze_workshop_experiments.py` | Workshop authors | Regenerate the experiment comparison chart from exported AML metric JSON reports |
| `requirements_dev.txt` | Workshop authors | Runtime dependencies plus Matplotlib for chart generation |
| `data/experiment-metrics/` | Workshop authors | Sanitized committed metric fixtures for offline chart regeneration |
| `docs/index.md` through `docs/05-*.md` | Workshop participants | The five-page, one-hour guided tutorial |
| `docs/apim-ada-poc.md` | Implementers | Full APIM provisioning, policy, experiment, and acceptance evidence |
| `docs/rpm-optimization.md` | Implementers | Packing and optional admission-control design |
| `docs/tpm-optimization.md` | Implementers | Deployment capacity and APIM pooling strategy |
| `docs/tpm-rpm-verification.md` | Reviewers | Azure formulas, CLI verification, and empirical discrepancies |
| `docs/rbac-concept.md` | Security reviewers | Managed identity and role-assignment model |

## Workshop Tutorial

Run the MkDocs site from the repository root:

```bash
uvx --from mkdocs-material mkdocs serve
```

The tutorial navigation intentionally contains five lessons. The additional
Markdown files under `docs/` are deep references linked from those lessons and
are not separate workshop steps.

This sample exposes three Microsoft Foundry embedding models through four
deployments on one Azure Machine Learning batch endpoint. Direct
`text-embedding-ada-002` (ADA Embedding Model) and APIM-pooled ADA Embedding
Model routes remain separate for A/B testing and rollback.

| CLI choice | AML deployment role | Foundry model | Dimensions |
| --- | --- | --- | --- |
| `small` | Small-model deployment | `text-embedding-3-small` | 1,536 |
| `large` | Large-model deployment | `text-embedding-3-large` | 3,072 |
| `ada` | Direct ADA Embedding Model deployment | `text-embedding-ada-002` | 1,536 |
| `ada-apim` | ADA Embedding Model through APIM regional pool | `text-embedding-ada-002` | 1,536 |

Select the model per invocation with `--model small`, `--model large`,
`--model ada`, or `--model ada-apim`.

## Input and output format

The input folder must contain JSONL files. Each non-empty line uses this explicit AML batch schema:

```json
{"input_id":"document-42:chunk-0","input":"Text to embed","model":"text-embedding-3-small","encoding_format":"float"}
```

The AML parent job name is the batch request ID. Inside each JSONL line, `input_id` identifies the corresponding OpenAI [create embeddings](https://developers.openai.com/api/reference/resources/embeddings) `input`. For a string input, use one string ID. For an array input, use a parallel ID array of the same length. The remaining fields map directly to the OpenAI body: `model`, `dimensions`, `encoding_format`, and `user`. The request model must match the AML deployment selected with `--model`.

Successful output keeps the OpenAI response shape. Every `data` item contains the matching `input_id` and OpenAI `index`:

```json
{"object":"list","data":[{"object":"embedding","input_id":"document-42:chunk-0","index":0,"embedding":[0.1,0.2]}],"model":"text-embedding-3-small","usage":{"prompt_tokens":3,"total_tokens":3}}
```

A failed line is `{"input_ids":["document-42:chunk-0"],"error":{"code":"...","message":"..."}}`. Input IDs must be unique across the uploaded batch.

## RPM optimization

Each current Foundry deployment has capacity `15`, corresponding to 15,000
assigned TPM. The subscription's regional ADA quota pool is 2,593,000 TPM, but
only the 15,000 TPM assigned to the ADA deployment determines that deployment's
runtime TPM and paired RPM limits. Microsoft does not currently publish ADA's
capacity-to-RPM ratio in its quota table. The measured ADA behavior is
consistent with approximately 900 RPM, or about 60 RPM per 1,000 assigned TPM,
but this remains an empirical inference until the live response header confirms
it.
See [TPM and RPM verification](docs/tpm-rpm-verification.md) for the Azure CLI
commands, formulas, runtime test procedure, and Microsoft Learn references.
See [RPM optimization plan](docs/rpm-optimization.md) for the measured plan to
batch by default, size arrays by tokens, pace against TPM, and validate
utilization.
See [TPM optimization plan](docs/tpm-optimization.md) for the escalation from
deployment allocation through quota requests, independent regional capacity,
and APIM routing.
See [Identity and RBAC concept](docs/rbac-concept.md) for the AML compute,
workspace, APIM, Foundry, and Storage identity and role-assignment model.
See [APIM ADA proof of concept](docs/apim-ada-poc.md) for provisioning, smoke
testing, paced load steps, metrics, and acceptance criteria.

`utils/embedding_optimization.py` is the shared optimization layer. The AML
component uses `pack_compatible_requests` to reduce HTTP requests per logical
input. The local APIM experiments use the same packer for RPM tests and use
`pacing_interval_seconds` plus `tokens_per_minute` for TPM tests. Direct ADA and
APIM-pooled ADA therefore apply the same request-packing behavior.

Packing and pacing are separate. Packing creates input arrays; token-aware
packing closes an array at its item or token ceiling. Optional AML dual pacing
then delays request starts by the stricter of the configured token-rate interval
and logical-input-rate interval. Use `--target-tpm` for token pacing and
`--target-inputs-per-minute` for the empirical ADA Embedding Model call-rate
guard. The input-rate target is workload-derived because Microsoft does not
currently publish the model's exact RPM ratio.

Both pacing controls are optional and default to disabled. They are useful for
controlled experiments or bursty jobs; a naturally low-rate workload with
sufficient assigned capacity does not need an in-component pacing algorithm.
AML provides asynchronous job orchestration and APIM provides routing, but
neither service automatically schedules model calls to the current Foundry
TPM/RPM counters. Production admission control can instead live in a queue,
distributed rate limiter, or workflow scheduler.

RPM and TPM are dependent only when quota is allocated: each model capacity
unit supplies a predefined RPM/TPM pair, so they cannot be configured
independently. During inference, Azure measures request rate and estimated token
rate separately. Exceeding either counter can return HTTP 429; runtime RPM is
not calculated from the tokens consumed by each request.

The AML component can batch JSONL lines with matching request settings into OpenAI array inputs. This reduces online requests without materially changing the tokens processed:

| Mode | Sample inputs | Online embedding requests | Relative RPM use |
| --- | ---: | ---: | ---: |
| `none` | 100 | 100 | 100% |
| `batch` | 100 | 1 | 1% |

Run the local A/B demonstration over the same
`data/workshop-rpm/short-chunks.jsonl` corpus:

```bash
uv run aml-batch-embeddings test \
    --model small --input data/workshop-rpm --output outputs/demo-unoptimized \
    --packing none

uv run aml-batch-embeddings test \
    --model small --input data/workshop-rpm --output outputs/demo-optimized \
    --packing batch
```

Both runs process the same 100 IDs. In the live test, the unoptimized output had 100 response records and the optimized output had one response record containing 100 `data` items. All 100 inputs succeeded in both modes. The embedding loop took 35,259 ms without packing and 4,713 ms with packing. Trace attributes report `embedding.source_line_count`, `embedding.input_count`, and `embedding.online_request_count`.

Disabling retries and sending 100 one-input-per-request calls with 20 workers still did
not produce a 429: all 100 completed in 3,275 ms. The packed control completed
as one request in 4,258 ms. This proves the 100-record fixture is useful for
showing request reduction, but it is below the expected 166.67 requests per
10-second evaluation window. The throttle test used 1,200 short inputs with 100
workers and `--max-retries 0` to exceed that short-window budget.

The live 1,200-input test produced 996 successes and 204 Foundry
`RateLimitReached` responses. Foundry instructed the client to retry after 30
seconds, and the original `openai.RateLimitError` failed the AML job. A packed
control placed the same 1,200 inputs in one HTTP request, but Foundry also
rejected it with `RateLimitReached` and a 60-second retry instruction while the
endpoint was still recovering from the one-input-per-request burst. Therefore that packed run
does not isolate whether array items affect call-rate accounting. Packing
unambiguously reduces client HTTP requests; a clean packed comparison must run
after the endpoint's rolling throttle state has fully cleared.

### Measured rate-limit results

Measured on 2026-07-31 against `text-embedding-3-small`, with SDK retries
disabled:

| Metric | 1,200 one-input-per-request calls | 1,200 packed inputs |
| --- | ---: | ---: |
| HTTP embedding requests | 1,200 | 1 |
| Request concurrency | 100 | 1 |
| Component duration | 39,849 ms | 1,205 ms |
| Successful HTTP responses | 996 | 0 |
| HTTP 429 responses | 204 | 1 |
| Peak request starts in a rolling 10 seconds | 391 | 1 |
| Request latency p50 | 2,679 ms | 1,137 ms |
| Request latency p95 | 6,103 ms | 1,137 ms |
| Request latency p99 | 6,379 ms | 1,137 ms |
| Foundry retry instruction | 30 seconds | 60 seconds |
| AML outcome | Failed with endpoint 429 | Failed with endpoint 429 |

A smaller boundary test sent 200 one-input-per-request calls with 100 workers and no SDK
retries. All 200 request starts occurred within 2,226 ms, all returned HTTP 200,
and the component finished in 7,822 ms. This exceeded the nominal 166.67
requests per 10-second pacing calculation without throttling. The formula is
therefore useful for conservative client pacing, but it is not an exact public
cutoff; Foundry applies service-controlled burst capacity and evaluation state.

### Measured ADA Embedding Model rate limit

The ADA deployment `text-embedding-ada-002-test` has capacity 15, or 15,000
assigned TPM. It accepted 400 one-input-per-request calls whose starts fit inside 9.588
seconds. A 1,200-request run with 100 workers and SDK retries disabled returned
870 HTTP 200 responses and 330 HTTP 429 responses. Throttling began around
submission sequence 814; all sequences 0-799 succeeded, and no sequence 1000 or
greater succeeded. The successful responses consumed 9,060 actual prompt
tokens, below 15,000 TPM, while Foundry explicitly reported a call-rate limit.

The 429 `Retry-After` values ranged from 26 to 32 seconds. Adding each value to
its response time produced a common reset approximately 61-62 seconds after the
run began. This is consistent with an approximately 900-request one-minute
bucket:

$$
15 \times 60 \approx 900\ \text{RPM}
$$

The exact value is not yet a documented contract. Concurrent requests race at
the boundary, rejected requests can count against the limiter, and 870
successes do not imply an 870-RPM allocation. The component now records
`x-ratelimit-*` response headers on successful calls so subsequent runs can
report the authoritative request and token limits when Foundry returns them. A
two-request packed ADA probe returned `x-ratelimit-limit-tokens: 15000` and
remaining-token values, confirming the assigned TPM, but omitted
`x-ratelimit-limit-requests`. The approximately 900-RPM value therefore remains
an enforcement-based estimate.

The successful 100-input packed request reported 1,030 prompt tokens. Repeating
that workload 12 times projects to about 12,360 prompt tokens, below the
deployment's 15,000 TPM allocation. This estimate does not prove which limiter
rejected the packed control because it followed immediately after a throttled
burst. Azure enforces request rate and token rate as separate counters; either
counter can return HTTP 429, and the endpoint error text does not identify the
counter precisely.

Use `--request-concurrency` only for controlled load testing. Normal invocations
default to one worker. Comparable runs use a stable experiment name such as
`embeddings-tpm-ada-apim-packed-input-array`. Each invocation also requests a
detailed job name such as
`embeddings-tpm-ada-apim-packed-input-array-records-200-items-128-tokens-1200-retries-0-workers-1-2026-08-03-131500z`.
AML can still display its generated immutable `pipelinejob-...` identifier.

The CLI retains `--packing none` and `--packing batch` for compatibility. Their
explicit meanings are **one input per HTTP request** and **packed input array**,
respectively.

Batching is deterministic and groups only requests with identical `model`,
`dimensions`, `encoding_format`, and `user` values. It does not semantically
shuffle or cluster text. `--max-inputs-per-request` controls the maximum batched
array size and defaults to 128.

| Azure embedding request limit | Maximum |
| --- | ---: |
| Inputs in one array | 2,048 |
| Tokens in each individual input | 8,192 |
| Aggregate input tokens in one request | 300,000 |

See [Azure embedding best practices](https://learn.microsoft.com/azure/foundry/openai/how-to/embeddings#best-practices)
and the [Embeddings REST API](https://learn.microsoft.com/rest/api/microsoft-foundry/azureopenai/embeddings#create-embedding).
The deployed AML component uses batch mode and disables pipeline output reuse
so each submitted job calls Foundry.

## Architecture

```text
                         +-> small AML deployment -> text-embedding-3-small
input folder -> AML batch endpoint
                         +-> large AML deployment -> text-embedding-3-large
                         +-> ada AML deployment   -> text-embedding-ada-002
                         +-> ada-apim deployment  -> APIM pool -> two ADA v2 backends
```

An AML batch endpoint can contain multiple deployments. The Python SDK selects
one with `batch_endpoints.invoke(..., deployment_name=...)`. Four deployments
provide an explicit model and route selector while sharing the endpoint,
compute, input contract, monitoring, and output format.

Resource names, subscription identifiers, regions, and endpoints are supplied through the ignored `config/.env` file. Local uploads use additive NSP/storage firewall access; AML compute uses workspace-managed Blob/File private endpoints. Existing NSP and storage rules remain intact.

## Setup

```bash
cd 05-batch-embedding
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

## Provision one endpoint with four deployments

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

The command validates the existing Foundry deployments, grants the AML compute
identity access to Foundry and workspace Blob storage, and creates all four AML
batch deployments. The small deployment is the endpoint default, but invocation
always supports explicit selection.

Provision only the parallel APIM-pooled ADA deployment without updating the
existing direct deployments or endpoint default:

```bash
uv run aml-batch-embeddings provision-apim
```

## Invoke the small model

```bash
uv run aml-batch-embeddings invoke --model small --input data/workshop-rpm
```

## Invoke the large model

```bash
uv run aml-batch-embeddings invoke --model large --input data/workshop-rpm
```

## Invoke the ADA Embedding Model directly

```bash
uv run aml-batch-embeddings invoke --model ada --input data/workshop-rpm
```

## Invoke the ADA Embedding Model through the APIM pool

```bash
uv run aml-batch-embeddings invoke --model ada-apim --input data/workshop-rpm
```

The `ada-apim` deployment obtains a token for the Cognitive Services audience
and sends it to a separate AML-facing APIM API. APIM validates the AML compute
object ID, then authenticates to either Foundry backend with its own managed
identity. No APIM subscription key or Foundry key is stored in the AML job.

Each invocation submits an asynchronous parent pipeline job and monitors its child command job.

```bash
uv run aml-batch-embeddings monitor <parent-job-name>
```

On failure, monitoring downloads child artifacts under `outputs/jobs/<child-job-name>/` and prints the relevant user or authentication trace.

## Download results

```bash
uv run aml-batch-embeddings download <parent-job-name> --output outputs/<run-name>
```

The named output contains `embeddings.jsonl` and `trace.jsonl`. Trace records include deployment, counts, duration, trace/span IDs, and status without storing source text, vectors, credentials, or tokens.
