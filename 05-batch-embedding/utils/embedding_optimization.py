from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import tiktoken


TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class EmbeddingRequestFields:
    input_ids: str = "input_ids"
    texts: str = "texts"
    body: str = "body"
    input_count: str = "input_count"
    estimated_tokens: str = "estimated_tokens"
    model: str = "model"
    input: str = "input"
    dimensions: str = "dimensions"
    encoding_format: str = "encoding_format"
    user: str = "user"


REQUEST = EmbeddingRequestFields()


@dataclass(frozen=True)
class OptimizationPlanFields:
    target: str = "target"
    primary_tpm: str = "primary_tpm"
    secondary_tpm: str = "secondary_tpm"
    assigned_tpm: str = "assigned_tpm"
    capacity_source: str = "capacity_source"
    target_utilization: str = "target_utilization"
    target_tpm: str = "target_tpm"
    requests_per_minute: str = "requests_per_minute"
    max_batch_inputs: str = "max_batch_inputs"
    max_batch_tokens: str = "max_batch_tokens"
    tokenizer_model: str = "tokenizer_model"


PLAN = OptimizationPlanFields()


def embedding_request(
    input_ids: str | Iterable[str],
    texts: str | Iterable[str],
    model: str,
    *,
    dimensions: int | None = None,
    encoding_format: str = "float",
    user: str | None = None,
) -> dict[str, Any]:
    """Build the normalized request contract consumed by the shared packer."""
    normalized_ids = [input_ids] if isinstance(input_ids, str) else list(input_ids)
    normalized_texts = [texts] if isinstance(texts, str) else list(texts)
    if len(normalized_ids) != len(normalized_texts):
        raise ValueError("input_ids and texts must have equal lengths")
    if not normalized_texts:
        raise ValueError("texts must not be empty")
    body: dict[str, Any] = {
        REQUEST.model: model,
        REQUEST.input: normalized_texts,
        REQUEST.encoding_format: encoding_format,
    }
    if dimensions is not None:
        body[REQUEST.dimensions] = dimensions
    if user is not None:
        body[REQUEST.user] = user
    return {
        REQUEST.input_ids: normalized_ids,
        REQUEST.texts: normalized_texts,
        REQUEST.body: body,
        REQUEST.input_count: len(normalized_texts),
    }


def token_counter_for_model(model: str) -> TokenCounter:
    """Create a tokenizer-backed counter for an embedding model."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    return lambda text: len(encoding.encode(text))


def pack_compatible_requests(
    requests: Iterable[dict[str, Any]],
    packing: str,
    max_inputs_per_request: int,
    max_tokens_per_request: int | None = None,
    count_tokens: TokenCounter | None = None,
) -> Iterator[dict[str, Any]]:
    """Pack embedding inputs that share the same request settings."""
    if max_inputs_per_request < 1:
        raise ValueError("max_inputs_per_request must be positive")
    if max_tokens_per_request is not None and max_tokens_per_request < 1:
        raise ValueError("max_tokens_per_request must be positive")
    if max_tokens_per_request is not None and count_tokens is None:
        raise ValueError("count_tokens is required with max_tokens_per_request")
    if packing == "none":
        yield from requests
        return
    if packing != "batch":
        raise ValueError(f"Unsupported packing mode: {packing}")

    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for request in requests:
        body = request[REQUEST.body]
        key = (
            body[REQUEST.model],
            body.get(REQUEST.dimensions),
            body.get(REQUEST.encoding_format, "float"),
            body.get(REQUEST.user),
        )
        group = groups.setdefault(
            key,
            {
                REQUEST.input_ids: [],
                REQUEST.texts: [],
                REQUEST.body: {
                    field: value
                    for field, value in body.items()
                    if field != REQUEST.input
                },
            },
        )
        group[REQUEST.input_ids].extend(request[REQUEST.input_ids])
        group[REQUEST.texts].extend(request[REQUEST.texts])

    for group in groups.values():
        input_ids: list[str] = []
        texts: list[str] = []
        token_count = 0
        for input_id, text in zip(
            group[REQUEST.input_ids],
            group[REQUEST.texts],
            strict=True,
        ):
            text_tokens = count_tokens(text) if count_tokens else 0
            if max_tokens_per_request is not None and text_tokens > max_tokens_per_request:
                raise ValueError(
                    f"One input uses {text_tokens} tokens, exceeding the "
                    f"{max_tokens_per_request}-token request target"
                )
            reaches_input_limit = len(texts) >= max_inputs_per_request
            reaches_token_limit = (
                max_tokens_per_request is not None
                and bool(texts)
                and token_count + text_tokens > max_tokens_per_request
            )
            if reaches_input_limit or reaches_token_limit:
                yield {
                    REQUEST.input_ids: input_ids,
                    REQUEST.texts: texts,
                    REQUEST.body: {**group[REQUEST.body], REQUEST.input: texts},
                    REQUEST.input_count: len(texts),
                    REQUEST.estimated_tokens: token_count,
                }
                input_ids = []
                texts = []
                token_count = 0
            input_ids.append(input_id)
            texts.append(text)
            token_count += text_tokens
        if texts:
            yield {
                REQUEST.input_ids: input_ids,
                REQUEST.texts: texts,
                REQUEST.body: {**group[REQUEST.body], REQUEST.input: texts},
                REQUEST.input_count: len(texts),
                REQUEST.estimated_tokens: token_count,
            }


def capacity_units_to_tpm(capacity_units: int) -> int:
    """Convert Azure OpenAI deployment capacity units to assigned TPM."""
    if capacity_units < 0:
        raise ValueError("capacity_units must be non-negative")
    return capacity_units * 1000


def utilization_target_tpm(assigned_tpm: int, utilization: float) -> int:
    """Calculate an integer TPM target below assigned capacity."""
    if assigned_tpm <= 0:
        raise ValueError("assigned_tpm must be positive")
    if utilization <= 0 or utilization > 1:
        raise ValueError("utilization must be in (0, 1]")
    return int(assigned_tpm * utilization)


def target_tokens_per_request(target_tpm: int, requests_per_minute: float) -> int:
    """Calculate a token ceiling per request for a planned request cadence."""
    if target_tpm <= 0:
        raise ValueError("target_tpm must be positive")
    if requests_per_minute <= 0:
        raise ValueError("requests_per_minute must be positive")
    return max(int(target_tpm / requests_per_minute), 1)


def pacing_interval_seconds(prompt_tokens: int, target_tpm: int) -> float:
    """Return the request spacing needed to maintain a target token rate."""
    if prompt_tokens < 0:
        raise ValueError("prompt_tokens must be non-negative")
    if target_tpm <= 0:
        raise ValueError("target_tpm must be positive")
    return prompt_tokens * 60 / target_tpm


def input_pacing_interval_seconds(
    input_count: int,
    target_inputs_per_minute: float,
) -> float:
    """Return spacing for an empirical logical-input rate ceiling."""
    if input_count < 0:
        raise ValueError("input_count must be non-negative")
    if target_inputs_per_minute <= 0:
        raise ValueError("target_inputs_per_minute must be positive")
    return input_count * 60 / target_inputs_per_minute


def tokens_per_minute(prompt_tokens: int, elapsed_seconds: float) -> float:
    """Normalize accepted prompt tokens to a one-minute rate."""
    if prompt_tokens < 0:
        raise ValueError("prompt_tokens must be non-negative")
    if elapsed_seconds <= 0:
        raise ValueError("elapsed_seconds must be positive")
    return prompt_tokens * 60 / elapsed_seconds


def percentile(values: Iterable[float], percent: float) -> float | None:
    """Calculate a linearly interpolated percentile."""
    ordered = sorted(values)
    if not ordered:
        return None
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction