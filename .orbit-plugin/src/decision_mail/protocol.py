"""Orbit exec protocol: one request envelope on stdin, one response envelope on stdout.

The request is `{"schema_version": 1, "tool": "<ns>.<verb>", "input": {...},
"context": {...}}`; the response is `{"ok": true, "output": {...}}` or `{"ok": false,
"error": {"code", "message", "retryable", "detail"?}}`. Identity, config and secrets come
from `context`, never from `input`. As a last line of defence every response, and every
log line (see `redact`), is scrubbed of the delivered secret values before it leaves the
process.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from decision_mail import NAMESPACE, redact
from decision_mail.errors import ToolError
from decision_mail.tools import Deps, activity, default_deps, notify, parse_reply, poll, status

__all__ = ["SCHEMA_VERSION", "TOOLS", "ToolError", "dispatch", "serve"]

SCHEMA_VERSION = 1

JsonObject = dict[str, Any]
Handler = Callable[[JsonObject, JsonObject, Deps], JsonObject]


# Verb -> (accepted input keys, handler). A handler receives validated input, the call
# context and the transport dependencies.
TOOLS: Mapping[str, tuple[frozenset[str], Handler]] = {
    "status": (frozenset(), status),
    "activity": (frozenset(), activity),
    "notify": (frozenset({"request_ids", "resend"}), notify),
    "poll": (frozenset(), poll),
    "parse-reply": (frozenset({"text", "answers"}), parse_reply),
}


def serve(raw: str, deps: Callable[[], Deps] | None = None) -> str:
    """Answer one raw request with one serialized response envelope."""
    redact.install(())
    try:
        request = _parse(raw)
        redact.install(_secret_values(request))
        output = dispatch(request, deps or default_deps)
        body = json.dumps({"ok": True, "output": output}, sort_keys=True)
    except ToolError as error:
        body = json.dumps(_error_envelope(error), sort_keys=True)
    except Exception as error:  # an unexpected failure still answers with one envelope
        unexpected = ToolError(
            "internal_error", f"{type(error).__name__}: {' '.join(str(error).split())[:300]}"
        )
        body = json.dumps(_error_envelope(unexpected), sort_keys=True)
    return redact.text(body)


def dispatch(request: JsonObject, deps: Callable[[], Deps] = default_deps) -> JsonObject:
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
    unknown = sorted(set(tool_input) - accepted)  # pyright: ignore[reportUnknownArgumentType]
    if unknown:
        raise ToolError(
            "invalid_argument",
            f"{tool} does not accept: {', '.join(unknown)}",
            detail={"unknown_keys": unknown},
        )

    context = request.get("context", {})
    if not isinstance(context, dict):
        raise ToolError("invalid_request", "context must be a JSON object")
    return handler(tool_input, context, deps())  # pyright: ignore[reportUnknownArgumentType]


def _parse(raw: str) -> JsonObject:
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ToolError("invalid_request", f"request is not JSON: {error.msg}") from error
    if not isinstance(request, dict):
        raise ToolError("invalid_request", "request must be a JSON object")
    version = request.get("schema_version")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if version != SCHEMA_VERSION:
        raise ToolError(
            "unsupported_schema_version",
            f"this backend speaks exec schema_version {SCHEMA_VERSION}, got {version!r}",
        )
    return request  # pyright: ignore[reportUnknownVariableType]


def _error_envelope(error: ToolError) -> JsonObject:
    body: JsonObject = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.detail is not None:
        body["detail"] = error.detail
    return {"ok": False, "error": body}


def _secret_values(request: JsonObject) -> list[str]:
    """Every secret value delivered in the call context."""
    context = request.get("context")
    secrets = context.get("secrets") if isinstance(context, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(secrets, dict):
        return []
    values: list[str] = []
    for entry in secrets.values():  # pyright: ignore[reportUnknownVariableType]
        value = entry.get("value") if isinstance(entry, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(value, str):
            values.append(value)
    return values
