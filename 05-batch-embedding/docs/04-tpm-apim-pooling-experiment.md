# 04 — TPM and APIM Pooling Experiment

**Time:** 15 minutes  
**Goal:** Compare one deployment with an APIM pool using the same packed workload and clean quota windows.

> Disclaimer: This is a learning/sample artifact — not production hardened.

---

## Capacity Model

For two independently allocated backends:

$$
\mathrm{aggregate\ TPM}=\mathrm{TPM}_1+\mathrm{TPM}_2
$$

For this environment:

$$
15{,}000+15{,}000=30{,}000\ \mathrm{TPM}
$$

At the workshop's 60% utilization plan:

$$
\mathrm{direct\ target}=15{,}000\times0.6=9{,}000\ \mathrm{TPM}
$$

$$
\mathrm{pool\ target}=30{,}000\times0.6=18{,}000\ \mathrm{TPM}
$$

APIM does not create quota. The capacity must be independently assigned to eligible backends.

## Use the TPM Corpus

`data/workshop-tpm/sample.jsonl` contains 10 longer chunks:

| Property | Measured value |
|---|---:|
| Tokens per cycle | 442 |
| Tokens/input | 39–49 |
| Recommended repetitions | 40 |
| Approximate total tokens | 17,680 |
| Logical inputs | 400 |

This corpus creates token volume with fewer logical inputs than the RPM corpus.

## Optional Experiment Pacing

Pacing is disabled by default. The lab enables it only to offer comparable load without relying on retries.

For each packed request:

$$
\mathrm{interval}=
\max\left(
\frac{\mathrm{batch\ tokens}\times60}{\mathrm{target\ TPM}},
\frac{\mathrm{batch\ inputs}\times60}{\mathrm{target\ inputs/minute}}
\right)
$$

The logical-input target is empirical because Microsoft does not publish the ADA Embedding Model RPM ratio. A production design can implement admission control in a queue, scheduler, or distributed rate limiter instead.

## Clean-Window Rule

!!! warning
    Do not run the pooled job immediately after a throttled direct job. `Retry-After` and rolling service state can contaminate the next result. Use separate clean windows and record job order.

The one-hour workshop should use pre-recorded sustained evidence while participants run shorter path-validation jobs. Instructor capacity runs should use two-to-five-minute windows and repeat selected points at least three times.

### Workshop plan and measured headroom

The current plan uses 60% of assigned TPM plus conservative logical-input
ceilings:

| Route | Assigned TPM | Plan target (60%) | Input target/minute |
|---|---:|---:|---:|
| Direct | 15,000 | 9,000 | 180 |
| APIM pool | 30,000 | 18,000 | 360 |

Earlier 80% experiments used 12K direct and 24K pooled targets. The 12K AML
direct run received explicit **call-rate** HTTP 429 responses, so 80% remains
historical boundary evidence rather than the default workshop plan. The 60%
settings are empirically safe for this workload, not universal Azure defaults.

## A — Direct Control

```bash
uv run aml-batch-embeddings invoke \
  --model ada \
  --experiment-kind tpm \
  --input data/workshop-tpm \
  --repeat-inputs 40 \
  --packing batch \
  --max-inputs-per-request 100 \
  --max-tokens-per-request 1200 \
  --target-tpm 9000 \
  --target-inputs-per-minute 180 \
  --max-retries 0 \
  --request-concurrency 1 \
  --metric-logging mlflow \
  --metric-prefix workshop_tpm_direct
```

## B — APIM-Pooled Route

Run in a separate clean quota window:

```bash
uv run aml-batch-embeddings invoke \
  --model ada-apim \
  --experiment-kind tpm \
  --input data/workshop-tpm \
  --repeat-inputs 40 \
  --packing batch \
  --max-inputs-per-request 100 \
  --max-tokens-per-request 1200 \
  --target-tpm 18000 \
  --target-inputs-per-minute 360 \
  --max-retries 0 \
  --request-concurrency 1 \
  --metric-logging mlflow \
  --metric-prefix workshop_tpm_pool
```

## Export Evidence

```bash
uv run aml-batch-embeddings metrics <direct-parent-job-id> \
  --prefix workshop_tpm_direct \
  --output outputs/workshop/tpm-direct-metrics.json

uv run aml-batch-embeddings metrics <pool-parent-job-id> \
  --prefix workshop_tpm_pool \
  --output outputs/workshop/tpm-pool-metrics.json
```

## Existing Sustained Evidence

### AML safe-rate comparison

The completed AML comparison used the same 400 logical inputs, 17,680 prompt
tokens, 16 packed requests, 1,200-token ceiling, retry policy, and worker count.
Only the route and offered-load target changed:

| Metric | Direct ADA Embedding Model | APIM pool |
|---|---:|---:|
| Parent job | `pipelinejob-95f9db54-ce1f-43c2-9e53-0768b86647fb` | `pipelinejob-0882017a-081c-43a8-9a73-11394a7fbc47` |
| Child task | `593bc954-5ed3-46d4-a104-ac476dc2fdfe` | `82ef2a34-f84d-4707-91d0-b736fc6eba1e` |
| Configured target TPM | 9,000 | 18,000 |
| Configured target inputs/minute | 180 | 360 |
| Accepted TPM | 7,946.988 | 15,867.064 |
| Successful inputs | 400 | 400 |
| Successful requests | 16 | 16 |
| Prompt tokens | 17,680 | 17,680 |
| HTTP 429 | 0 | 0 |
| Request-window seconds | 133.485 | 66.855 |
| Latency p50 | 1,082.512 ms | 297.979 ms |
| Latency p95 | 1,384.656 ms | 1,690.912 ms |
| Latency p99 | 1,791.222 ms | 3,904.900 ms |

### Reading latency percentiles

Latency percentiles answer, “How long did requests take?” without allowing one
average to hide unusually slow requests:

| Metric | Plain-language meaning |
|---|---|
| **p50** | Median latency: half of requests completed at or below this value |
| **p95** | 95% of requests completed at or below this value; the slowest 5% took longer |
| **p99** | 99% of requests completed at or below this value; it highlights the extreme tail |

For example, pooled p95 of 1,690.912 ms means approximately 95% of measured
requests completed within 1.691 seconds. It does **not** mean that requests were
95% successful or 95% faster.

This comparison contains only 16 requests per route. The code calculates
percentiles by linear interpolation, so p95/p99 are useful warning signals but
are not stable production estimates. Use hundreds or thousands of requests for
reliable tail-latency conclusions.

The pooled route increased accepted TPM by:

$$
\frac{15{,}867.064}{7{,}946.988}-1=99.661\%
$$

Downloaded output validation confirmed identical sets of 400 `input_id`
values, zero error records, and finite 1,536-dimensional vectors on both routes.
The result supports the pooled-capacity hypothesis at this safe operating point.

The latency distribution is mixed: a typical pooled request was faster because
p50 fell from 1,082.512 ms to 297.979 ms, while the small sample's slow tail was
worse because p95/p99 increased. Higher aggregate throughput therefore does not
prove uniformly lower latency. Repeat with a much larger request count before
selecting a production operating point.

### Deep evidence

Earlier three-minute local HTTP controls and the failed 12K AML experiment are
retained in [APIM ADA proof-of-concept evidence](apim-ada-poc.md#initial-verified-result)
and [RPM optimization](rpm-optimization.md#why-dual-pacing-was-added). Lesson 5
uses the failed run only to explain how to interpret throttling; it is not part
of the successful capacity A/B.

## Pass Criteria

- same corpus, model version, packing ceiling, retry policy, and observation method;
- pooled accepted TPM exceeds the direct measurement;
- less than 1% HTTP 429 at the selected operating point;
- zero HTTP 503 in the capacity run;
- all input IDs and output dimensions validate;
- repeat before choosing a production target.

Microsoft reference: [APIM load-balanced backend pools](https://learn.microsoft.com/azure/api-management/backends#load-balanced-pool).

---

Next: [05 — Evidence, Errors, and Operations](05-evidence-and-operations.md)
