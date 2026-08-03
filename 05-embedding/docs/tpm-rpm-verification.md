# Verify embedding TPM and RPM

This document explains how to derive and verify the rate limits for the three
Standard embedding deployments used by this project.

See [RPM optimization plan](rpm-optimization.md) for the follow-up design based
on these measurements.
See [TPM optimization plan](tpm-optimization.md) for increasing deployment and
aggregate token capacity after batching and pacing are optimized.

## Current quota and deployment metadata

Verified with Azure CLI on 2026-07-31:

| Model | Deployment capacity | Deployment TPM | RPM basis |
| --- | ---: | ---: | --- |
| `text-embedding-3-small` | 15 | 15,000 | Tier table lists 1,000 RPM |
| `text-embedding-3-large` | 15 | 15,000 | Tier table lists 1,000 RPM |
| `text-embedding-ada-002` | 15 | 15,000 | Approximately 900 RPM inferred from live enforcement |

The embedding-3 Tier 1 table column is Requests Per Minute. Its `1000 / 10s` notation means
1,000 RPM evaluated over a 10-second interval, not 1,000 requests allowed in
every 10-second interval. ADA is not present in the current public ratio table,
so its RPM must not be copied from the embedding-3 tier. The live ADA test is
consistent with an approximately 900-request one-minute bucket.

## Get assigned capacity with Azure CLI

Load the project configuration without printing its values, then query the three
deployments:

```bash
cd 05-embedding
set -a
source config/.env
set +a

az cognitiveservices account deployment list \
  --resource-group "$FOUNDRY_RESOURCE_GROUP" \
  --name "$FOUNDRY_ACCOUNT_NAME" \
  --query "[?name=='text-embedding-3-small' || name=='text-embedding-3-large' || name=='text-embedding-ada-002-test'].[name,sku.name,sku.capacity]" \
  --output table
```

Expected capacity for all three deployments is `15`. The deployment capacity is an
allocation, not observed token consumption.

## Convert capacity to TPM

Microsoft Learn states:

> "By setting Sku Capacity to 10 ... this deployment is set to a 10K TPM limit."

Source: [Automate Azure OpenAI deployments with quota](https://learn.microsoft.com/azure/foundry/openai/how-to/automate-quota-deployments#create-a-deployment-and-query-usage)

Therefore:

$$
\text{TPM} = \text{SKU capacity} \times 1{,}000
$$

For capacity 15:

$$
15 \times 1{,}000 = 15{,}000\ \text{TPM}
$$

Microsoft also states:

> "When a deployment is created, the assigned TPM directly maps to the tokens-per-minute rate limit enforced on its inferencing requests."

Source: [Manage Azure OpenAI quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#introduction-to-quota)

## Where RPM and TPM dependency comes from

The dependency exists at **quota allocation time**, not because request tokens
are converted into requests at runtime. Microsoft Learn states:

> "A Requests-Per-Minute (RPM) rate limit is also enforced, whose value is set proportionally to the TPM assignment using the following ratio."

It also states:

> "You don't have granular control over TPM and RPM as independent values. Quota is allocated in terms of units of capacity, which have corresponding amounts of RPM & TPM."

Source: [Manage Azure OpenAI quota](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#introduction-to-quota)

Therefore a deployment capacity choice allocates a predefined pair:

$$
\mathrm{capacity\ units}\longrightarrow(\mathrm{TPM\ allocation},\ \mathrm{RPM\ allocation})
$$

The pair is model-specific. Do not derive one model's RPM from another model's
TPM-to-RPM ratio.

At **inference time**, Azure evaluates two different measurements:

- request rate: requests observed during a short evaluation window;
- token rate: estimated processed tokens accumulated against the TPM window.

There is no runtime formula such as $\text{requests}=\text{tokens}/k$. A small
request can hit the request-rate limit while using few tokens, and one large
request can hit the token-rate limit while using one request slot.

## Calculate the short-window request limit

The Microsoft quota-tier table lists both `text-embedding-3-small` and
`text-embedding-3-large` Global Standard at:

- 1,000,000 TPM
- 1,000 RPM, evaluated over 10 seconds

Source: [Azure OpenAI quotas and limits](https://learn.microsoft.com/azure/foundry/openai/quotas-limits#quota-tiers)

Microsoft Learn says Azure evaluates incoming request rate over a small period,
typically 1 or 10 seconds. The expected request allowance for an evaluation
window is:

$$
L_w = \text{RPM} \times \frac{w}{60}
$$

For Tier 1 embeddings with $\text{RPM}=1{,}000$ and $w=10$ seconds:

$$
L_{10s} = 1{,}000 \times \frac{10}{60} = 166.67
$$

Because request counts are integral, approximately 166 requests in a 10-second
window is a conservative steady-rate pacing budget. Microsoft describes the
window as typically 1 or 10 seconds but does not publish the exact rolling
algorithm or burst allowance. A request can receive HTTP 429 when the
service-observed rate exceeds its effective allowance:

$$
\mathrm{429}_{RPM}\quad\mathrm{when}\quad R_w > L_w
$$

In one measured burst, the client started 391 requests in the busiest rolling
10 seconds and Foundry returned HTTP 429 with error code `RateLimitReached`.
However, a separate test started 200 requests within 2.226 seconds and all 200
succeeded. Therefore $L_{10s}=166.67$ is an expected pacing calculation, not an
exact observable cutoff.

TPM is enforced independently. The token limiter can be represented as:

$$
\mathrm{429}_{TPM}\quad\mathrm{when}\quad T_{60s} > \mathrm{assigned\ TPM}
$$

## ADA RPM analysis

The subscription has 2,593,000 TPM available for ADA in the region. This is the
regional model quota pool, not the deployment limit. The ADA deployment has
15,000 TPM assigned, represented by ARM SKU capacity 15.

Microsoft documents that assigned TPM sets a deployment's TPM and proportional
RPM pair, but its current public capacity-ratio table does not include
`text-embedding-ada-002`. The live 2026-07-31 test provides the following
evidence:

- 400 one-input-per-request calls succeeded with all starts inside 9.588 seconds.
- Of 1,200 one-input-per-request calls, 870 succeeded and 330 returned HTTP 429.
- All submission sequences 0-799 succeeded; throttling began at sequence 814;
  no sequence 1000 or greater succeeded.
- Successful responses reported 9,060 prompt tokens, below the 15,000 TPM
  allocation.
- The endpoint error explicitly identified the call-rate limit.
- The server returned `Retry-After` values from 26 through 32 seconds. Response
  time plus retry delay converged on one reset around 61-62 seconds after the
  run began.

This distribution is consistent with approximately 60 RPM per 1,000 assigned
TPM for ADA:

$$
\mathrm{inferred\ ADA\ RPM}
= \frac{15{,}000\ \mathrm{TPM}}{1{,}000}
  \cdot 60\ \frac{\mathrm{RPM}}{1{,}000\ \mathrm{TPM}}
\approx 900\ \mathrm{RPM}
$$

This is an inference, not a published ADA ratio. The 870 successful responses
do not establish an 870-RPM limit because 100 calls were concurrently in flight
at the boundary and Microsoft states unsuccessful requests can count against
the rate limit. Use `x-ratelimit-limit-requests` from a successful response as
the authoritative value when the service returns it.

After adding raw response-header capture, a low-rate packed ADA probe completed
two HTTP requests successfully. Both responses reported
`x-ratelimit-limit-tokens: 15000`; the observed remaining-token values were
12,774 and 11,550. Neither response included `x-ratelimit-limit-requests`,
`x-ratelimit-remaining-requests`, or `x-ratelimit-reset-requests`. The data plane
therefore confirms the 15,000 TPM limit but does not expose ADA's RPM in these
responses. Approximately 900 RPM remains the best estimate from the measured
one-minute call-rate boundary.

### Azure formulas compared with ADA measurements

The Azure documentation provides an exact TPM allocation formula and a generic,
model-specific RPM relationship:

$$
\mathrm{assigned\ TPM}=\mathrm{SKU\ capacity}\cdot1{,}000
$$

$$
\mathrm{assigned\ RPM}
=\frac{\mathrm{assigned\ TPM}}{1{,}000}\cdot r_{model}
$$

Here, $r_{model}$ is the RPM supplied by one 1,000-TPM capacity unit. Microsoft
does not currently publish $r_{model}$ for `text-embedding-ada-002`. Azure also
documents a conservative short-window pacing conversion:

$$
L_w=\mathrm{assigned\ RPM}\cdot\frac{w}{60}
$$

The following table separates documented values, derived expectations, and
measured behavior:

| Item | Azure/control-plane value | Derived expectation | ADA measurement | Discrepancy |
| --- | ---: | ---: | ---: | --- |
| Regional ADA quota pool | 2,593,000 TPM | Not a deployment limit | Not load-tested | No direct comparison |
| Deployment TPM | Capacity 15 | 15,000 TPM | Header reported 15,000 TPM | No discrepancy |
| ARM request metadata | 15 requests / 10 seconds | 90 RPM if interpreted literally | 400 starts in 9.588 seconds, all HTTP 200 | Literal interpretation is disproved by at least $400/15=26.67\times$ |
| Inferred ADA RPM | ADA ratio unpublished | Approximately 900 RPM using measured $r_{model}\approx60$ | 870 successes; throttling began near sequence 814 | Boundary is consistent but not header-confirmed |
| 10-second pacing at 900 RPM | Generic Azure pacing formula | $900\cdot10/60=150$ requests | 400 successful starts in 9.588 seconds | Observed burst was $400/150=2.67\times$ the pacing value |
| TPM at throttle | 15,000 TPM | 429 if estimated token counter reaches allocation | 9,060 actual prompt tokens across successes | Actual billed tokens were only 60.4% of TPM; error identified call rate |
| Reset behavior | Exact ADA algorithm unpublished | Could be evaluated over short intervals | All 429 retry clocks converged near 61-62 seconds | Evidence favors a minute-scale bucket with burst tolerance |

The central discrepancy is therefore not the TPM calculation; capacity 15 maps
cleanly to 15,000 TPM and the response header confirms it. The discrepancy is
between request-rate metadata or conservative pacing formulas and actual ADA
admission behavior. Neither 15 requests per 10 seconds nor 150 requests per 10
seconds behaved as a hard cutoff. The measured run instead supports an
approximately 900-request minute allowance with substantial short-term burst
tolerance. Because Azure omits ADA's request-limit headers and does not publish
its capacity ratio, this remains an empirical model rather than a service
contract.

In short: RPM and TPM are **paired for allocation** but **separately measured
for enforcement**. Either limiter can produce HTTP 429. The error body does not
always identify which counter fired.

### Classify 429 evidence conservatively

Use `rpm-explicit` or `tpm-explicit` only when the service response text names
the limiter. When explicit wording is absent, use `rpm-likely` only when the
request counter is exhausted while token headroom remains, and `tpm-likely`
only when the token counter is exhausted while request headroom remains. Use
`unknown` when counters are absent, both are exhausted, or evidence conflicts.

`Retry-After` indicates when to retry; it does not identify RPM or TPM. APIM's
circuit breaker also reacts to the HTTP 429 status and does not distinguish the
underlying limiter.

## Check subscription quota allocation

The following command shows total quota allocated across the active
subscription in the account region. `currentValue` is allocated quota, not
current traffic:

```bash
LOCATION=$(az cognitiveservices account show \
  --resource-group "$FOUNDRY_RESOURCE_GROUP" \
  --name "$FOUNDRY_ACCOUNT_NAME" \
  --query location --output tsv)

az cognitiveservices usage list \
  --location "$LOCATION" \
  --query "[?contains(name.value, 'text-embedding-3-small') || contains(name.value, 'text-embedding-3-large') || contains(name.value, 'text-embedding-ada-002')].{quota:name.value,current:currentValue,limit:limit,unit:unit}" \
  --output table
```

This regional result can include embedding deployments in other Cognitive
Services accounts in the same subscription. Use the deployment-list command to
find this project's assigned limit.

## Runtime verification

The deployment control plane currently advertises these limits for the three
embedding deployments:

- `request`: count 15, renewal period 10 seconds
- `token`: count 15,000, renewal period 60 seconds

For `text-embedding-3-small`, a live 2026-07-31 data-plane test completed 100 one-input-per-request calls in
3.275 seconds with 20 workers, SDK retries disabled, and zero 429 responses.
The subscription was assigned Tier 1, whose Global Standard embedding row lists
1,000 RPM with 10-second evaluation and 1,000,000 TPM. Treat the deployment
`rateLimits` values as advertised allocation metadata, not proof of the
effective request ceiling. Verify enforcement empirically and retain 429
handling in production.

### Read response headers

A minimal data-plane request can inspect token-limit headers without printing
an embedding. Depending on service behavior, embedding responses might omit
request-limit headers.

```python
response = client.embeddings.with_raw_response.create(
    model=deployment,
    input="rate limit probe",
)
response.parse()
print(response.headers.get("x-ratelimit-limit-tokens"))
print(response.headers.get("x-ratelimit-remaining-tokens"))
print(response.headers.get("x-ratelimit-limit-requests"))
print(response.headers.get("x-ratelimit-remaining-requests"))
```

Treat the Azure CLI deployment capacity as the control-plane source of truth.
Header values can reflect a rolling window, temporary capacity adjustments, or
recent quota changes that have not fully propagated. Microsoft says quota
allocation changes can take up to 15 minutes to propagate.

### Test request throttling safely

Do not send an uncontrolled burst to a production deployment. To empirically
verify the request boundary:

1. Use a nonproduction deployment with known capacity.
2. Disable SDK retries so intermediate 429 responses remain visible.
3. Use concurrent short requests; a sequential loop can self-throttle on network latency.
4. Record status, `retry-after-ms`, and rate-limit headers.
5. Stop at the first HTTP 429 and retry only after the instructed delay.
6. Repeat several intervals because Standard deployments can experience
   temporary capacity throttling independently of configured quota.

This test observes enforcement but consumes real quota. The embedding-3-small 100-record sample
did not reach the approximately 167-request budget for a 10-second evaluation
window. The intentional 1,200-call test used concurrency to exceed that
short-window budget.

The embedding-3-small 200-request boundary test used 100 workers and no SDK retries. All 200
requests started within 2.226 seconds and returned HTTP 200. Component duration
was 7.822 seconds. This confirms that Foundry can allow bursts above the nominal
short-window pacing calculation.

In the embedding-3-small live 1,200-input test, the busiest rolling 10-second interval started 391
one-input-per-request calls. Foundry returned 996 HTTP 200 responses and 204 HTTP 429
`RateLimitReached` responses over 39.849 seconds. The endpoint instructed a
30-second retry. The component preserved the result and trace records, then
re-raised the original `openai.RateLimitError`, so AML reported the endpoint
error as the job failure.

A one-request packed control containing all 1,200 inputs was also rejected with
`RateLimitReached` and a 60-second retry instruction, but it ran while the
endpoint was still recovering from the one-input-per-request burst. It therefore cannot prove
that array items count as separate requests. Request rate and token rate are
independent runtime limiters, and a clean packed comparison must run after the
rolling throttle state has fully cleared.

## Packing and rate-limit impact

Batching inputs with matching request settings changes request consumption, not the amount of text:

- 100 one-input-per-request calls use 100 request slots.
- One call containing an array of 100 texts uses one request slot.
- TPM remains approximately the sum of tokens in those same 100 texts.

Microsoft's embedding guidance defines these request constraints:

- Each input is limited to 8,192 tokens.
- An input array can contain at most 2,048 items.
- One embedding request can contain at most 300,000 aggregate input tokens.

Source: [Generate embeddings with Azure OpenAI](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/embeddings#best-practices)

This project's default pack size is 128 inputs, below the service item limit.
The packer also separates requests with different `model`, `dimensions`,
`encoding_format`, or `user` settings.
