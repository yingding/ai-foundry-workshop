# 04 — TPM and APIM Pooling Experiment

**Time:** 15 minutes  
**Goal:** Compare one deployment with an APIM pool using the same packed workload and clean quota windows.

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

At 80% utilization:

$$
\mathrm{direct\ target}=15{,}000\times0.8=12{,}000\ \mathrm{TPM}
$$

$$
\mathrm{pool\ target}=30{,}000\times0.8=24{,}000\ \mathrm{TPM}
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
  --target-tpm 12000 \
  --target-inputs-per-minute 720 \
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
  --target-tpm 24000 \
  --target-inputs-per-minute 1440 \
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

The local HTTP capacity runner previously used matched 100-input arrays over three-minute windows:

| Route | Configured TPM | Steady TPM | Success | 429 | 503 |
|---|---:|---:|---:|---:|---:|
| Direct primary | 15,000 | 14,234.792 | 30/33 | 3 | 0 |
| APIM pool | 24,000 | 23,988.846 | 52/52 | 0 | 0 |
| APIM pool boundary | 27,000 | 25,536.802 | 55/55 | 0 | 0 |

The 24K pooled run exceeded the direct measurement by 68.523%; the 27K run exceeded it by 79.397%. These runs support the pooling hypothesis once, but production selection still requires repetition and backend-member telemetry.

## A Useful Failed Experiment

A direct AML run paced toward 12K TPM immediately after earlier throttling tests still returned explicit **call-rate** 429 responses. Its accepted tokens remained below 15K. This demonstrates:

- token pacing does not prove request/call-rate safety;
- packed logical inputs can still contribute to model-side call-rate pressure;
- experiment ordering and clean windows matter;
- HTTP 429 alone is not enough; retain the error message and counters.

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
