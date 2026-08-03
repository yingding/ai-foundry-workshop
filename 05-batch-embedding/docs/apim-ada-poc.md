# APIM ADA Embedding Model proof of concept

This proof of concept uses an existing BasicV2 API Management service to route
embedding requests across two independently allocated `text-embedding-ada-002`
(ADA Embedding Model version 2) deployments.
It does not create another APIM service.

## Provisioned shape

- isolated API path: `/ada-embeddings-test`;
- primary and secondary single backends;
- equal-weight backend pool;
- circuit breaker on three HTTP 429 responses in 30 seconds;
- `Retry-After` accepted by each breaker;
- APIM system-assigned managed identity authentication to Foundry;
- API-scoped APIM client subscription;
- no Foundry keys, APIM keys, or bearer tokens in `.env` or output files.

Primary and secondary are routing roles. The accounts can be in any supported
regions, provided both expose the same deployment name, ADA model version 2,
1,536 dimensions, request contract, and independently assigned capacity.

## Implementation map

The PoC is implemented in Python with `azure-mgmt-apimanagement==6.0.0b1`.
No Bicep, ARM template, or generic `azure-mgmt-resource` deployment is used.

| Module | CLI | Responsibility |
| --- | --- | --- |
| `apim_ada_poc.py` | `apim-ada-poc` | Validate prerequisites and idempotently configure APIM backends, pool, API, policy, subscription, and Foundry RBAC |
| `apim_ada_load.py` | `apim-ada-load` | Run local synchronous smoke, RPM packing, and TPM pacing experiments |
| `apim_ada_analyze.py` | `apim-ada-analyze` | Analyze secret-free `summary.json` and `requests.jsonl` outputs offline |
| `permissions_setup.py` | reused by setup commands | Own shared role contracts and deterministic idempotent role assignment |
| `utils/embedding_optimization.py` | reused by AML and local experiments | Own compatible packing, TPM pacing, throughput normalization, and percentiles |
| `tests/test_apim_ada.py` | `python -m unittest` | Validate header mappings, retry timing, credential laziness, throttle classification, cosine handling, percentiles, and RBAC creation |

Implementation constants are grouped into immutable data contracts:

- `EnvironmentKeys` defines configuration variable names;
- `ApimPocContract` defines APIM resource IDs, model requirements, pool weights,
  breaker settings, diagnostic headers, and API defaults;
- `LoadContract` defines request timing, dimensions, header mappings, and local
  load defaults;
- `AnalysisContract`, `LogFields`, `SummaryFields`, `AnalysisMessages`, and
  `ThrottlePhrases` define the analysis input and output contract;
- `ThrottleClassification` and `RbacRole` provide typed domain values.

The shared optimization functions separate two concerns:

```text
logical inputs
  -> pack_compatible_requests        reduces HTTP requests and RPM pressure
  -> direct Foundry or APIM pool     selects available assigned TPM
  -> pacing_interval_seconds         controls release rate against target TPM
  -> tokens_per_minute               measures accepted token throughput
```

APIM does not replace RPM optimization. Requests sent through APIM remain
packed arrays. APIM contributes independently allocated backend capacity after
packing removes unnecessary request pressure.

Subscription-specific resource identifiers live only in ignored
`config/.env`. The tracked `config/.env.example` contains generic placeholders.
The provisioner requires these environment values rather than embedding a
subscription fallback in source code.

### Implemented Azure resources

`apim-ada-poc apply` currently creates or reconciles only child resources in the
existing BasicV2 APIM service:

- `ada-primary-poc` and `ada-secondary-poc` single backends;
- `ada-regional-pool-poc` with equal priority and weight;
- `ada-embeddings-poc` API and `create-embeddings` operation;
- API policy for managed identity, pool selection, API version, and diagnostic
  response headers;
- `ada-embeddings-poc-client` API-scoped subscription;
- `ada-embeddings-aml-poc` API with AML compute token validation and no
  subscription-key requirement;
- `Cognitive Services OpenAI User` assignments for the APIM system identity on
  both Foundry accounts.

It does not create or resize APIM, change model quota, or create Foundry
accounts. The separate AML provisioner adds `embedding-ada-apim-v1` alongside
the existing direct `embedding-ada-v1` deployment.

### Output and security behavior

The load runner retrieves the APIM subscription key only for gateway tests and
an Entra bearer token only for direct Foundry tests. Credentials remain in
process memory and are never written to result files. Request logs contain
status, duration, input count, normalized retry delay, rate-limit headers,
sanitized error code/message, and pool-level diagnostic context. Input text,
embedding vectors, keys, and bearer tokens are excluded.

The analyzer runs offline and requires no Azure credential. It classifies 429
evidence conservatively, reports breaker signals without claiming breaker proof,
and preserves compatibility with older output files that used `observed_tpm`.

## Find the configuration in Azure Portal

Open the existing API Management service, then use the current portal paths
below. The API policy, load-balancer weights, and circuit breakers are stored at
different scopes and do not appear in one editor.

| Configuration | Portal path | Expected value |
| --- | --- | --- |
| Frontend API | **APIs → APIs → ADA embeddings regional pool PoC** | API ID `ada-embeddings-poc`, suffix `ada-embeddings-test` |
| AML-facing API | **APIs → APIs → ADA embeddings AML regional pool PoC** | API ID `ada-embeddings-aml-poc`, suffix `ada-embeddings-aml`, subscription not required |
| AML caller policy | Select **All operations** on the AML-facing API | Validate tenant, Cognitive Services audience, and AML compute `oid` |
| API-level policy | **APIs → APIs → ADA embeddings regional pool PoC → All operations → Design → Inbound processing → `</>`** | Managed identity, `ada-regional-pool-poc`, and API-version policies |
| Operation | **APIs → APIs → ADA embeddings regional pool PoC → Create embeddings** | `POST /deployments/{deployment-name}/embeddings` |
| Backend pool | **APIs → Backends → Load balancer → ada-regional-pool-poc** | Primary and secondary members |
| Pool weights | Open `ada-regional-pool-poc` in the **Load balancer** view | Both members priority `1`, weight `50` |
| Primary breaker | **APIs → Backends → ada-primary-poc → Settings → Circuit breaker settings** | Three 429 responses in 30 seconds; one-minute trip; accept `Retry-After` |
| Secondary breaker | **APIs → Backends → ada-secondary-poc → Settings → Circuit breaker settings** | Same rule as primary |
| Client subscription | **APIs → Subscriptions → ADA embeddings PoC client** | Active, scoped to `ada-embeddings-poc` |
| APIM identity | **Security → Managed identities** | System-assigned identity enabled |

Portal labels can shift slightly, but resource IDs remain stable. Use
`uv run apim-ada-poc plan` or the Python SDK when the portal blade is ambiguous.

### Policy scope distinction

Select **All operations** to inspect the API-level policy. It contains:

```xml
<authentication-managed-identity resource="https://cognitiveservices.azure.com" />
<set-backend-service backend-id="ada-regional-pool-poc" />
<set-query-parameter name="api-version" exists-action="override">
  <value>2024-02-01</value>
</set-query-parameter>
```

Selecting **Create embeddings** opens the narrower operation scope. No explicit
operation-level policy resource is configured, so that editor can show only the
default `<base />` sections. `<base />` inherits the API-level policy above. If
available in the portal editor, **Calculate effective policy** displays the
combined inherited result.

### Endpoint construction

The client endpoint combines the APIM gateway host, API suffix, and operation
template:

```text
https://{apim-name}.azure-api.net/
  ada-embeddings-test/
  deployments/{ada-deployment-name}/embeddings
```

The API policy selects the pool. The load balancer stores weights. Each single
backend stores its own URL and circuit breaker. Therefore the 50:50 values and
breaker rules do not appear in the API policy XML.

## API key authentication options

There are two independent authentication hops. Both can involve a value called
an API key, but the keys are not interchangeable:

| Hop | Current mechanism | Alternative |
| --- | --- | --- |
| Client → APIM | APIM subscription key | OAuth/JWT policy or another APIM client-auth mechanism |
| APIM → each Foundry account | APIM managed identity plus Foundry RBAC | Separate Foundry account API key per backend |

### Call the routed endpoint with its APIM subscription key

The routed endpoint already uses an API-scoped APIM subscription key. Obtain
the key under **APIs → Subscriptions → ADA embeddings PoC client → Show keys**.
Send it only to APIM:

```bash
curl -X POST \
  "https://{apim-name}.azure-api.net/ada-embeddings-test/deployments/{ada-deployment-name}/embeddings" \
  -H "Content-Type: application/json" \
  -H "Ocp-Apim-Subscription-Key: {apim-subscription-key}" \
  -d '{"input":["text to embed"],"encoding_format":"float"}'
```

This key authorizes the client at the gateway. It is not a Foundry account key,
and it cannot call either Foundry endpoint directly. The local load runner
retrieves it through the management SDK and does not store it in `.env` or
result files.

### Alternative: APIM uses Foundry account keys

Managed identity is the enabled and preferred backend mechanism for this PoC.
If backend key authentication is required, configure it separately on each
single backend because the two Foundry accounts have different keys.

1. Retrieve one key for each Foundry account from its **Keys & Endpoint** page.
  Use Key 1/Key 2 rotation rather than copying a key into source control.
2. In APIM, open **Named values** and create two secret named values, for
  example `ada-primary-api-key` and `ada-secondary-api-key`. Prefer Azure Key
  Vault references so APIM does not become the system of record for the
  secrets.
3. Open **APIs → Backends → ada-primary-poc → Authorization credentials**.
  Configure request header `api-key` with the primary secret named value.
4. Repeat on `ada-secondary-poc` with the secondary secret named value.
5. Under **APIs → APIs → ADA embeddings regional pool PoC → All operations →
  Design → Inbound processing → `</>`**, remove:

  ```xml
  <authentication-managed-identity
     resource="https://cognitiveservices.azure.com" />
  ```

6. Keep `set-backend-service`, `api-version`, the pool weights, and the circuit
  breakers unchanged.
7. Run direct backend smoke tests first, then the gateway smoke test. Verify
  both backends independently before a load test.

The resulting API-level policy is conceptually:

```xml
<inbound>
  <base />
  <set-backend-service backend-id="ada-regional-pool-poc" />
  <set-query-parameter name="api-version" exists-action="override">
   <value>2024-02-01</value>
  </set-query-parameter>
</inbound>
```

The `api-key` header belongs on each single backend, not as one global policy
header. A global key would authenticate only to the account that issued it and
would fail whenever the pool selected the other account. Never forward a
Foundry key from the client or return it in APIM response headers, traces, or
load-test output.

When key authentication is enabled, APIM no longer needs `Cognitive Services
OpenAI User` for inference on those accounts. Removing the now-unused role
assignments is optional cleanup after key-based smoke tests pass. Keeping the
APIM subscription key on the client-facing API remains independent of this
backend change.

This alternative is documentation only; `apim_ada_poc.py` currently implements
managed identity. Re-running `uv run apim-ada-poc apply` restores the managed
identity policy and the code-defined backend contract. Implement an explicit
authentication mode in the provisioner before managing the key alternative as
code.

Microsoft references:

- [Authenticate AI APIs through APIM with an API key](https://learn.microsoft.com/azure/api-management/api-management-authenticate-authorize-ai-apis#authenticate-by-using-api-key)
- [APIM named values and Key Vault references](https://learn.microsoft.com/azure/api-management/api-management-howto-properties)
- [Azure OpenAI REST authentication](https://learn.microsoft.com/azure/ai-foundry/openai/reference#authentication)
- [Azure OpenAI keys and endpoint](https://learn.microsoft.com/azure/ai-foundry/openai/use-your-data-quickstart#retrieve-resource-information)

## Circuit breaker configuration and behavior

The circuit breaker is configured on each individual APIM backend resource. It
is not an XML API policy. The provisioner applies this equivalent backend
contract to both the primary and secondary backends:

```python
{
    "circuitBreaker": {
        "rules": [
            {
                "name": "ada-rate-limit",
                "failureCondition": {
                    "count": 3,
                    "interval": "PT30S",
                    "statusCodeRanges": [{"min": 429, "max": 429}],
                },
                "tripDuration": "PT1M",
                "acceptRetryAfter": True,
            }
        ]
    }
}
```

The applied state transition is:

```text
Closed
  | three observed HTTP 429 responses in 30 seconds
  v
Open
  | APIM stops selecting that backend on the gateway instance
  | wait for accepted Retry-After, or the one-minute fallback
  v
Closed and eligible again
```

Microsoft documents that an open circuit stops requests to that backend for the
trip duration and that APIM can return HTTP 503 while the backend is
unavailable. In this load-balanced pool, subsequent requests can use another
eligible member. If no member is eligible, the gateway returns 503.

The literal `ada` and `ada-apim` selectors intentionally share the Foundry deployment name
`text-embedding-ada-002-test`; the deployment name identifies the model path,
not the transport. `ada` sends the ADA Embedding Model request to the direct
Foundry base URL. `ada-apim` sends it to the
AML-facing APIM base URL plus `/deployments/text-embedding-ada-002-test` and the
Cognitive Services token scope. The CLI plan now prints the route and full
endpoint so this distinction is visible before invocation.

Live child run `752ce3eb-12c4-4759-bc44-ff061af96f66` verified both paths of
evidence:

- execution logs recorded the APIM endpoint ending in
  `/ada-embeddings-aml/deployments/text-embedding-ada-002-test`;
- AML persisted 21 `ada_apim_batch.*` MLflow metrics on the child run;
- one packed request processed 100 inputs and 1,030 prompt tokens with HTTP 200;
- the burst-window report showed 24.850 attempted/successful RPM, 25,595.218
  accepted TPM, 85.833% token-ceiling fill, and zero throttling.

These rates describe a 2.415-second single-request burst and are not a sustained
capacity result. Use longer paced jobs for quota conclusions.
The threshold is based on responses observed by APIM, not the backend's current
TPM counter. One or two 429 responses do not open this configured circuit. The
third qualifying response within the 30-second interval can trip it.

`acceptRetryAfter: true` tells the breaker to accept the backend's
`Retry-After` duration before making the backend eligible again. The configured
`PT1M` trip duration is the fallback when a usable `Retry-After` value is not
applied. Azure OpenAI can return a long `Retry-After`, so the actual open period
can be longer than one minute.

Circuit state and load balancing are approximate because managed APIM is
distributed. Gateway instances do not synchronize their breaker counters or
balancing state. A few requests can therefore still observe different state
during a transition, and a short run should not be expected to show an exact
50:50 split.

### What happens to the triggering request

The circuit breaker protects future routing decisions. It does not replay the
request that already received HTTP 429. The current PoC backend policy contains
one `forward-request` and no `retry` wrapper:

```xml
<backend>
  <forward-request buffer-request-body="true" />
</backend>
```

The caller therefore receives that 429 and remains responsible for bounded,
idempotent retry with backoff. If automatic same-request failover is required,
it must be designed explicitly with APIM's `retry` policy or in the client. The
Microsoft retry-policy example can switch from a primary to a secondary backend
after a 429, but that is a different behavior from the backend-pool circuit
breaker and is not enabled in this PoC.

This separation is intentional for the capacity experiment. Automatic APIM
replay would hide the backend boundary, change request counts, and make 429 and
latency measurements harder to interpret. Retry behavior should be tested in a
separate resilience run after the no-retry capacity boundary is understood.

### API policy interaction

The API policy performs authentication and selects the pool:

```xml
<inbound>
  <base />
  <authentication-managed-identity
      resource="https://cognitiveservices.azure.com" />
  <set-backend-service backend-id="ada-regional-pool-poc" />
  <set-query-parameter name="api-version" exists-action="override">
    <value>2024-02-01</value>
  </set-query-parameter>
</inbound>
```

The pool gives both backends priority `1` and weight `50`. APIM chooses among
eligible members approximately according to those weights. The breaker attached
to each single backend determines whether that member remains eligible. The
managed-identity policy obtains an Entra token for whichever backend is chosen;
it does not control balancing or breaker state.

### Microsoft references

- [APIM backend circuit breakers](https://learn.microsoft.com/azure/api-management/backends#circuit-breaker)
  defines trip conditions, HTTP 503 behavior, reset behavior, distributed-state
  limitations, and the Azure OpenAI `Retry-After` recommendation.
- [APIM load-balanced backend pools](https://learn.microsoft.com/azure/api-management/backends#load-balanced-pool)
  defines weighted and priority routing and notes that balancing is approximate
  across gateway instances.
- [APIM retry policy](https://learn.microsoft.com/azure/api-management/retry-policy)
  defines explicit request replay and includes a separate example that switches
  backend after HTTP 429.
- [APIM managed identity authentication policy](https://learn.microsoft.com/azure/api-management/authentication-managed-identity-policy)
  defines backend token acquisition used by this API policy.

## Experiment purpose

The experiments answer four separate questions. They must not be collapsed into
one pass/fail result because each question needs different evidence.

| Experiment | Question | Why it matters |
| --- | --- | --- |
| Compatibility smoke | Can either deployment serve vectors for one shared index? | Aggregate throughput is unusable if vectors, dimensions, or indexes differ |
| Direct controls | Where are the capacity and latency boundaries of each backend? | A gateway result has no meaning without single-backend controls |
| Gateway capacity | Does independently allocated capacity increase sustained useful throughput? | APIM routing alone does not create quota |
| Breaker and recovery | Does APIM stop routing to a throttled backend and restore it later? | Aggregate capacity must degrade predictably instead of amplifying failures |

The primary hypothesis is:

$$
\mathrm{TPM}_{gateway}>\max(\mathrm{TPM}_{primary},\mathrm{TPM}_{secondary})
$$

when both backends have independent quota and the gateway distributes enough
traffic to each. The upper bound remains:

$$
\mathrm{TPM}_{gateway}\leq
\mathrm{TPM}_{primary}+\mathrm{TPM}_{secondary}
$$

For two confirmed 15,000-TPM deployments, the current workshop plan is 18,000
TPM, or 60% of assigned capacity. The measured 24,000-27,000 TPM runs are
historical boundary evidence, and the unbuffered sum of 30,000 TPM is not the
recommended operating point.

## Commands

The `apim-ada-load` command is a local synchronous HTTP test client. It calls
the primary Foundry endpoint, secondary Foundry endpoint, or APIM gateway
directly from the development environment. It does **not** invoke the Azure
Machine Learning batch endpoint, submit an AML job, or use the AML deployment
named by `AML_ADA_DEPLOYMENT_NAME`, such as `embedding-ada-v1`.

This separation isolates the online data path before AML integration. A failed
smoke or load test can therefore be attributed to the local client, APIM, RBAC,
routing, or Foundry rather than AML scheduling, compute startup, input assets,
or batch output handling.

Inspect live prerequisites without mutation:

```bash
uv run apim-ada-poc plan
```

Apply or reconcile the isolated APIM children and RBAC assignments:

```bash
uv run apim-ada-poc apply
```

Run the focused local contract tests:

```bash
cd 05-batch-embedding
uv run python -m unittest discover -s tests -v
```

Run the compatibility smoke test:

```bash
uv run apim-ada-load smoke \
  --inputs 8 \
  --output outputs/apim-ada-smoke
```

The runner sends identical inputs to the primary account, secondary account,
and gateway. It verifies complete indexes, 1,536-dimensional vectors, and
reports cosine similarity and maximum absolute difference without persisting
the input text or vectors. Its input array exercises the Foundry embeddings
request contract, but it is not an AML batch-endpoint invocation.

Run an RPM packing step:

```bash
uv run apim-ada-load rpm \
  --target gateway \
  --inputs 100 \
  --batch-size 100 \
  --output outputs/apim-ada-rpm-gateway
```

Run a TPM-paced load step:

```bash
uv run apim-ada-load tpm \
  --target gateway \
  --batch-size 100 \
  --duration-seconds 180 \
  --target-tpm 24000 \
  --output outputs/apim-ada-load-gateway-24k
```

`load` remains an alias for `tpm` so existing commands and result history remain
valid.

Analyze one or more smoke, RPM, and TPM output directories offline:

```bash
uv run apim-ada-analyze \
  outputs/apim-ada-smoke-attribution \
  outputs/apim-ada-load-primary \
  outputs/apim-ada-load-gateway \
  --output outputs/apim-ada-analysis
```

The analyzer reads only `summary.json` and `requests.jsonl`. It produces
`analysis.json` and `analysis.md` containing status counts, latency, pacing,
retry guidance, breaker signals, rate-limit classification, and available
backend context. It requires no Azure credentials and does not read or emit
subscription keys, bearer tokens, input text, or vectors.

Use the same batch size, duration, and generated input distribution for direct
and gateway comparisons. Run direct controls separately so their quota windows
do not contaminate the gateway test.

The integration stage is implemented as a separate `ada-apim` selector and AML
deployment named `embedding-ada-apim-v1`. It does not replace
`embedding-ada-v1`. The AML-facing API validates the compute managed identity,
then APIM re-establishes its own managed identity to Foundry.

Provision and invoke it with:

```bash
uv run aml-batch-embeddings provision-apim
uv run aml-batch-embeddings invoke \
  --model ada-apim \
  --input data/workshop-rpm \
  --packing batch \
  --max-inputs-per-request 100 \
  --max-retries 0 \
  --request-concurrency 1 \
  --repeat-inputs 2
```

## Staged experiment

1. Run `smoke` and require successful primary, secondary, and gateway responses.
2. Run primary and secondary controls at conservative rates.
3. Run the gateway at 12,000 and 15,000 TPM to establish APIM overhead.
4. Run the gateway at 18,000, 24,000, and 27,000 TPM over clean windows.
5. Attempt 30,000 TPM only as a boundary test, not a steady production target.
6. Run each selected point at least three times.
7. Test breaker behavior separately by throttling one backend deliberately.

Use two-to-five-minute windows for capacity conclusions. Short probes validate
the code path but do not establish sustained TPM.

## Controls and isolation

Keep these variables identical when comparing direct and gateway runs:

- generated text distribution and logical input count;
- batch size and encoding format;
- deployment name, model, and API version;
- configured target TPM and run duration;
- retry behavior and request timeout;
- load-generator location and client concurrency.

Run each direct control and gateway step in separate, clean quota windows. A
prior direct burst can leave one backend throttled and make the following
gateway test look weaker than it is. Conversely, retrying automatically can hide
the actual capacity boundary. Disable retries while locating the boundary, then
enable bounded retries in a separate resilience experiment.

The direct controls are not alternatives to APIM. They are the counterfactual:
they show what the same workload achieves without pooling. The gateway result
must exceed that control before aggregate-capacity success can be claimed.

## Metrics

`summary.json` contains request counts, statuses, logical inputs, prompt tokens,
and latency percentiles. `requests.jsonl` contains one secret-free metric record
per HTTP request.

`window_tpm` divides all accepted tokens by total test duration. It includes the
first batch sent at time zero and can exceed the configured rate in short runs.
`steady_state_tpm` excludes that initial batch and measures tokens over the
subsequent paced interval. Use repeated long-window results and Azure Monitor
telemetry for final capacity claims.

Interpret the remaining metrics as follows:

| Metric | Interpretation |
| --- | --- |
| `logical_inputs` | Useful work completed; compare only with the same input distribution |
| `prompt_tokens` | Accepted model work reported by successful responses |
| HTTP 429 | Backend admission pressure; distinguish it from gateway 503 |
| HTTP 503 | APIM could not select an available backend, including open-breaker cases |
| `retry_after` | Backend recovery guidance; do not treat it as latency |
| p50 latency | Typical request cost |
| p95/p99 latency | Tail cost from regional distance, queueing, or throttling |
| backend share | Evidence that both independent allocations are actually used |

### Token-size optimization report

Every new RPM or TPM summary contains an `optimization_plan` with:

- primary, secondary, and target-assigned TPM;
- whether capacity came from Azure metadata or an environment override;
- utilization target and target TPM;
- planned requests/minute;
- maximum inputs and tokens per request;
- tokenizer model.

Every request record contains `input_count`, `estimated_tokens`, actual
`prompt_tokens`, status, latency, retry guidance, and available rate-limit
headers. The analyzer adds:

| Report value | Interpretation |
| --- | --- |
| Inputs/request min, mean, p50, p95, max | Whether item count or token size controls packing |
| Estimated/actual token distributions | Batch shape and service-accounted work |
| Estimate-to-actual ratio | Tokenizer accuracy; values near 1 are desirable |
| Item-capacity fill | Mean items divided by configured item ceiling |
| Token-capacity fill | Mean tokens divided by configured token ceiling |
| Logical inputs/HTTP request | Useful RPM reduction |
| Steady-state capacity utilization | Observed TPM divided by assigned TPM |

The verified automatic report discovered 15K + 15K from Azure, derived a 24K
target and 1,200-token ceiling, and split 100 inputs into batches of 1,190 and
210 tokens. Estimates matched actual prompt tokens exactly. The resulting two
requests represented a 98% request reduction. A low mean fill ratio can be
caused by the final remainder batch; inspect p95/max and run larger datasets
before changing the ceiling.

The token-aware AML validation over 200 inputs also produced two requests with
estimated/actual token sizes of 860 and 1,200. Both returned HTTP 200 with zero
failures. This confirms the same tokenizer and ceiling are used in the deployed
AML component, not only in the local APIM experiment.

### AML Studio child-job metrics

MLflow metrics belong to the embedding command child job, not the parent
pipeline. In AML Studio, open the invoked pipeline job, select the embedding
step, and open **Metrics**. With the default prefix, the run exposes:

- `embedding_batch.attempted_rpm`, `successful_rpm`, and `accepted_tpm`;
- `logical_inputs_per_minute` and request/input/token totals;
- `success_rate`, `throttled_requests`, and `throttle_rate`;
- request latency p50/p95/p99;
- inputs/request and prompt tokens/successful request;
- token/item ceiling fill and estimated/actual token ratio;
- request-window duration.

Azure ML automatically starts the MLflow run for job code. The component calls
`mlflow.log_metrics()` directly and intentionally does not call
`mlflow.start_run()`, which could create a separate nested run. The environment
includes both `mlflow` and the `azureml-mlflow` workspace integration package.
This follows Microsoft's [MLflow and Azure Machine Learning](https://learn.microsoft.com/azure/machine-learning/concept-mlflow?view=azureml-api-2)
and [metric logging](https://learn.microsoft.com/azure/machine-learning/how-to-log-view-metrics?view=azureml-api-2)
guidance for Azure ML SDK v2 jobs.

MLflow publishing defaults to enabled for AML invocations:

```bash
uv run aml-batch-embeddings invoke \
  --model ada-apim \
  --metric-logging mlflow \
  --metric-prefix embedding_batch
```

Disable only the AML Metrics-tab publication while retaining `trace.jsonl`:

```bash
uv run aml-batch-embeddings invoke \
  --model ada-apim \
  --metric-logging disabled
```

Use a different prefix to compare named workload families without changing the
metric schema. Prefixes must start with a letter and may contain letters,
digits, dots, dashes, and underscores.

### Interpreting HTTP 429: RPM or TPM

Both request-rate and token-rate enforcement return HTTP 429. The status code
and `Retry-After` value do not identify which limiter fired. Use this evidence
order:

1. Treat explicit service wording such as `call rate` or `request rate` as
  `rpm-explicit`, and explicit `token rate` wording as `tpm-explicit`.
2. When wording is absent, an exhausted request counter with token headroom is
  `rpm-likely`.
3. An exhausted token counter with request headroom is `tpm-likely`.
4. If both counters are exhausted, both are missing, or the evidence conflicts,
  classify the event as `unknown`.

Only the explicit classifications are proof. The `likely` classifications are
diagnostic inferences and must remain labeled as such.

The live ADA smoke responses exposed `x-ratelimit-limit-tokens: 15000` and
remaining-token values through both direct and APIM paths. They did not expose
request-limit headers. This confirms the deployment token allocation but means
an otherwise unexplained 429 cannot be assigned to RPM or TPM from headers
alone. The analyzer records the available headers and applies the conservative
classification above.

A low 429 rate at low utilization proves only that the path works. A high
gateway TPM without backend attribution is incomplete evidence because all
traffic might still be reaching one backend. Similarly, a near-50:50 request
share does not prove equal token share when batches have different token sizes.
Compare both requests and prompt tokens per backend when telemetry permits.

The PoC policy returns documented `context.Backend` fields in diagnostic
response headers. The live probe reported backend ID `ada-regional-pool-poc`,
type `Pool`, and region `n/a`. APIM therefore exposes the configured pool at
this policy point, not the selected member. Use gateway diagnostics or Azure
Monitor backend URL telemetry for authoritative member attribution. Do not infer
backend selection from vector equality or latency.

## Initial verified result

The two-input smoke test completed successfully through all three targets:

| Target | Status | Prompt tokens | Dimensions | Correlated inputs |
| --- | ---: | ---: | ---: | ---: |
| Primary | 200 | 28 | 1,536 | 2 |
| Secondary | 200 | 28 | 1,536 | 2 |
| Gateway | 200 | 28 | 1,536 | 2 |

Primary-to-secondary minimum cosine similarity was `0.9999398402`, with maximum
absolute difference `0.001320787`. The sampled gateway response matched the
primary response exactly. This proves compatibility for the sample, not that
APIM always selected the primary backend.

Matched low-rate primary and gateway probes each completed three requests and
60 logical inputs with no HTTP 429 or 503 responses. These 12-second probes
validate pacing and reporting only; they are too short for a capacity claim.

The sustained three-minute direct control and gateway runs produced:

| Target | Configured TPM | Steady-state TPM | Success | Logical inputs | 429 | 503 | p95 latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Primary direct | 15,000 | 14,234.792 | 30/33 | 3,000 | 3 | 0 | 2,689.292 ms |
| APIM pool | 24,000 | 23,988.846 | 52/52 | 5,200 | 0 | 0 | 1,821.026 ms |
| APIM pool | 27,000 | 25,536.802 | 55/55 | 5,500 | 0 | 0 | 1,884.721 ms |

Compared with the direct control, the 24K pool run added 9,754.054 TPM, or
68.523%, and the 27K pool run added 11,302.010 TPM, or 79.397%. The three direct
429 responses explicitly identified call/request-rate enforcement, so the
analyzer classified them as `rpm-explicit`. Neither gateway run returned 429 or
503.

The shared RPM experiment packed 100 logical inputs into one successful gateway
request, a 99% request reduction. This composes with the TPM result because the
gateway capacity runs also used 100-input arrays rather than one input per
request.

A post-refactor low-rate `tpm` probe also completed three of three requests with
no 429 or 503, confirming the shared pacing and throughput functions execute in
the live APIM path. Its 12-second rate is not capacity evidence because the
first request and response time materially affect such a short window.

The 27K run was client/latency limited below its configured target but still
landed inside the intended 24K-27K operating range. Its p99 latency reached
7,466.052 ms, so tail behavior needs repetition before choosing a production
target.

The initial parallel AML integration job `pipelinejob-44603118-1f71-4412-a16c-92919508dbaa`
processed 200 logical inputs in two packed APIM requests. Both returned HTTP
200, no request failed, and the embedding loop completed in 5,888.215 ms. The
original direct ADA deployment remains available and the endpoint default
remains `embedding-small-v1`.

Downloaded output validation confirmed two response records, exactly 200 unique
`input_id` values, 1,536 finite values in every vector, and zero error records.

After extracting the shared utility, `pipelinejob-d139de7b-4ae3-490f-b757-5fd85eba1259`
revalidated the deployed implementation: 200 inputs became two packed requests,
both HTTP 200, with zero failures and a 4,275.229 ms embedding-loop duration.

### Current evidence status

| Claim | Status | Evidence still needed |
| --- | --- | --- |
| Same model contract and dimensions | Verified | Repeat if either deployment changes |
| Compatible vectors for shared indexing | Verified for the smoke sample | Larger representative sample and an agreed similarity tolerance |
| APIM managed-identity data path works | Verified | None for path validation |
| Load pacing and secret-free metrics work | Verified at low rate | Long-window repetitions |
| Both backends receive gateway traffic | Pending | APIM diagnostics or Azure Monitor backend URL telemetry over a long run |
| Pool exceeds measured direct throughput | Verified: +68.523% and +79.397% | Backend-member attribution still required |
| 18,000 TPM workshop operating point | Verified in the safe AML A/B with zero throttling | Repeat before production selection |
| 24,000-27,000 TPM boundary range | Verified once at 24K and once at 27K | Treat as historical boundary evidence and repeat before use |
| Circuit breaker removes and restores a backend | Pending | Isolated overload and recovery experiment |
| Parallel AML deployment works through APIM | Verified | Repeat with representative production input distribution |

The verified rows establish prerequisites. They do not yet establish aggregate
capacity. The PoC should remain experimental until the pending rows pass.

## Interpretation outcomes

| Observation | Meaning | Next action |
| --- | --- | --- |
| Gateway stays near one-backend throughput and traffic uses one backend | Pool is not distributing usable capacity | Inspect pool health, breaker state, and backend attribution |
| Both backends receive traffic but gateway stays near 15,000 TPM | Client pacing, APIM throughput, or one quota pool is limiting | Compare direct controls, APIM latency, and confirmed allocations |
| Gateway reaches 24,000-27,000 TPM with low 429 | Aggregate capacity hypothesis is supported | Select a headroom target and repeat under representative data |
| Gateway approaches 30,000 TPM but 429 or p99 rises sharply | Hard boundary found without safe operating margin | Reduce target to the highest stable repeated point |
| 503 appears after backend 429 | Circuit breaker opened and no eligible backend was available | Check breaker threshold, pool health, and client retry handling |
| Vector similarity or dimensions differ | Backends are not interchangeable for one index | Stop capacity testing and align deployment model/version |

Results should be interpreted as workload-specific. Tokens per input, batch
size, network location, and APIM gateway load all affect the useful operating
point even when assigned TPM remains unchanged.

## Acceptance criteria

- every logical input returns exactly one response index;
- all vectors contain 1,536 finite values;
- both backends receive traffic over a sufficiently long run;
- sustained gateway throughput exceeds a single 15,000-TPM backend;
- selected steady state reaches the current 18,000-TPM workshop plan without throttling;
- HTTP 429 remains below 1%;
- breaker tests remove and restore a throttled backend;
- p50, p95, and p99 latency are reported per target.

APIM balancing and circuit-breaker state are distributed and approximate. Do
not require an exact 50:50 split in every short window.