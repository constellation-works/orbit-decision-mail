"""Callbacks into Orbit through `orbit tool run`, as the plugin's own callback session.

The host hands the backend its session record on file descriptor 3; the child inherits it,
and that descriptor — not tool input, not an environment variable — is what makes core see
the plugin as the caller. The call never carries `--model` or `--agent`, which would make
core treat it as an agent and refuse a submission.

Input is written to an owner-only file under TMPDIR (plugin state) and passed with
`--input-file`, so a reply token never sits in argv.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any, Protocol

# Refusal codes `orbit.decision.submit` and `orbit.decision.show` name as their message
# prefix (Orbit's orbit-setup plugins.md, "Decision channels").
REFUSAL_CODES = (
    "decision_channel_disabled",
    "channel_not_registered",
    "channel_revoked",
    "caller_not_allowed",
    "sender_not_allowed",
    "authentication_failed",
    "token_invalid",
    "token_consumed",
    "request_expired",
    "request_superseded",
    "answer_not_allowed",
    "request_not_found",
)
_REFUSAL = re.compile(r"\b(" + "|".join(REFUSAL_CODES) + r"):")
CALL_TIMEOUT_SECONDS = 45.0
_KILL_GRACE_SECONDS = 5.0
_SESSION_FD = 3


class CoreRefusal(Exception):
    """Core judged the call and refused it with one of `REFUSAL_CODES`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CallbackError(Exception):
    """The callback did not produce a judgement. `unknown_outcome` when it may have run."""

    def __init__(self, message: str, *, unknown_outcome: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.unknown_outcome = unknown_outcome


class OrbitApi(Protocol):
    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class OrbitCli:
    """`orbit tool run <tool> --input-file <file> --format json` in its own process group."""

    def __init__(
        self,
        program: str | None = None,
        *,
        timeout: float = CALL_TIMEOUT_SECONDS,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._program = program or which("orbit")
        self._timeout = timeout

    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._program:
            raise CallbackError("the orbit program is not on the backend's PATH")
        with tempfile.NamedTemporaryFile(
            "w", prefix="callback-", suffix=".json", encoding="utf-8"
        ) as handle:
            json.dump(payload, handle)
            handle.flush()
            argv = [
                self._program,
                "tool",
                "run",
                tool,
                "--input-file",
                handle.name,
                "--format",
                "json",
            ]
            returncode, stdout, stderr = self._run(argv)
        if returncode == 0:
            try:
                output = json.loads(stdout)
            except json.JSONDecodeError as error:
                raise CallbackError(
                    f"{tool} returned output that is not JSON", unknown_outcome=True
                ) from error
            if not isinstance(output, dict):
                raise CallbackError(f"{tool} returned non-object output", unknown_outcome=True)
            return output  # pyright: ignore[reportUnknownVariableType]
        message = _error_message(stderr) or _error_message(stdout) or f"exit {returncode}"
        match = _REFUSAL.search(message)
        if match:
            raise CoreRefusal(match.group(1), message[:500])
        raise CallbackError(f"{tool} failed: {message[:300]}")

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        pass_fds = (_SESSION_FD,) if _fd_open(_SESSION_FD) else ()
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
        try:
            stdout, stderr = child.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            _terminate_group(child)
            raise CallbackError(
                f"{argv[3]} did not answer within {self._timeout:.0f}s", unknown_outcome=True
            ) from None
        except BaseException:
            _terminate_group(child)
            raise
        return child.returncode, stdout, stderr


def _terminate_group(child: subprocess.Popen[str]) -> None:
    """TERM the child's group, KILL it after a grace period, and reap the child."""
    if child.returncode is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.communicate(timeout=_KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.communicate()


def _fd_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _error_message(stream: str) -> str:
    text = stream.strip()
    if not text:
        return ""
    for candidate in (text, text.splitlines()[-1]):
        try:
            body = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            error = body.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(error, dict):
                error = error.get("message")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(error, str):
                return error
    return " ".join(text.split())
