"""The manifest, schemas, goldens and code agree, and the tree is installable."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from decision_mail import NAMESPACE, __version__
from decision_mail.config import DEFAULTS, REQUIRED_KEYS
from decision_mail.protocol import TOOLS, serve
from decision_mail.tools import SECRET_NAMES, Deps
from fakes import Clock, FakeCore, ImapServer, SmtpServer

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".orbit-plugin"
SOURCE = PLUGIN / "src" / "decision_mail"

# What each tool may do: reading and judging are read-only, mailing or submitting mutates.
EXECUTION_KIND = {
    "status": "read_only",
    "parse-reply": "read_only",
    "notify": "mutating",
    "poll": "mutating",
}


def manifest() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    return data


def schema(ref: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((PLUGIN / ref).read_text())
    return data


def definitions(kind: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for pattern in manifest()["spec"]["definitions"][kind]:
        for path in sorted(PLUGIN.glob(pattern)):
            data: dict[str, Any] = yaml.safe_load(path.read_text())
            name = data["name"] if "name" in data else data["metadata"]["name"]
            found[name] = data
    return found


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
        jsonschema.Draft202012Validator.check_schema(request)
        jsonschema.Draft202012Validator.check_schema(schema(tool["output_schema"]["$ref"]))


def test_execution_kinds_follow_what_each_tool_does() -> None:
    kinds = {tool["name"]: tool["execution_kind"] for tool in manifest()["spec"]["tools"]}
    assert kinds == EXECUTION_KIND


def test_orbit_tools_grant_exactly_the_callbacks_the_code_makes() -> None:
    granted = set(manifest()["spec"]["permissions"]["orbit_tools"])
    called: set[str] = set()
    for path in SOURCE.glob("*.py"):
        called |= set(re.findall(r'"(orbit\.[a-z_]+\.[a-z_]+)"', path.read_text()))
    assert called == granted
    # The plugin never moves a task: decision.submit is its only write.
    assert not {name for name in granted if name.startswith("orbit.task.")} - {"orbit.task.show"}


def test_config_schema_matches_the_code() -> None:
    config = schema(manifest()["spec"]["config"]["schema"])
    assert config.get("additionalProperties") is False
    keys = set(config["properties"])
    assert keys == set(REQUIRED_KEYS) | set(DEFAULTS) | {"smtp_username", "imap_username"}
    for key, value in DEFAULTS.items():
        assert config["properties"][key].get("default") == value, key
    # Credentials are secrets, never config.
    assert not keys & set(SECRET_NAMES)
    assert not [key for key in keys if re.search("password|secret|token|key$", key)]


def test_routines_ship_disabled_and_target_shipped_jobs() -> None:
    routines = definitions("routines")
    jobs = definitions("jobs")
    activities = definitions("activities")
    assert set(routines) == {"notify", "poll"}
    for routine in routines.values():
        assert routine["enabled"] is False
        assert routine["target"].removeprefix("job:") in jobs
    state = routines["notify"]["trigger"]["state"]
    assert state["kind"] == "decision_requested"
    # Core's decision_requested trigger targets exactly this job name.
    assert routines["notify"]["target"] == "job:decision_request_pipeline"
    assert routines["poll"]["trigger"]["cron"]
    for job in jobs.values():
        for step in job["spec"]["steps"]:
            assert step["target"].removeprefix("activity:") in activities
    called = {activity["spec"]["config"]["tool"] for activity in activities.values()}
    assert called == {f"{NAMESPACE}.notify", f"{NAMESPACE}.poll"}


def test_goldens_satisfy_the_output_schemas(tmp_path: Path) -> None:
    tools = {tool["name"]: tool for tool in manifest()["spec"]["tools"]}
    cases = [
        case
        for path in sorted((PLUGIN / "tests" / "conformance").glob("*.yaml"))
        for case in yaml.safe_load(path.read_text())["tests"]
    ]
    assert cases
    core = FakeCore()

    def deps() -> Deps:
        # A golden reaches no server: every factory here fails the test if it is used.
        return Deps(
            orbit=core,
            smtp=SmtpServer(password="unused").client,
            imap=ImapServer(password="unused").client,
            state_dir=tmp_path / case["name"],
            now=Clock(),
        )

    for case in cases:
        secrets = {
            name: {"value": value, "version": "fixture"}
            for name, value in (case.get("secrets") or {}).items()
        }
        raw = json.dumps(
            {
                "schema_version": 1,
                "tool": f"{NAMESPACE}.{case['tool']}",
                "input": case["input"],
                "context": {"secrets": secrets, "config": {}},
            }
        )
        response = json.loads(serve(raw, deps))
        expect = case["expect"]
        if "output" in expect:
            assert response == {"ok": True, "output": expect["output"]}, case["name"]
            output_schema = schema(tools[case["tool"]]["output_schema"]["$ref"])
            jsonschema.validate(response["output"], output_schema)
        else:
            assert response["ok"] is False, case["name"]
            assert response["error"]["code"] == expect["error"]["code"], case["name"]
        for secret in secrets.values():
            assert secret["value"] not in json.dumps(response), case["name"]
    assert core.calls == []


@pytest.mark.parametrize("name", ["notify", "poll"])
def test_definitions_call_the_tools_with_no_input(name: str) -> None:
    activity = definitions("activities")[f"decision_mail_{name}"]
    assert activity["spec"]["action"] == "plugin.tool_call"
    assert activity["spec"]["config"]["input"] == {}


def test_plugin_tree_has_no_symlinks_and_an_executable_launcher() -> None:
    assert not [path for path in PLUGIN.rglob("*") if path.is_symlink()]
    launcher = PLUGIN / manifest()["spec"]["backend"]["command"]
    assert launcher.stat().st_mode & 0o111
