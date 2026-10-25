"""Callbacks through `orbit tool run`, against a fake `orbit` program (no real Orbit)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from decision_mail.orbit_api import CallbackError, CoreRefusal, OrbitCli

FAKE = """\
import json, os, sys, time
log = os.environ["FAKE_ORBIT_LOG"]
args = sys.argv[1:]
payload = json.load(open(args[args.index("--input-file") + 1]))
with open(log, "w") as handle:
    json.dump({"argv": args, "payload": payload}, handle)
mode = os.environ["FAKE_ORBIT_MODE"]
if mode == "ok":
    print(json.dumps({"id": payload.get("id"), "state": "answered"}))
elif mode == "refused":
    sys.stderr.write(json.dumps({"error": "invalid input: token_consumed: the token was "
                                 "already presented; nothing was applied",
                                 "code": "invalid_input"}) + "\\n")
    sys.exit(1)
elif mode == "broken":
    sys.stderr.write("thread 'main' panicked\\n")
    sys.exit(101)
elif mode == "garbage":
    print("not json")
elif mode == "hang":
    time.sleep(30)
"""


@pytest.fixture
def fake_orbit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    script = tmp_path / "fake_orbit.py"
    script.write_text(FAKE)
    program = tmp_path / "orbit"
    program.write_text(f"#!{sys.executable}\nexec(open({str(script)!r}).read())\n")
    program.chmod(0o700)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_ORBIT_LOG", str(log))
    return str(program), log


def logged(log: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(log.read_text())
    return data


def test_a_callback_passes_input_by_file_never_in_argv(
    fake_orbit: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    program, log = fake_orbit
    monkeypatch.setenv("FAKE_ORBIT_MODE", "ok")
    payload = {"id": "dr-0123456789abcdef", "token": "TZMGKITEFF35TIXW6EFN6IN6DR"}
    output = OrbitCli(program).call("orbit.decision.submit", payload)
    assert output == {"id": "dr-0123456789abcdef", "state": "answered"}
    record = logged(log)
    assert record["payload"] == payload
    argv: list[str] = record["argv"]
    assert argv[:3] == ["tool", "run", "orbit.decision.submit"]
    assert argv[-2:] == ["--format", "json"]
    assert payload["token"] not in " ".join(argv)
    # An agent flag would make core judge the caller an agent and refuse the submission.
    assert not {"--model", "--agent"} & set(argv)


def test_a_core_refusal_is_read_from_the_error_message(
    fake_orbit: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    program, _ = fake_orbit
    monkeypatch.setenv("FAKE_ORBIT_MODE", "refused")
    with pytest.raises(CoreRefusal) as raised:
        OrbitCli(program).call("orbit.decision.submit", {"id": "x"})
    assert raised.value.code == "token_consumed"


@pytest.mark.parametrize(("mode", "unknown"), [("broken", False), ("garbage", True)])
def test_other_failures_are_callback_errors(
    fake_orbit: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, mode: str, unknown: bool
) -> None:
    program, _ = fake_orbit
    monkeypatch.setenv("FAKE_ORBIT_MODE", mode)
    with pytest.raises(CallbackError) as raised:
        OrbitCli(program).call("orbit.decision.show", {"id": "x"})
    assert raised.value.unknown_outcome is unknown


def test_a_hung_callback_is_killed_with_an_unknown_outcome(
    fake_orbit: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    program, _ = fake_orbit
    monkeypatch.setenv("FAKE_ORBIT_MODE", "hang")
    started = time.monotonic()
    with pytest.raises(CallbackError) as raised:
        OrbitCli(program, timeout=0.5).call("orbit.decision.submit", {"id": "x"})
    assert raised.value.unknown_outcome is True
    assert time.monotonic() - started < 10


def test_a_missing_orbit_program_is_a_callback_error() -> None:
    with pytest.raises(CallbackError):
        OrbitCli(which=lambda name: None).call("orbit.decision.list", {})
