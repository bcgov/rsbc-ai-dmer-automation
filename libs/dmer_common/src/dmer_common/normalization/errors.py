"""Validation errors that are safe to serialize into Durable history, and the
schema-validated model call every normalization LLM step goes through."""

import json
import os
from collections.abc import Callable, Collection

from ..openai_client import OpenAIClient
from ..telemetry import get_logger
from .unmask import restore_masked, source_fields, source_strings

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


def _shape(value: object, allowed: Collection[str] | None = None) -> dict:
    """What a model call's input or output looked like, without its content:
    its size and the field names it carried (the keys of the object, or of its
    "dmer" object). With *allowed*, only names in that allowlist are listed and
    the rest are only counted: keys in a model response are model-controlled
    and can carry quoted Section D text, so they are not safe to log as-is."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    obj = value
    if isinstance(value, str):
        try:
            obj = json.loads(value)
        except ValueError:
            obj = None
    if isinstance(obj, dict) and isinstance(obj.get("dmer"), dict):
        obj = obj["dmer"]
    keys = [str(k) for k in obj] if isinstance(obj, dict) else []
    fields = sorted(k for k in keys if allowed is None or k in allowed)
    return {
        "chars": len(text),
        "field_count": len(keys),
        "unknown_field_count": len(keys) - len(fields),
        "fields": fields[:200],
    }


def call_model[T](
    openai: OpenAIClient,
    messages: list[dict],
    *,
    temperature: float | None,
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

    *temperature* None leaves it to the deployment's default (the triage
    LLM, as validated in the POC).

    Only the shape of the input sent and of each response is logged (size and
    field names -- for a response, only those in *known_keys*; any other key
    is counted, never logged), never their content: the input carries Section D
    free text, diagnoses, dates and scores, and the output quotes them as
    evidence -- health information about an identifiable person (FOIPPA), even
    without a name or licence number.
    """
    texts = source_strings(source)
    fields = source_fields(source)
    shape = _shape(source)
    _log.info(
        "model input",
        extra={
            "step": step,
            "input_chars": shape["chars"],
            "input_fields": shape["fields"],
        },
    )
    for attempt in range(1, MODEL_ATTEMPTS + 1):
        raw = openai.complete(
            messages=messages,
            response_format={"type": "json_object"},
            **({} if temperature is None else {"temperature": temperature}),
        )
        _log.info(
            "model output (raw)",
            extra={"step": step, "attempt": attempt, "output_chars": len(raw)},
        )
        try:
            response = restore_masked(
                model_object(raw), texts, known_keys, step=step, fields=fields
            )
            output = _shape(response, known_keys)
            _log.info(
                "model output (restored)",
                extra={
                    "step": step,
                    "attempt": attempt,
                    "output_field_count": output["field_count"],
                    "output_unknown_field_count": output["unknown_field_count"],
                    "output_fields": output["fields"],
                },
            )
            return accept(response)
        except (NormalizationValidationError, InvalidModelOutput) as exc:
            _log.warning(
                "model output failed schema validation",
                extra={"step": step, "attempt": attempt, "reason": str(exc)},
            )
    raise NormalizationValidationError(
        f"Normalization model output failed schema validation after {MODEL_ATTEMPTS} attempts ({step})"
    )
