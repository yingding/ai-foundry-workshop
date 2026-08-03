# RPM optimization plan

This plan improves throughput through the online embeddings endpoint by batching
multiple embedding inputs into each HTTP request. The objective is to reduce RPM
consumption while using more of the deployment's assigned TPM.

The implementation lives in `utils/embedding_optimization.py` as
`pack_compatible_requests`. Both the AML embedding component and the APIM RPM
experiment call this function, so direct and pooled routes use the same grouping
and chunking rules.

See [TPM optimization plan](tpm-optimization.md) when measured demand exceeds
the token capacity assigned to the deployment.

This is both a design record and an implemented validation plan. Token-aware
packing is implemented in `utils/embedding_optimization.py` with `tiktoken`.

## Measured baseline

The `text-embedding-ada-002-test` deployment has 15,000 assigned TPM. A live
one-input-per-request test sent 1,200 HTTP requests with 100 workers and SDK
retries disabled:

- 870 requests returned HTTP 200.
- 330 requests returned HTTP 429 `RateLimitReached`.
- Successful responses consumed 9,060 prompt tokens.
- Retry instructions converged on a minute-scale reset.
- The observed request boundary is consistent with approximately 900 RPM.

A separate batch-mode probe sent 200 logical inputs in two HTTP requests. Both
requests succeeded and confirmed `x-ratelimit-limit-tokens: 15000`. This shows
that embedding arrays can reduce request consumption without avoiding token
accounting.

## 1. Batch by default

Use `--packing batch` for normal batch-endpoint invocations. Use
`--packing none` means one input per HTTP request and is used only for
diagnostics and controlled rate-limit tests.

Batch mode combines inputs only when their request settings match:

- model;
- dimensions;
- encoding format;
- user.

The OpenAI embeddings endpoint receives one input array for each batch. Input
IDs remain associated with response indexes so batching does not lose
correlation.

For $N$ logical inputs and an average of $B$ inputs per HTTP request:

$$
\mathrm{HTTP\ requests}=\left\lceil\frac{N}{B}\right\rceil
$$

$$
\mathrm{RPM\ reduction}=1-\frac{1}{B}
$$

For the 100-line sample:

| Mode | Logical inputs | HTTP requests | Relative RPM use |
| --- | ---: | ---: | ---: |
| `none` | 100 | 100 | 100% |
| `batch` | 100 | 1 | 1% |

Batching 100 inputs per request therefore reduces client-side request
consumption by 99% while processing approximately the same input tokens.

## 2. Size batches by tokens

The item-count limit protects request size, but equal item counts do not imply
equal token counts. The implemented packer stops a batch when either limit is
reached:

- maximum inputs per request;
- target estimated tokens per request.

The target is derived from assigned TPM rather than the service's maximum request
size. For deployment TPM $T$, utilization target $u$, and planned HTTP requests
per minute $R$:

$$
\mathrm{target\ tokens\ per\ request}=\frac{T\cdot u}{R}
$$

For ADA with $T=15{,}000$, $u=0.8$, and $R=10$:

$$
\frac{15{,}000\cdot0.8}{10}=1{,}200\ \mathrm{tokens/request}
$$

The measured packed sample used approximately 1,224 token units per 100-input
request. That makes 100 inputs, or about 1,200 tokens, a reasonable initial
batch target for this fixture. Production data must be measured because input
lengths can differ substantially.

The batching algorithm preserves input order within each settings group, starts
a new array before the token target is exceeded, and always enforces the
published embedding endpoint limits of 2,048 inputs and 300,000 aggregate input
tokens per request.

## 3. Pace batches against TPM

Batching removes the immediate RPM bottleneck, but sending every batch at once
can move the workload directly into TPM throttling. Pace completed arrays over
the minute instead of releasing one large burst.

For assigned TPM $T$, target utilization $u$, and estimated batch tokens $b$:

$$
\mathrm{batches\ per\ minute}=\left\lfloor\frac{T\cdot u}{b}\right\rfloor
$$

$$
\mathrm{spacing\ seconds}=\frac{60}{\mathrm{batches\ per\ minute}}
$$

Using $T=15{,}000$, $u=0.8$, and $b=1{,}200$:

$$
\mathrm{batches\ per\ minute}=10
$$

$$
\mathrm{spacing}=6\ \mathrm{seconds}
$$

The initial operating point is therefore:

- batch mode enabled;
- approximately 100 sample inputs or 1,200 estimated tokens per request;
- 10 requests per minute;
- one request approximately every six seconds;
- 12,000 target tokens per minute, or 80% of assigned TPM.

This is a conservative starting point, not a permanent constant. Validate it
with representative input distributions before raising the utilization target.

## 6. Measure utilization and outcomes

Each validation run should report both logical throughput and online endpoint
consumption.

| Metric | Purpose | Initial target |
| --- | --- | ---: |
| Logical inputs per minute | Measures useful throughput | At least 1,000 for the sample distribution |
| HTTP requests per minute | Measures RPM consumption | Approximately 10 |
| Inputs per HTTP request | Confirms batching efficiency | Approximately 100 |
| Prompt tokens per request | Validates token-aware sizing | Approximately 1,200 |
| Prompt tokens per minute | Measures TPM utilization | 12,000-13,500 |
| TPM utilization | Assigned TPM actually used | 80-90% |
| HTTP 429 rate | Detects overdriving | Below 1% |
| Retry delay | Quantifies throttling cost | Zero during the steady-state test |
| Request latency p50/p95/p99 | Detects oversized batches | Record and compare across batch sizes |
| End-to-end duration | Measures total batch performance | Lower than one-input-per-request mode for the same inputs |

Calculate TPM utilization as:

$$
\mathrm{TPM\ utilization}=\frac{\mathrm{prompt\ tokens\ processed\ in\ window}}{\mathrm{assigned\ TPM}}
$$

Calculate batching efficiency as:

$$
\mathrm{batching\ efficiency}=\frac{\mathrm{logical\ inputs}}{\mathrm{HTTP\ requests}}
$$

## Validation sequence

1. Establish a one-input-per-request control with `--packing none` and retries disabled.
2. Run `--packing batch` over the same inputs and compare IDs and outputs.
3. Test token targets around 800, 1,000, 1,200, and 1,500 tokens per request.
4. Pace each candidate at 70%, 80%, and 90% of assigned TPM.
5. Repeat each setting across several clean minute windows.
6. Select the highest-throughput setting that keeps HTTP 429 below 1% and does
   not materially degrade p95 latency.

Run the focused APIM RPM packing experiment with:

```bash
uv run apim-ada-load rpm \
   --target gateway \
   --inputs 100 \
   --batch-size 100 \
   --output outputs/apim-ada-rpm-gateway
```

The verified run packed 100 logical inputs into one successful APIM request,
reporting 100 inputs per request and a 99% request reduction with no HTTP 429.
This proves request reduction, not token reduction: the packed request still
consumed the tokens for all 100 inputs.

With automatic capacity-aware sizing enabled, Azure metadata reported two
15,000-TPM backends. At 80% utilization and 20 planned requests/minute, the
derived token ceiling was 1,200 tokens/request. The same 100 logical inputs then
formed two requests: 85 inputs/1,190 tokens and 15 inputs/210 tokens. Estimated
and service-reported prompt tokens matched exactly at 1,400 total. Request use
was still reduced by 98% compared with one input per request.

The smaller final batch is expected. A greedy ordered packer fills each request
up to the item or token ceiling and emits the remainder as the final request.
Report p50/p95/max token size and item count rather than judging optimization
from the final batch alone.

The selected operating point must come from repeated measurements. The current
100-input and 1,200-token values are baseline candidates derived from this
sample, not universal embedding settings.

## AML child-step metrics

The embedding command component can publish run-level scalars to the AML child
job through MLflow. Publishing is controlled per invocation:

- `metric_logging=mlflow` publishes metrics to the child job's **Metrics** tab;
- `metric_logging=disabled` suppresses MLflow publishing;
- `metric_prefix` namespaces metric names for dashboards and comparisons.

Structured `trace.jsonl` output remains enabled in both modes. Disabling MLflow
therefore changes portal visibility, not the auditable request record.

The default child-step metric set is intentionally bounded:

| Group | Metrics | Purpose |
| --- | --- | --- |
| Throughput | attempted RPM, successful RPM, accepted TPM, attempted/successful input totals, successful logical inputs/minute | Compare useful work and online endpoint consumption without counting rejected batches |
| Reliability | success rate, failed requests, HTTP 429 count/rate | Detect overdriving and partial runs |
| Latency | request p50/p95/p99 | Detect queueing and oversized requests |
| Packing | inputs/request, actual tokens/request, token/item ceiling fill, estimate/actual ratio | Explain why RPM or TPM is controlling |
| Context | request-window seconds, request/input/token totals | Prevent short bursts from being read as sustained throughput |

RPM and TPM are observed rates over the online request window from the first
request start to the last request completion. They exclude AML queue,
environment preparation, and input download time. For concurrent short jobs,
these are burst-window metrics and can exceed assigned per-minute capacity;
only repeated long-window jobs support sustained-capacity claims.

Only successful service-reported `prompt_tokens` contribute to accepted TPM.
Attempted RPM includes failed requests, while successful RPM does not. This
separation makes HTTP 429 pressure visible instead of hiding it behind a single
request-rate value.

Rate-limit header values and individual request events remain in `trace.jsonl`
rather than AML scalar metrics. They are snapshots with request-level context,
not stable run aggregates, and publishing each value would create a noisy metric
surface.
