"""Validation errors that are safe to serialize into Durable history."""

import json


class NormalizationValidationError(Exception):
    """Invalid normalization input/output; caller owns terminal-failure routing."""


def model_object(raw: str) -> dict:
    """Parse model JSON without including its content in an exception."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise NormalizationValidationError("Invalid normalization model JSON") from None
    if not isinstance(value, dict):
        raise NormalizationValidationError("Normalization model response must be an object")
    return value
