"""Mask the delivered secret values in anything leaving the process: the response
envelope and every stderr log line. Matches values, not vocabulary (STD-05 R14).

One process serves one call, so the values are installed once per call by the protocol.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

REDACTED = "[redacted]"
_MIN_LENGTH = 4

_values: tuple[str, ...] = ()


def install(values: Iterable[str]) -> None:
    global _values
    _values = tuple(
        sorted({value for value in values if len(value) >= _MIN_LENGTH}, key=len, reverse=True)
    )


def text(value: str) -> str:
    """`value` with every installed secret masked, raw and JSON-escaped."""
    for secret in _values:
        value = value.replace(json.dumps(secret)[1:-1], REDACTED).replace(secret, REDACTED)
    return value
