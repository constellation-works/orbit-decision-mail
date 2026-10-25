"""The manifest, schemas, goldens and code agree, and the tree is installable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from decision_mail import NAMESPACE, __version__
from decision_mail.protocol import TOOLS, serve
from decision_mail.tools import SECRET_NAMES

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".orbit-plugin"


def manifest() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    return data


def schema(ref: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((PLUGIN / ref).read_text())
    return data


def test_identity_matches_the_package() -> None:
    meta = manifest()["metadata"]
    assert meta["name"] == NAMESPACE
    assert meta["version"] == __version__


def test_declared_secrets_match_the_code() -> None:
    declared = tuple(secret["name"] for secret in manifest()["spec"]["secrets"])
    assert declared == SECRET_NAMES


def test_each_tool_accepts_exactly_its_schema_properties() -> None:
    tools = manifest()["spec"]["tools"]
    assert {tool["name"] for tool in tools} == set(TOOLS)
    for tool in tools:
        request = schema(tool["input_schema"]["$ref"])
        assert request.get("additionalProperties") is False
        assert frozenset(request.get("properties", {})) == TOOLS[tool["name"]][0]


def test_goldens_satisfy_the_output_schemas() -> None:
    tools = {tool["name"]: tool for tool in manifest()["spec"]["tools"]}
    cases = [
        case
        for path in sorted((PLUGIN / "tests" / "conformance").glob("*.yaml"))
        for case in yaml.safe_load(path.read_text())["tests"]
    ]
    assert cases
    for case in cases:
        raw = json.dumps(
            {
                "schema_version": 1,
                "tool": f"{NAMESPACE}.{case['tool']}",
                "input": case["input"],
                "context": {},
            }
        )
        response = json.loads(serve(raw))
        expect = case["expect"]
        if "output" in expect:
            assert response == {"ok": True, "output": expect["output"]}, case["name"]
            output_schema = schema(tools[case["tool"]]["output_schema"]["$ref"])
            jsonschema.validate(response["output"], output_schema)
        else:
            assert response["ok"] is False, case["name"]
            assert response["error"]["code"] == expect["error"]["code"], case["name"]


def test_plugin_tree_has_no_symlinks_and_an_executable_launcher() -> None:
    assert not [path for path in PLUGIN.rglob("*") if path.is_symlink()]
    launcher = PLUGIN / manifest()["spec"]["backend"]["command"]
    assert launcher.stat().st_mode & 0o111
