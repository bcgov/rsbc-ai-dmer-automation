"""Validation errors that are safe to serialize into Durable history, and the
schema-validated model call every normalization LLM step goes through."""

import json
import os
from collections.abc import Callable, Collection

from ..openai_client import OpenAIClient
from ..telemetry import get_logger
from .unmask import restore_masked, source_strings

_log = get_logger(__name__)

# How many times one model call is made before invalid output is poison
# (docs/development/stages/04-activity-normalize.md: "a schema-validation
# failure that can't be resolved by retrying is poison").
MODEL_ATTEMPTS = max(1, int(os.environ.get("NORMALIZATION_MODEL_ATTEMPTS", "3")))


class NormalizationValidationError(Exception):
    """Invalid normalization input/output; caller owns terminal-failure routing."""


class InvalidModelOutput(Exception):
    """Model output that standardizing can't fix -- the call is retried.
    The message names fields/shapes only, never model or document content."""


def model_object(raw: str) -> dict:
    """Parse model JSON without including its content in an exception."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise NormalizationValidationError("Invalid normalization model JSON") from None
    if not isinstance(value, dict):
        raise NormalizationValidationError(
            "Normalization model response must be an object"
        )
    return value


def call_model[T](
    openai: OpenAIClient,
    messages: list[dict],
    *,
    temperature: float,
    accept: Callable[[dict], T],
    step: str,
    source: object = None,
    known_keys: Collection[str] = (),
) -> T:
    """Make a JSON-mode model call and validate its output against the schema.

    *accept* standardizes the parsed response (the same deterministic
    conversion DI input gets) and raises :class:`InvalidModelOutput` for
    anything that can't be fixed that way; the call is then retried, up to
    :data:`MODEL_ATTEMPTS` times in total, before the output is poison.

    Before that, words the deployment masked with asterisks are restored
    from *source* (the input this call sent) and *known_keys* (the field
    names it may return) -- see :mod:`.unmask`.
    """
    texts = source_strings(source)
    for attempt in range(1, MODEL_ATTEMPTS + 1):
        raw = openai.complete(
            messages=messages,
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        try:
            response = restore_masked(model_object(raw), texts, known_keys, step=step)
            return accept(response)
        except (NormalizationValidationError, InvalidModelOutput) as exc:
            _log.warning(
                "model output failed schema validation",
                extra={"step": step, "attempt": attempt, "reason": str(exc)},
            )
    raise NormalizationValidationError(
        f"Normalization model output failed schema validation after {MODEL_ATTEMPTS} attempts ({step})"
    )
