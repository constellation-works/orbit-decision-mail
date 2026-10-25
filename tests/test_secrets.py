"""No secret value leaves the process: not in output, error detail, log line or state (STD-05).

Every tool runs with sentinel secrets, through the real entry point, across the paths that
handle a password or the key: sends, polls, refusals, drops, transport errors whose text
quotes the password, and an unexpected exception that quotes the key.
"""

from __future__ import annotations

import io
import json
import smtplib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decision_mail import __main__, protocol
from decision_mail.orbit_api import CallbackError
from fakes import (
    Clock,
    FakeCore,
    ImapServer,
    Rig,
    SmtpServer,
    config,
    envelope,
    pilot_request,
    reply,
    stamp,
    task,
)

ROOT = Path(__file__).resolve().parents[1]
SMTP = "SENTINEL-smtp-3b9f1c"
IMAP = "SENTINEL-imap-77d2ae"
# The key must be hex to mint; it is still a distinctive value no output may carry.
KEY = "5e7e1e1a5e7e1e1a0bad5eed0bad5eedc0ffee11c0ffee115e7e1e1a5e7e1e1a"
SENTINELS = (SMTP, IMAP, KEY, KEY.upper())
RID = "dr-0123456789abcdef"


def sentinel_secrets() -> dict[str, Any]:
    return {
        "smtp_password": {"value": SMTP, "version": "v9"},
        "imap_password": {"value": IMAP, "version": "v9"},
        "channel_key": {"value": KEY, "version": "v9"},
    }


def make_rig(tmp_path: Path) -> Rig:
    core = FakeCore(key_hex=KEY)
    core.add(pilot_request(), task())
    return Rig(core, SmtpServer(password=SMTP), ImapServer(password=IMAP), Clock(), tmp_path)


def call_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rig: Rig,
    tool: str,
    tool_input: dict[str, Any] | None = None,
) -> tuple[str, str]:
    raw = envelope(tool, tool_input, config=config(), secrets=sentinel_secrets())
    monkeypatch.setattr(protocol, "default_deps", rig.factory())
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert __main__.main(["orbit-tool"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)
    return captured.out, captured.err


def assert_clean(*texts: str) -> None:
    for text in texts:
        for sentinel in SENTINELS:
            assert sentinel not in text


def _happy(rig: Rig) -> None:
    rig.imap.deliver(reply("2\nnote", token=rig.core.token(RID)))


def _unparseable(rig: Rig) -> None:
    rig.imap.deliver(reply("hmm", token=rig.core.token(RID)))


def _forged(rig: Rig) -> None:
    rig.imap.deliver(reply("1", token=rig.core.token(RID), stamps=(stamp(dkim="fail"),)))


def _refused(rig: Rig) -> None:
    rig.core.revoked = True
    _happy(rig)


def _smtp_quotes_password(rig: Rig) -> None:
    rig.smtp.fail_send = smtplib.SMTPAuthenticationError(535, f"bad password {SMTP}".encode())
    _happy(rig)


def _imap_quotes_password(rig: Rig) -> None:
    rig.imap.password = "something-else"


def _core_quotes_key(rig: Rig) -> None:
    rig.core.fail["orbit.decision.submit"] = CallbackError(f"echoed {KEY} and {IMAP}")
    _happy(rig)


def _unexpected_exception(rig: Rig) -> None:
    def explode(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"secret {SMTP} {KEY} {json.dumps(IMAP)}")

    rig.core.call = explode  # type: ignore[method-assign]
    _happy(rig)


SCENARIOS: dict[str, tuple[str, Callable[[Rig], None]]] = {
    "status": ("status", lambda rig: None),
    "activity": ("activity", lambda rig: None),
    "parse-reply": ("parse-reply", lambda rig: None),
    "notify": ("notify", lambda rig: None),
    "notify_smtp_error": ("notify", _smtp_quotes_password),
    "notify_unexpected_exception": ("notify", _unexpected_exception),
    "poll_applied": ("poll", _happy),
    "poll_unparseable": ("poll", _unparseable),
    "poll_dropped": ("poll", _forged),
    "poll_core_refusal": ("poll", _refused),
    "poll_smtp_error": ("poll", _smtp_quotes_password),
    "poll_imap_error": ("poll", _imap_quotes_password),
    "poll_callback_error": ("poll", _core_quotes_key),
    "poll_unexpected_exception": ("poll", _unexpected_exception),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_tool_path_leaks_a_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    tool, arrange = SCENARIOS[name]
    rig = make_rig(tmp_path)
    arrange(rig)
    tool_input = {"text": "1", "answers": ["a"]} if tool == "parse-reply" else None
    out, err = call_main(monkeypatch, capsys, rig, tool, tool_input)
    assert_clean(out, err)
    # Plugin state and every mail it sent are clean too.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert_clean(path.read_text(errors="replace"))
    for message in rig.smtp.sent:
        assert_clean(message.as_string())


def test_the_injected_errors_really_carry_the_sentinels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guard the guard: without redaction the scenarios above would leak."""
    rig = make_rig(tmp_path)
    _unexpected_exception(rig)
    monkeypatch.setattr(protocol.redact, "install", lambda values: None)
    protocol.redact._values = ()  # pyright: ignore[reportPrivateUsage]
    out, _ = call_main(monkeypatch, capsys, rig, "poll")
    assert SMTP in out


@pytest.mark.parametrize(
    ("tool", "tool_input", "code"),
    [
        ("status", {}, None),
        ("activity", {}, None),
        ("parse-reply", {"text": "2", "answers": ["a", "b"]}, None),
        ("notify", {}, "orbit_callback_failed"),
        ("poll", {}, "not_configured"),
    ],
)
def test_the_real_entry_point_leaks_nothing(
    tmp_path: Path, tool: str, tool_input: dict[str, Any], code: str | None
) -> None:
    """`python -m decision_mail orbit-tool` as the launcher runs it, with no network: notify
    finds no `orbit` on PATH and poll lacks its IMAP password before any server is opened."""
    secrets = sentinel_secrets()
    if tool == "poll":
        del secrets["imap_password"]
    raw = envelope(tool, tool_input, config=config(), secrets=secrets)
    env = {
        "PATH": str(tmp_path / "empty-bin"),
        "PYTHONPATH": str(ROOT / ".orbit-plugin" / "src"),
        "ORBIT_PLUGIN_STATE": str(tmp_path / "state"),
        "TMPDIR": str(tmp_path),
        "HOME": str(tmp_path),
    }
    done = subprocess.run(
        [sys.executable, "-s", "-m", "decision_mail", "orbit-tool"],
        input=raw,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    response = json.loads(done.stdout)
    if code is None:
        assert response["ok"] is True
    else:
        assert response["error"]["code"] == code
    assert_clean(done.stdout, done.stderr)
