# 05 — Evidence, Errors, and Operations

**Time:** 10 minutes  
**Goal:** Export AML metrics, classify rate-limit evidence conservatively, and separate proven claims from production follow-up.

> Disclaimer: This is a learning/sample artifact — not production hardened.

---

## Find Metrics in AML Studio

MLflow metrics belong to the child command job:

1. Open **Azure Machine Learning studio → Jobs**.
2. Open the parent `pipelinejob-...` run.
3. Select the embedding step.
4. Open **Metrics**.
5. Select the experiment prefix, such as `workshop_rpm_packed.*`.

Azure ML automatically starts the MLflow run for job code. The component logs directly with `mlflow.log_metrics()`.

Reference: [View MLflow metrics in Azure Machine Learning studio](https://learn.microsoft.com/azure/machine-learning/how-to-log-view-metrics?view=azureml-api-2#view-information-about-jobs-or-runs-in-the-studio).

## Export Metrics as JSON

Portal screenshots support instruction, but JSON is the reproducible evidence:

```bash
uv run aml-batch-embeddings metrics <parent-or-child-job-id> \
  --prefix workshop_rpm_packed \
  --output outputs/workshop/rpm-packed-metrics.json
```

The report contains:

- requested parent and resolved child IDs;
- child status;
- selected metric prefix;
- attempted/successful RPM;
- accepted TPM and prompt-token totals;
- request/input totals and success rate;
- 429 count/rate;
- latency percentiles;
- item/token fill ratios;
- configured experiment pacing targets.

`trace.jsonl` remains the request-level record. It stores statuses, retry guidance, request timing, and token counts without source text, vectors, credentials, or bearer tokens.

## Distinguish RPM from TPM Evidence

Both limiters can return HTTP 429. Classify evidence in this order:

| Evidence | Classification | Confidence |
|---|---|---|
| Error explicitly says `call rate` or `request rate` | `rpm-explicit` | Proof for that response |
| Error explicitly says `token rate` | `tpm-explicit` | Proof for that response |
| Request counter is zero while token counter has headroom | `rpm-likely` | Diagnostic inference |
| Token counter is zero while request counter has headroom | `tpm-likely` | Diagnostic inference |
| Both counters exhausted, absent, or conflicting | `unknown` | Do not guess |

`Retry-After` tells the client when to retry. It does not identify the limiter. Latency, request sequence, or local token estimates alone are also insufficient.

Azure documents that RPM and TPM are paired at quota allocation but measured separately during inference: [Understand rate limits](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#understanding-rate-limits).

## Circuit-Breaker Evidence

The configured APIM rule trips a backend after repeated 429 responses in its observation interval. When tripped:

- APIM stops selecting that backend temporarily;
- remaining eligible backends can continue serving;
- APIM can return 503 when no backend remains eligible;
- traffic can resume after the trip duration.

A 429 does not prove the breaker opened. A later 503 is consistent with no eligible backend, but authoritative proof requires APIM diagnostics or Azure Monitor backend telemetry.

Reference: [APIM circuit breaker](https://learn.microsoft.com/azure/api-management/backends#circuit-breaker).

## Resilience Extension

Run separately from capacity measurement:

1. Establish successful traffic and backend-member telemetry.
2. Deliberately overload one backend with retries disabled.
3. Observe enough configured 429 responses to satisfy the breaker rule.
4. Verify traffic stops reaching that backend.
5. Verify the other backend continues when eligible.
6. After the trip duration, verify the backend receives traffic again.

Do not use this failure injection during the direct-versus-pool capacity A/B.

## Evidence Status

| Claim | Status | Remaining work |
|---|---|---|
| Packing reduces client HTTP requests | Verified: 100 → 1 for identical 100 inputs | Repeat for representative distributions |
| Token estimates match service prompt tokens | Verified on packed workshop runs | Monitor after tokenizer/model changes |
| APIM managed-identity route works | Verified | None for path validation |
| Pool exceeds one direct deployment | Verified in one 24K and one 27K sustained run | Repeat and capture backend-member telemetry |
| Exact ADA Embedding Model RPM | Unknown | Service did not return request-limit header; public ratio unavailable |
| Packed arrays reduce model-side call accounting 1:1 | Not proven | Controlled clean-window boundary experiment |
| Circuit breaker withdraws/restores one backend | Pending | Failure injection plus APIM diagnostics |

## Workshop Completion Checklist

- [x] Explain service request limits versus deployment capacity.
- [x] Run and export the RPM packing A/B.
- [x] Explain direct and pooled TPM targets.
- [x] Locate child metrics in AML Studio.
- [x] Classify 429 evidence without guessing.
- [ ] Repeat sustained TPM and breaker experiments before production adoption.

---

Return to [01 — Problem and Solution Architecture](index.md).
