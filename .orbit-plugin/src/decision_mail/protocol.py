"""Orbit exec protocol: one request envelope on stdin, one response envelope on stdout.

The request is `{"schema_version": 1, "tool": "<ns>.<verb>", "input": {...},
"context": {...}}`; the response is `{"ok": true, "output": {...}}` or `{"ok": false,
"error": {"code", "message", "retryable", "detail"?}}`. Identity and secrets come
from `context`, never from `input`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from decision_mail import NAMESPACE
from decision_mail.tools import status

SCHEMA_VERSION = 1

JsonObject = dict[str, Any]
Handler = Callable[[JsonObject, JsonObject], JsonObject]


class ToolError(Exception):
    """A refusal reported to Orbit as an error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        detail: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail


# Verb -> (accepted input keys, handler). A handler receives validated input and
# the call context.
TOOLS: Mapping[str, tuple[frozenset[str], Handler]] = {
    "status": (frozenset(), status),
}


def serve(raw: str) -> str:
    """Answer one raw request with one serialized response envelope."""
    try:
        output = dispatch(_parse(raw))
    except ToolError as error:
        return json.dumps(_error_envelope(error), sort_keys=True)
    return json.dumps({"ok": True, "output": output}, sort_keys=True)


def dispatch(request: JsonObject) -> JsonObject:
    tool = request.get("tool")
    if not isinstance(tool, str) or not tool.startswith(f"{NAMESPACE}."):
        raise ToolError("unknown_tool", f"this backend serves only {NAMESPACE}.* tools")
    verb = tool.removeprefix(f"{NAMESPACE}.")
    entry = TOOLS.get(verb)
    if entry is None:
        raise ToolError("unknown_tool", f"{tool} is not a {NAMESPACE} tool")
    accepted, handler = entry

    tool_input = request.get("input", {})
    if not isinstance(tool_input, dict):
        raise ToolError("invalid_argument", "input must be a JSON object")
    unknown = sorted(set(tool_input) - accepted)
    if unknown:
        raise ToolError(
            "invalid_argument",
            f"{tool} does not accept: {', '.join(unknown)}",
            detail={"unknown_keys": unknown},
        )

    context = request.get("context", {})
    if not isinstance(context, dict):
        raise ToolError("invalid_request", "context must be a JSON object")
    return handler(tool_input, context)


def _parse(raw: str) -> JsonObject:
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ToolError("invalid_request", f"request is not JSON: {error.msg}") from error
    if not isinstance(request, dict):
        raise ToolError("invalid_request", "request must be a JSON object")
    version = request.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ToolError(
            "unsupported_schema_version",
            f"this backend speaks exec schema_version {SCHEMA_VERSION}, got {version!r}",
        )
    return request


def _error_envelope(error: ToolError) -> JsonObject:
    body: JsonObject = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.detail is not None:
        body["detail"] = error.detail
    return {"ok": False, "error": body}
