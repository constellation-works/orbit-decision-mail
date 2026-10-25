"""Tool handlers. Each takes validated input, the call context and the transport
dependencies, and returns output.

The plugin is a transport: `notify` mails open decision requests, `poll` reads replies,
authenticates them (design §4 layers 4–5) and hands each answer to
`orbit.decision.submit`, where core re-checks everything and applies it. Nothing here moves
a task.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from decision_mail import __version__, grammar, inbound, redact, render
from decision_mail.config import (
    SECRET_NAMES,
    Config,
    ConfigError,
    load,
    missing_config,
    raw_config,
    secret,
    secret_presence,
)
from decision_mail.errors import ToolError
from decision_mail.orbit_api import CallbackError, CoreRefusal, OrbitApi, OrbitCli
from decision_mail.state import StateError, StateStore, record_drop
from decision_mail.tokens import TokenError, fingerprint, mint, unix_seconds
from decision_mail.transport import (
    ImapFactory,
    Mailbox,
    Outbox,
    SmtpFactory,
    TransportError,
    connect_imap,
    connect_smtp,
)

__all__ = ["SECRET_NAMES", "Deps", "default_deps", "notify", "parse_reply", "poll", "status"]

REQUEST_ID = re.compile(r"dr-[0-9a-f]{16}")
REFUSAL_MAIL_INTERVAL = timedelta(hours=1)
# Unparseable (answer_not_allowed-class) replies to one request that still get a mail.
PARSE_REFUSAL_MAILS = 3
# Core refusals that tell the human, by mail, why nothing landed (design §5).
MAILED_REFUSALS = frozenset(
    {
        "token_consumed",
        "request_expired",
        "request_superseded",
        "answer_not_allowed",
        "channel_revoked",
        "token_invalid",
    }
)
# Core refusals on authentication or identity: dropped like a forged sender, no mail.
SILENT_REFUSALS = frozenset({"sender_not_allowed", "authentication_failed", "request_not_found"})


@dataclass(frozen=True)
class Deps:
    orbit: OrbitApi
    smtp: SmtpFactory
    imap: ImapFactory
    state_dir: Path | None
    now: Callable[[], datetime]


def default_deps() -> Deps:
    root = os.environ.get("ORBIT_PLUGIN_STATE")
    return Deps(
        orbit=OrbitCli(),
        smtp=connect_smtp,
        imap=connect_imap,
        state_dir=Path(root) / "mail" if root else None,
        now=lambda: datetime.now(UTC),
    )


# --- status -------------------------------------------------------------------------------


def status(_input: dict[str, Any], context: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Version, transport readiness, secret presence (names only) and recent activity."""
    presence = secret_presence(context)
    missing = missing_config(context) + [name for name in SECRET_NAMES if presence[name] != "set"]
    problem: str | None = None
    if not missing_config(context):
        try:
            load(context, secrets=())
        except ConfigError as error:
            problem = str(error)
    merged = raw_config(context)
    epoch = merged.get("key_epoch")
    output: dict[str, Any] = {
        "version": __version__,
        "transport": "ready" if not missing and problem is None else "not_configured",
        "secrets": presence,
        "missing": missing,
        "channel": str(merged.get("channel", "")),
        "key_epoch": epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None,
        "activity": _activity(deps),
    }
    if problem is not None:
        output["config_error"] = problem
    return output


def _activity(deps: Deps) -> dict[str, Any]:
    activity: dict[str, Any] = {
        "last_notify": None,
        "last_poll": None,
        "last_error": None,
        "notified_requests": 0,
        "recent_drops": [],
    }
    if deps.state_dir is None:
        return activity
    try:
        state = StateStore(deps.state_dir).load()
    except StateError as error:
        activity["last_error"] = {"tool": "status", "code": error.code, "message": error.message}
        return activity
    for key in ("last_notify", "last_poll", "last_error"):
        activity[key] = state[key]
    activity["notified_requests"] = len(state["notified"])
    activity["recent_drops"] = state["drops"]
    return activity


# --- notify -------------------------------------------------------------------------------


def notify(tool_input: dict[str, Any], context: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Mail every open decision request not yet mailed (or only `request_ids`)."""
    config = _config(context, ("smtp_password", "channel_key"))
    wanted = _request_ids(tool_input.get("request_ids"))
    resend = tool_input.get("resend", False) is True
    store = _store(deps)
    smtp_password = secret(context, "smtp_password") or ""
    key = secret(context, "channel_key") or ""
    now = deps.now()
    with _locked(store, "notify"):
        state = _load(store)
        listing = _call(deps, "orbit.decision.list", {})
        requests = [
            request
            for request in _records(listing.get("requests"))
            if wanted is None or request.get("id") in wanted
        ]
        sent: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        if wanted is not None:
            found = {request.get("id") for request in requests}
            skipped += [{"request_id": rid, "reason": "not_open"} for rid in wanted - found]
        with Outbox(config, smtp_password, deps.smtp) as outbox:
            for request in requests:
                request_id = str(request.get("id", ""))
                task_id = str(request.get("task_id", ""))
                if request.get("state") != "open":
                    skipped.append({"request_id": request_id, "reason": "not_open"})
                    continue
                if request_id in state["notified"] and not resend:
                    skipped.append({"request_id": request_id, "reason": "already_notified"})
                    continue
                expires_at = str(request.get("expires_at", ""))
                try:
                    token = mint(key, request_id, task_id, config.key_epoch, expires_at)
                    expiry = datetime.fromtimestamp(unix_seconds(expires_at), UTC)
                except TokenError as error:
                    raise ToolError("token_unmintable", str(error)) from None
                message = render.request_mail(config, request, _task(deps, task_id), token)
                try:
                    outbox.send(message)
                except TransportError as error:
                    state["last_error"] = _error_record(now, "notify", "smtp_failed", str(error))
                    state["last_notify"] = _run_record(now, sent=len(sent), failed=request_id)
                    _save(store, state, now)
                    raise ToolError(
                        "smtp_failed",
                        f"sending the mail for {request_id} failed: {error}",
                        retryable=True,
                        detail={"sent": len(sent), "failed_request": request_id},
                    ) from None
                state["notified"][request_id] = {
                    "at": now.isoformat(),
                    "task_id": task_id,
                    "expires_at": expiry.isoformat(),
                }
                _save(store, state, now)
                sent.append(
                    {"request_id": request_id, "task_id": task_id, "gate": request.get("gate")}
                )
        state["last_notify"] = _run_record(now, sent=len(sent), skipped=len(skipped))
        _save(store, state, now)
    return {"sent": sent, "skipped": skipped}


def _task(deps: Deps, task_id: str) -> dict[str, Any]:
    """The task's title and status for the mail; a failed read still mails the request."""
    try:
        return _call(deps, "orbit.task.show", {"id": task_id})
    except ToolError:
        return {}


# --- poll ---------------------------------------------------------------------------------


@dataclass
class _Poll:
    config: Config
    deps: Deps
    state: dict[str, Any]
    mailbox: Mailbox
    outbox: Outbox
    now: datetime
    mail_failures: int = 0


class _Halt(Exception):
    """Stop the poll and leave the current message unseen for the next tick."""

    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message)
        self.error = error


def poll(_input: dict[str, Any], context: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Read unseen replies, authenticate them and submit each answer to core."""
    config = _config(context, ("imap_password", "smtp_password"))
    store = _store(deps)
    now = deps.now()
    results: list[dict[str, Any]] = []
    mail_failures = 0
    with _locked(store, "poll"):
        state = _load(store)
        try:
            with (
                Mailbox(config, secret(context, "imap_password") or "", deps.imap) as mailbox,
                Outbox(config, secret(context, "smtp_password") or "", deps.smtp) as outbox,
            ):
                run = _Poll(config, deps, state, mailbox, outbox, now)
                for uid in mailbox.unseen(config.max_messages):
                    try:
                        results.append(_handle(run, uid))
                    except _Halt as halt:
                        state["last_error"] = _error_record(
                            now, "poll", halt.error.code, halt.error.message
                        )
                        state["last_poll"] = _run_record(now, **_counts(results), halted=True)
                        _save(store, state, now)
                        raise halt.error from None
                    _save(store, state, now)
                mail_failures = run.mail_failures
        except TransportError as error:
            state["last_error"] = _error_record(now, "poll", "imap_failed", str(error))
            state["last_poll"] = _run_record(now, **_counts(results), halted=True)
            _save(store, state, now)
            raise ToolError("imap_failed", str(error), retryable=True) from None
        counts = _counts(results)
        if mail_failures:
            state["last_error"] = _error_record(
                now, "poll", "smtp_failed", f"{mail_failures} reply mail(s) failed to send"
            )
        state["last_poll"] = _run_record(now, **counts)
        _save(store, state, now)
    return {"messages": results, "counts": counts}


def _handle(run: _Poll, uid: str) -> dict[str, Any]:
    raw = run.mailbox.fetch(uid)
    if raw is None:
        return _drop(run, uid, None, "unreadable_message", None)
    if len(raw) > inbound.MAX_MESSAGE_BYTES:
        return _drop(run, uid, None, "message_too_large", None)
    try:
        message = inbound.read(raw, run.config)
    except Exception:  # hostile or broken mail must not wedge every later poll
        return _drop(run, uid, None, "unreadable_message", None)
    sender = message.sender
    if not message.addressed_to_channel:
        return _drop(run, uid, sender, "not_addressed_to_channel", message)
    if sender is None or sender not in run.config.allowed_senders:
        return _drop(run, uid, sender, "sender_not_allowed", message)
    envelope = message.envelope_sender
    if envelope is not None and envelope not in run.config.allowed_senders:
        return _drop(run, uid, sender, "envelope_sender_not_allowed", message)
    if not message.verdicts.passes:
        return _drop(run, uid, sender, "authentication_failed", message)
    if len(message.tokens) != 1:
        return _drop(run, uid, sender, "malformed_token", message)
    if len(message.request_ids) != 1:
        return _drop(run, uid, sender, "unknown_request", message)
    (token,) = message.tokens
    (request_id,) = message.request_ids
    try:
        request = _show(run.deps, request_id)
    except CoreRefusal:
        return _drop(run, uid, sender, "unknown_request", message)

    resolution = request.get("resolution")
    if (
        request.get("state") == "answered"
        and isinstance(resolution, dict)
        and fingerprint(token) in str(resolution.get("provenance", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    ):
        # This reply's own earlier submission landed but the poll stopped before filing it.
        answer = str(resolution.get("answer_id", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        return _applied(run, uid, sender, request, answer, recovered=True)

    answers = [str(answer.get("id", "")) for answer in _records(request.get("answers"))]
    if message.text is None:
        reply = grammar.Reply("unparseable", None, "", "the reply has no readable text part")
    else:
        reply = grammar.parse(message.text, answers)
    if reply.outcome != "answer" or reply.answer_id is None:
        mail = _parse_refusal(run, sender, request, reply.reason)
        run.mailbox.mark_seen(uid, flagged=True)
        return _result(uid, f"refused_{reply.outcome}", request_id, sender, mail=mail)

    submission: dict[str, Any] = {
        "id": request_id,
        "channel": run.config.channel,
        "answer": reply.answer_id,
        "token": token,
        "sender": sender,
        "auth_results": message.verdicts.header,
    }
    if reply.note:
        submission["note"] = reply.note
    try:
        _call_raw(run.deps, "orbit.decision.submit", submission)
    except CoreRefusal as refusal:
        return _core_refusal(run, uid, sender, request, refusal, message)
    return _applied(run, uid, sender, request, reply.answer_id, recovered=False)


def _applied(
    run: _Poll,
    uid: str,
    sender: str,
    request: dict[str, Any],
    answer_id: str,
    *,
    recovered: bool,
) -> dict[str, Any]:
    request_id = str(request.get("id", ""))
    current = _show_quietly(run.deps, request_id) or {**request, "state": "answered"}
    task = _task(run.deps, str(request.get("task_id", "")))
    task_status = task.get("status")
    mail = _send(
        run,
        render.confirmation_mail(
            run.config,
            sender,
            current,
            answer_id,
            str(task_status) if task_status else None,
        ),
    )
    _file(run, uid)
    outcome = "applied_earlier" if recovered else "applied"
    return _result(uid, outcome, request_id, sender, answer=answer_id, mail=mail)


def _core_refusal(
    run: _Poll,
    uid: str,
    sender: str,
    request: dict[str, Any],
    refusal: CoreRefusal,
    message: inbound.Inbound,
) -> dict[str, Any]:
    request_id = str(request.get("id", ""))
    code = refusal.code
    if code in SILENT_REFUSALS:
        return _drop(run, uid, sender, code, message)
    if code not in MAILED_REFUSALS:
        raise _Halt(
            ToolError(
                "submit_refused",
                f"core refused the submission for {request_id} with {code}; the channel "
                "needs operator attention and the reply stays unseen",
                detail={"refusal": code},
            )
        )
    if code == "answer_not_allowed":
        mail = _parse_refusal(run, sender, request, _core_reason(refusal.message))
    else:
        recipient = run.config.notify_to if code == "token_consumed" else sender
        detail = _superseded_detail(run.deps, request_id) if code == "request_superseded" else ""
        mail = _rate_limited_refusal(run, request, recipient, code, detail)
    _file(run, uid)
    return _result(uid, "refused", request_id, sender, refusal=code, mail=mail)


def _parse_refusal(run: _Poll, sender: str, request: dict[str, Any], reason: str) -> str:
    """A re-listing refusal mail, at most three per request (loop guard) and one an hour."""
    request_id = str(request.get("id", ""))
    entry = run.state["parse_refusals"].get(request_id) or {}
    count = int(entry.get("count", 0)) + 1
    run.state["parse_refusals"][request_id] = {"count": count, "at": run.now.isoformat()}
    if count > PARSE_REFUSAL_MAILS:
        return "loop_guard"
    return _rate_limited_refusal(run, request, sender, "answer_not_allowed", reason)


def _rate_limited_refusal(
    run: _Poll, request: dict[str, Any], to: str, code: str, detail: str
) -> str:
    request_id = str(request.get("id", ""))
    last = run.state["refusal_mail"].get(request_id)
    if isinstance(last, dict):
        try:
            then = datetime.fromisoformat(str(last.get("at")))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        except ValueError:
            then = None
        if then is not None and run.now - then < REFUSAL_MAIL_INTERVAL:
            return "rate_limited"
    mail = _send(run, render.refusal_mail(run.config, to, request, code, detail))
    if mail == "sent":
        run.state["refusal_mail"][request_id] = {"at": run.now.isoformat()}
    return mail


def _superseded_detail(deps: Deps, request_id: str) -> str:
    current = _show_quietly(deps, request_id)
    if current is None:
        return "The task or its request no longer exists."
    resolution = current.get("resolution")
    if current.get("state") == "answered" and isinstance(resolution, dict):
        return (
            f"It was already answered ({resolution.get('answer_id', '?')}) by "  # pyright: ignore[reportUnknownMemberType]
            f"{resolution.get('answered_by', 'someone')}."  # pyright: ignore[reportUnknownMemberType]
        )
    return f"The request now reads {current.get('state', 'superseded')}."


def _core_reason(message: str) -> str:
    """Core's own wording, without the code prefix and the 'nothing was applied' tail."""
    text = message.split("answer_not_allowed:", 1)[-1]
    return redact.text(text.split(";", 1)[0].strip())


def _drop(
    run: _Poll,
    uid: str,
    sender: str | None,
    reason: str,
    message: inbound.Inbound | None,
) -> dict[str, Any]:
    """Leave the mail in the inbox, seen, with no reply and no core submission."""
    verdicts = message.verdicts.summary() if message is not None else {}
    shown = render.line(sender, 120) if sender else None
    record = {"at": run.now.isoformat(), "sender": shown, "reason": reason, **verdicts}
    record_drop(run.state, record)
    _log({"event": "dropped", "uid": uid, **record})
    run.mailbox.mark_seen(uid)
    return _result(uid, "dropped", None, shown, reason=reason)


def _file(run: _Poll, uid: str) -> None:
    run.mailbox.mark_seen(uid)
    run.mailbox.file_processed(uid)


def _send(run: _Poll, message: Any) -> str:
    try:
        run.outbox.send(message)
    except TransportError as error:
        run.mail_failures += 1
        _log({"event": "reply_mail_failed", "error": str(error)})
        return "failed"
    return "sent"


def _result(
    uid: str, outcome: str, request_id: str | None, sender: str | None, **extra: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {"uid": uid, "outcome": outcome}
    if request_id:
        result["request_id"] = request_id
    if sender:
        result["sender"] = render.line(sender, 120)
    result.update(extra)
    return result


def _counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"applied": 0, "refused": 0, "dropped": 0}
    for result in results:
        outcome = str(result["outcome"])
        if outcome.startswith("applied"):
            counts["applied"] += 1
        elif outcome.startswith("refused"):
            counts["refused"] += 1
        else:
            counts["dropped"] += 1
    return counts


# --- parse_reply --------------------------------------------------------------------------


def parse_reply(
    tool_input: dict[str, Any], _context: dict[str, Any], _deps: Deps
) -> dict[str, Any]:
    """Apply the reply grammar to a text against printed answers; reads and sends nothing."""
    text = tool_input.get("text")
    answers = tool_input.get("answers")
    if not isinstance(text, str) or len(text) > 20000:
        raise ToolError("invalid_argument", "text must be a string of at most 20000 characters")
    if (
        not isinstance(answers, list)
        or not 1 <= len(answers) <= 20  # pyright: ignore[reportUnknownArgumentType]
        or not all(isinstance(answer, str) and answer for answer in answers)  # pyright: ignore[reportUnknownVariableType]
    ):
        raise ToolError("invalid_argument", "answers must list 1 to 20 non-empty answer ids")
    reply = grammar.parse(text, [str(answer) for answer in answers])  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return {
        "outcome": reply.outcome,
        "answer_id": reply.answer_id,
        "note": reply.note,
        "reason": reply.reason,
    }


# --- shared -------------------------------------------------------------------------------


def _config(context: dict[str, Any], secrets: tuple[str, ...]) -> Config:
    try:
        return load(context, secrets=secrets)
    except ConfigError as error:
        detail = {"missing": error.missing} if error.missing else None
        raise ToolError("not_configured", str(error), detail=detail) from None


def _store(deps: Deps) -> StateStore:
    if deps.state_dir is None:
        raise ToolError("launcher_env", "ORBIT_PLUGIN_STATE is not set; run through Orbit")
    return StateStore(deps.state_dir)


class _locked:
    def __init__(self, store: StateStore, tool: str) -> None:
        self._context = store.locked(tool)

    def __enter__(self) -> None:
        try:
            self._context.__enter__()
        except StateError as error:
            raise ToolError(error.code, error.message, retryable=True) from None

    def __exit__(self, *exc: Any) -> None:
        self._context.__exit__(*exc)


def _load(store: StateStore) -> dict[str, Any]:
    try:
        return store.load()
    except StateError as error:
        raise ToolError(error.code, error.message) from None


def _save(store: StateStore, state: dict[str, Any], now: datetime) -> None:
    try:
        store.save(state, now)
    except StateError as error:
        raise ToolError(error.code, error.message) from None


def _call(deps: Deps, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A read callback; any failure is a retryable tool error."""
    try:
        return deps.orbit.call(tool, payload)
    except CoreRefusal as refusal:
        raise ToolError(
            "orbit_refused", f"{tool} refused: {refusal.code}", detail={"refusal": refusal.code}
        ) from None
    except CallbackError as error:
        raise ToolError("orbit_callback_failed", error.message, retryable=True) from None


def _call_raw(deps: Deps, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A callback whose core refusal the caller handles; other failures halt the poll."""
    try:
        return deps.orbit.call(tool, payload)
    except CallbackError as error:
        outcome = "may have been applied" if error.unknown_outcome else "was not judged"
        raise _Halt(
            ToolError(
                "orbit_callback_failed",
                f"{tool} {outcome}: {error.message}; the reply stays unseen and is retried",
                retryable=True,
            )
        ) from None


def _show(deps: Deps, request_id: str) -> dict[str, Any]:
    request = _call_raw(deps, "orbit.decision.show", {"id": request_id})
    if request.get("id") != request_id or not isinstance(request.get("answers"), list):
        raise _Halt(
            ToolError("orbit_callback_failed", f"orbit.decision.show returned no {request_id}")
        )
    return request


def _show_quietly(deps: Deps, request_id: str) -> dict[str, Any] | None:
    try:
        return deps.orbit.call("orbit.decision.show", {"id": request_id})
    except (CoreRefusal, CallbackError):
        return None


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]  # pyright: ignore[reportUnknownVariableType]


def _request_ids(value: Any) -> set[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(item, str) and REQUEST_ID.fullmatch(item)
        for item in value  # pyright: ignore[reportUnknownVariableType]
    ):
        raise ToolError("invalid_argument", "request_ids must list decision request ids (dr-…)")
    return {str(item) for item in value}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _run_record(now: datetime, **fields: Any) -> dict[str, Any]:
    return {"at": now.isoformat(), **fields}


def _error_record(now: datetime, tool: str, code: str, message: str) -> dict[str, Any]:
    """The last error as plugin state keeps it; a server's text is scrubbed before it lands."""
    text = redact.text(message)[:300]
    return {"at": now.isoformat(), "tool": tool, "code": code, "message": text}


def _log(fields: dict[str, Any]) -> None:
    line = json.dumps({"plugin": "decision-mail", **fields}, sort_keys=True)
    print(redact.text(line), file=sys.stderr)
