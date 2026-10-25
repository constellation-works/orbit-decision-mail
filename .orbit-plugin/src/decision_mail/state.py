"""The plugin's own bookkeeping under plugin state: which requests were mailed, refusal-mail
rate limits, the unparseable-reply loop guard, recent drops, transport errors and a bounded
record of drops, refusals and suppressions.

It holds no secret, no token, no message body and no header dump. An event keeps the sender
address, the reason, the request id and the time. `notify` and `poll` hold `run.lock` for
their whole call, so two calls never interleave their read-modify-write; the file is replaced
atomically and created owner-only. A state file written by a newer plugin is never overwritten.

Digest answers are confirmed in batches: `poll` queues one `pending_confirmations` entry per
applied item (request, task, title, answer, task status; no mail text) and the next digest mail
lists and clears them. `last_digest` records the latest digest sent.

Counters are cumulative for the life of the state file. The event list keeps the newest
`MAX_EVENTS` (20); an older event is evicted first. Schema 1 and 2 files still load. The next
save stamps schema 3, which this shape belongs to.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
STATE_FILE = "state.json"
LOCK_FILE = "run.lock"
LOCK_DEADLINE_SECONDS = 20.0
MAX_DROPS = 20
# Applied digest answers waiting for the next digest; the oldest is evicted past the bound.
MAX_PENDING = 100
# Newest events kept. Documented in README.md and docs/runbook.md; tests import this name.
MAX_EVENTS = 20
RETAIN = timedelta(days=14)
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_ID = re.compile(r"^dr-[0-9a-f]{16}$")
_KINDS = frozenset({"drop", "refusal", "transport", "loop_guard"})
_TRANSPORT = frozenset({"smtp", "imap"})


class StateError(Exception):
    """The state cannot be read, written or locked; `code` names which."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def empty_counters() -> dict[str, Any]:
    return {
        "drops": {},
        "refusals": {},
        "transport": {"smtp": 0, "imap": 0},
        "loop_guard": 0,
    }


def empty() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "notified": {},
        "refusal_mail": {},
        "parse_refusals": {},
        "drops": [],
        "pending_confirmations": [],
        "counters": empty_counters(),
        "events": [],
        "last_notify": None,
        "last_poll": None,
        "last_digest": None,
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
        normalize(state)
        return state

    def save(self, state: dict[str, Any], now: datetime) -> None:
        normalize(state)
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


def queue_confirmation(state: dict[str, Any], entry: dict[str, Any]) -> None:
    """Remember an applied digest answer for the next digest; one entry per request."""
    pending: list[Any] = state["pending_confirmations"]
    pending[:] = [item for item in pending if item.get("request_id") != entry.get("request_id")]
    pending.append(entry)
    del pending[:-MAX_PENDING]


def record_event(
    state: dict[str, Any],
    *,
    kind: str,
    reason: str,
    at: str,
    sender: str | None = None,
    request_id: str | None = None,
) -> None:
    """Count one outcome and append its event, evicting the oldest past `MAX_EVENTS`.

    `reason` is a plugin code (`sender_not_allowed`, `token_consumed`, `smtp`, `loop_guard`),
    never a message, a header or a server's text. An unrecognised reason counts as `other`.
    """
    normalize(state)
    if kind not in _KINDS:
        return
    if _REASON.fullmatch(reason) is None:
        reason = "other"
    if kind == "transport" and reason not in _TRANSPORT:
        return
    if kind == "loop_guard":
        reason = "loop_guard"
    counters: dict[str, Any] = state["counters"]
    if kind == "drop":
        _bump(counters["drops"], reason)
    elif kind == "refusal":
        _bump(counters["refusals"], reason)
    elif kind == "transport":
        transport: dict[str, int] = counters["transport"]
        transport[reason] = transport[reason] + 1
    else:
        counters["loop_guard"] = counters["loop_guard"] + 1
    events: list[Any] = state["events"]
    events.append(
        {
            "at": at,
            "kind": kind,
            "reason": reason,
            "sender": _sender(sender),
            "request_id": _request_id(request_id),
        }
    )
    del events[:-MAX_EVENTS]


def report(state: dict[str, Any]) -> dict[str, Any]:
    """The counters, the retained events and the documented bound. No other state."""
    normalize(state)
    counters: dict[str, Any] = state["counters"]
    transport: dict[str, int] = counters["transport"]
    events: list[Any] = state["events"]
    return {
        "bound": MAX_EVENTS,
        "counters": {
            "drops": dict(counters["drops"]),
            "refusals": dict(counters["refusals"]),
            "transport": {"smtp": transport["smtp"], "imap": transport["imap"]},
            "loop_guard": counters["loop_guard"],
        },
        "events": [dict(event) for event in events],
    }


def normalize(state: dict[str, Any]) -> None:
    """Fill counters and events the shape this plugin writes, and stamp the schema version."""
    raw = state.get("counters")
    raw = raw if isinstance(raw, dict) else {}
    transport = raw.get("transport")
    transport = transport if isinstance(transport, dict) else {}
    loop_guard = raw.get("loop_guard")
    state["counters"] = {
        "drops": _count_map(raw.get("drops")),
        "refusals": _count_map(raw.get("refusals")),
        "transport": {"smtp": _count(transport.get("smtp")), "imap": _count(transport.get("imap"))},
        "loop_guard": _count(loop_guard),
    }
    pending = state.get("pending_confirmations")
    state["pending_confirmations"] = (
        [item for item in pending if _pending_ok(item)][-MAX_PENDING:]
        if isinstance(pending, list)
        else []
    )
    events = state.get("events")
    kept = [event for event in events if _event_ok(event)] if isinstance(events, list) else []
    state["events"] = kept[-MAX_EVENTS:]
    state["schema_version"] = SCHEMA_VERSION


def _bump(table: dict[str, int], key: str) -> None:
    table[key] = table.get(key, 0) + 1


def _count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, int] = {}
    for key, count in value.items():
        if isinstance(key, str) and _REASON.fullmatch(key) is not None and _count_ok(count):
            cleaned[key] = count
    return cleaned


def _count_ok(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _count(value: Any) -> int:
    return value if _count_ok(value) else 0


def _request_id(value: str | None) -> str | None:
    if isinstance(value, str) and _REQUEST_ID.fullmatch(value):
        return value
    return None


def _sender(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(char for char in " ".join(value.split()) if char.isprintable())
    if not text:
        return None
    return text if len(text) <= 120 else text[:119] + "…"


def _pending_ok(value: Any) -> bool:
    fields = ("request_id", "task_id", "title", "answer", "label", "task_status", "at")
    return (
        isinstance(value, dict)
        and all(isinstance(value.get(field), str) for field in fields)
        and _REQUEST_ID.fullmatch(str(value["request_id"])) is not None
    )


def _event_ok(value: Any) -> bool:
    fields = {"at", "kind", "reason", "sender", "request_id"}
    if not isinstance(value, dict) or set(value) != fields:
        return False
    kind = value.get("kind")
    reason = value.get("reason")
    sender = value.get("sender")
    request_id = value.get("request_id")
    return (
        kind in _KINDS
        and isinstance(value.get("at"), str)
        and isinstance(reason, str)
        and _REASON.fullmatch(reason) is not None
        and (sender is None or isinstance(sender, str))
        and (request_id is None or isinstance(request_id, str))
    )


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
    pending: list[Any] = state["pending_confirmations"]
    pending[:] = [
        item for item in pending if (stamp := _stamp(item, "at")) is None or stamp >= now - RETAIN
    ]


def _stamp(entry: Any, field: str) -> datetime | None:
    value = entry.get(field) if isinstance(entry, dict) else None
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None
