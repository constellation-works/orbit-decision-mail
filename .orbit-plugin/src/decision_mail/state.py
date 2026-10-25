"""The plugin's own bookkeeping under plugin state: which requests were mailed, refusal-mail
rate limits, the unparseable-reply loop guard, recent drops and the last transport error.

It holds no secret and no token. `notify` and `poll` hold `run.lock` for their whole call,
so two calls never interleave their read-modify-write; the file is replaced atomically and
created owner-only. A state file written by a newer plugin is never overwritten.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATE_FILE = "state.json"
LOCK_FILE = "run.lock"
LOCK_DEADLINE_SECONDS = 20.0
MAX_DROPS = 20
RETAIN = timedelta(days=14)


class StateError(Exception):
    """The state cannot be read, written or locked; `code` names which."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def empty() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "notified": {},
        "refusal_mail": {},
        "parse_refusals": {},
        "drops": [],
        "last_notify": None,
        "last_poll": None,
        "last_error": None,
    }


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @contextlib.contextmanager
    def locked(self, tool: str, deadline: float = LOCK_DEADLINE_SECONDS) -> Iterator[None]:
        self._ensure_dir()
        fd = os.open(self.directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            give_up = time.monotonic() + deadline
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= give_up:
                        holder = os.pread(fd, 200, 0).decode(errors="replace").strip()
                        holder = holder or "unknown holder"
                        raise StateError(
                            "state_locked",
                            f"another decision-mail call holds the state lock ({holder})",
                        ) from None
                    time.sleep(0.2)
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"pid={os.getpid()} tool={tool}".encode(), 0)
            yield
        finally:
            os.close(fd)

    def load(self) -> dict[str, Any]:
        path = self.directory / STATE_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty()
        except OSError as error:
            raise StateError("state_unreadable", f"cannot read {STATE_FILE}: {error}") from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise StateError("state_unreadable", f"{STATE_FILE} is not JSON") from None
        if not isinstance(data, dict):
            raise StateError("state_unreadable", f"{STATE_FILE} is not a JSON object")
        version = data.get("schema_version")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise StateError(
                "state_too_new",
                f"{STATE_FILE} has schema_version {version!r}; this plugin writes "
                f"{SCHEMA_VERSION} and will not overwrite it",
            )
        state = empty()
        for key, default in state.items():
            value = data.get(key)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if value is not None and isinstance(
                value, type(default) if default is not None else dict
            ):
                state[key] = value
        return state

    def save(self, state: dict[str, Any], now: datetime) -> None:
        _prune(state, now)
        self._ensure_dir()
        target = self.directory / STATE_FILE
        staged = self.directory / f".{STATE_FILE}.{os.getpid()}.tmp"
        body = json.dumps(state, sort_keys=True, indent=1).encode()
        try:
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(staged, target)
            directory = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            with contextlib.suppress(OSError):
                staged.unlink()
            raise StateError("state_unwritable", f"cannot write {STATE_FILE}: {error}") from None

    def _ensure_dir(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def record_drop(state: dict[str, Any], entry: dict[str, Any]) -> None:
    drops: list[Any] = state["drops"]
    drops.append(entry)
    del drops[:-MAX_DROPS]


def _prune(state: dict[str, Any], now: datetime) -> None:
    """Forget requests long past: a notified request once it has expired a day ago (a
    request is never mailed twice while it can still be answered), the rest after RETAIN."""
    for key, field, cutoff in (
        ("notified", "expires_at", now - timedelta(days=1)),
        ("refusal_mail", "at", now - RETAIN),
        ("parse_refusals", "at", now - RETAIN),
    ):
        table: dict[str, Any] = state[key]
        for request_id in list(table):
            stamp = _stamp(table[request_id], field)
            if stamp is not None and stamp < cutoff:
                del table[request_id]


def _stamp(entry: Any, field: str) -> datetime | None:
    value = entry.get(field) if isinstance(entry, dict) else None
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None
