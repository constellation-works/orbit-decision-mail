"""The exec protocol answers exactly one envelope per call, and refuses bad requests."""

from __future__ import annotations

import json
from typing import Any

from decision_mail.protocol import serve


def call(request: dict[str, Any] | str) -> dict[str, Any]:
    raw = request if isinstance(request, str) else json.dumps(request)
    response: dict[str, Any] = json.loads(serve(raw))
    return response


def request(tool: str = "decision-mail.status", **overrides: Any) -> dict[str, Any]:
    return {"schema_version": 1, "tool": tool, "input": {}, "context": {}, **overrides}


def test_status_reports_secret_names_without_values() -> None:
    secrets = {"channel_key": {"value": "do-not-echo", "version": "v1"}}
    response = call(request(context={"secrets": secrets}))
    assert response["ok"] is True
    assert response["output"]["secrets"] == {
        "smtp_password": "unset",
        "imap_password": "unset",
        "channel_key": "set",
    }
    assert "do-not-echo" not in json.dumps(response)


def test_unknown_input_keys_are_refused() -> None:
    response = call(request(input={"verbose": True}))
    assert response["error"]["code"] == "invalid_argument"
    assert response["error"]["detail"] == {"unknown_keys": ["verbose"]}


def test_other_namespaces_and_verbs_are_refused() -> None:
    assert call(request(tool="pulsar.status"))["error"]["code"] == "unknown_tool"
    assert call(request(tool="decision-mail.send"))["error"]["code"] == "unknown_tool"


def test_malformed_requests_get_an_error_envelope() -> None:
    assert call("not json")["error"]["code"] == "invalid_request"
    assert call("[]")["error"]["code"] == "invalid_request"
    assert call(request(schema_version=2))["error"]["code"] == "unsupported_schema_version"
