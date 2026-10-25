"""Tool handlers. Each takes validated input, the call context and the transport
dependencies, and returns output.

The plugin is a transport: `notify` mails open decision requests, `poll` reads replies,
authenticates them (design §4 layers 4–5) and hands each answer to
`orbit.decision.submit`, where core re-checks everything and applies it. Nothing here moves
a task.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from decision_mail import __version__, grammar, inbound, redact, render, schedule
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
from decision_mail.state import (
    StateError,
    StateStore,
    empty,
    queue_confirmation,
    record_drop,
    record_event,
    report,
)
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

__all__ = [
    "SECRET_NAMES",
    "Deps",
    "activity",
    "default_deps",
    "digest",
    "notify",
    "parse_reply",
    "poll",
    "status",
]

REQUEST_ID = re.compile(r"dr-[0-9a-f]{16}")
# Items one digest mail lists at most; the rest wait for the next digest.
DIGEST_MAX_ITEMS = 50
# Task fields the digest reads for its summary and pilot state.
DIGEST_TASK_FIELDS = ["id", "title", "description", "status", "readiness", "pilot_warnings"]
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
    blank = report(empty())
    snapshot: dict[str, Any] = {
        "last_notify": None,
        "last_poll": None,
        "last_digest": None,
        "last_error": None,
        "pending_confirmations": 0,
        "notified_requests": 0,
        "recent_drops": [],
        "counters": blank["counters"],
        "events": blank["events"],
        "bound": blank["bound"],
    }
    if deps.state_dir is None:
        return snapshot
    try:
        state = StateStore(deps.state_dir).load()
    except StateError as error:
        snapshot["last_error"] = {"tool": "status", "code": error.code, "message": error.message}
        return snapshot
    for key in ("last_notify", "last_poll", "last_digest", "last_error"):
        snapshot[key] = state[key]
    snapshot["notified_requests"] = len(state["notified"])
    snapshot["pending_confirmations"] = len(state["pending_confirmations"])
    snapshot["recent_drops"] = state["drops"]
    observed = report(state)
    snapshot["counters"] = observed["counters"]
    snapshot["events"] = observed["events"]
    snapshot["bound"] = observed["bound"]
    return snapshot


def activity(_input: dict[str, Any], _context: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Counters and recent events. Reads plugin state and nothing else; writes nothing."""
    if deps.state_dir is None:
        return report(empty())
    try:
        state = StateStore(deps.state_dir).load()
    except StateError as error:
        output = report(empty())
        output["error"] = {"code": error.code, "message": error.message}
        return output
    return report(state)


# --- notify -------------------------------------------------------------------------------


def notify(tool_input: dict[str, Any], context: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Mail the open decision requests the batching mode lets go out on their own.

    `immediate` mails every request not yet mailed; `daily` only the `block_triage` ones (the
    other gates wait for the digest); `off` none. A send inside the quiet hours fails
    retryably, so the trigger keeps the request un-notified until the window ends.
    """
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
        due: list[dict[str, Any]] = []
        for request in requests:
            request_id = str(request.get("id", ""))
            reason = _unsent_reason(config, request, state, now, resend)
            if reason is None:
                due.append(request)
            else:
                skipped.append({"request_id": request_id, "reason": reason})
        until = schedule.deferred_until(config.quiet_hours, now)
        if due and until is not None:
            state["last_notify"] = _run_record(
                now, sent=0, deferred=len(due), until=until.isoformat()
            )
            _save(store, state, now)
            raise ToolError(
                "quiet_hours",
                f"{len(due)} request mail(s) wait for the quiet hours to end at "
                f"{until.isoformat()}; nothing was sent",
                retryable=True,
                detail={"deferred": len(due), "until": until.isoformat()},
            )
        with Outbox(config, smtp_password, deps.smtp) as outbox:
            for request in due:
                request_id = str(request.get("id", ""))
                task_id = str(request.get("task_id", ""))
                expires_at = str(request.get("expires_at", ""))
                try:
                    token = mint(key, request_id, task_id, config.key_epoch, expires_at)
                    expiry = datetime.fromtimestamp(unix_seconds(expires_at), UTC)
                except TokenError as error:
                    raise ToolError("token_unmintable", str(error)) from None
                message = render.request_mail(config, request, _task(deps, task_id), token, now=now)
                try:
                    outbox.send(message)
                except TransportError as error:
                    state["last_error"] = _error_record(now, "notify", "smtp_failed", str(error))
                    state["last_notify"] = _run_record(now, sent=len(sent), failed=request_id)
                    _observe(
                        state,
                        kind="transport",
                        reason="smtp",
                        at=now.isoformat(),
                        request_id=request_id,
                    )
                    _save(store, state, now)
                    raise ToolError(
                        "smtp_failed",
                        f"sending the mail for {request_id} failed: {error}",
                        retryable=True,
                        detail={"sent": len(sent), "failed_request": request_id},
                    ) from None
                _mark_notified(state, request_id, task_id, expiry, config.key_epoch, now)
                _save(store, state, now)
                sent.append(
                    {"request_id": request_id, "task_id": task_id, "gate": request.get("gate")}
                )
        state["last_notify"] = _run_record(now, sent=len(sent), skipped=len(skipped))
        _save(store, state, now)
    return {"sent": sent, "skipped": skipped}


def _unsent_reason(
    config: Config, request: dict[str, Any], state: dict[str, Any], now: datetime, resend: bool
) -> str | None:
    """Why `notify` does not mail this request now, or None when it should."""
    request_id = str(request.get("id", ""))
    if request.get("state") != "open":
        return "not_open"
    if config.digest_mode == "off":
        return "mode_off"
    if not schedule.mails_individually(config.digest_mode, str(request.get("gate", ""))):
        return "held_for_digest"
    if request_id in state["notified"] and not resend:
        return "already_notified"
    if _expired(request, now):
        return "expired"
    return None


def _expired(request: dict[str, Any], now: datetime) -> bool:
    """Whether the request's token is already dead at `now`; an unreadable stamp is not."""
    try:
        return unix_seconds(str(request.get("expires_at", ""))) <= int(now.timestamp())
    except TokenError:
        return False


def _mark_notified(
    state: dict[str, Any],
    request_id: str,
    task_id: str,
    expiry: datetime,
    epoch: int,
    now: datetime,
) -> None:
    state["notified"][request_id] = {
        "at": now.isoformat(),
        "task_id": task_id,
        "expires_at": expiry.isoformat(),
        "epoch": epoch,
    }


def _task(deps: Deps, task_id: str, fields: list[str] | None = None) -> dict[str, Any]:
    """The task's title and status for the mail; a failed read still mails the request."""
    payload: dict[str, Any] = {"id": task_id}
    if fields:
        payload["fields"] = fields
    try:
        return _call(deps, "orbit.task.show", payload)
    except ToolError:
        return {}


# --- digest -------------------------------------------------------------------------------


def digest(_input: dict[str, Any], context: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Send one digest mail: proposed tasks, requests held for it, reminders, confirmations.

    Core's `orbit.decision.digest` raises the missing approval requests and returns the
    digest set, so every request raised here is timed from this send. Nothing to list (and no
    confirmations owed) sends no mail.
    """
    config = _config(context, ("smtp_password", "channel_key"))
    if config.digest_mode == "off":
        return _digest_output(None, [], 0, 0, reason="mode_off")
    store = _store(deps)
    key = secret(context, "channel_key") or ""
    now = deps.now()
    until = schedule.deferred_until(config.quiet_hours, now)
    if until is not None:
        raise ToolError(
            "quiet_hours",
            f"the digest waits for the quiet hours to end at {until.isoformat()}; nothing was "
            "raised or sent",
            retryable=True,
            detail={"until": until.isoformat()},
        )
    with _locked(store, "digest"):
        state = _load(store)
        core = _call(deps, "orbit.decision.digest", {"channel": config.channel})
        listing = _call(deps, "orbit.decision.list", {})
        raised = len(_records(core.get("raised")))
        picked = _digest_requests(config, core, listing, state, now)
        omitted = max(0, len(picked) - DIGEST_MAX_ITEMS)
        items = [
            _digest_item(deps, config, key, number, section, request)
            for number, (section, request) in enumerate(picked[:DIGEST_MAX_ITEMS], start=1)
        ]
        owed = [_confirmation(entry) for entry in state["pending_confirmations"]]
        if not items and not owed:
            state["last_digest"] = _run_record(now, sent=False, items=0, raised=raised)
            _save(store, state, now)
            return _digest_output(None, [], 0, raised, reason="nothing_to_list")
        digest_id = (
            "dg-"
            + hashlib.sha256(
                "|".join([now.isoformat(), *(str(i.request.get("id")) for i in items)]).encode()
            ).hexdigest()[:16]
        )
        message = render.digest_mail(config, digest_id, items, owed, now, omitted=omitted)
        with Outbox(config, secret(context, "smtp_password") or "", deps.smtp) as outbox:
            try:
                outbox.send(message)
            except TransportError as error:
                state["last_error"] = _error_record(now, "digest", "smtp_failed", str(error))
                state["last_digest"] = _run_record(now, sent=False, failed=True, raised=raised)
                _observe(state, kind="transport", reason="smtp", at=now.isoformat())
                _save(store, state, now)
                raise ToolError(
                    "smtp_failed",
                    f"sending the digest failed: {error}",
                    retryable=True,
                    detail={"items": len(items)},
                ) from None
        for item in items:
            if item.token is None:
                continue
            request = item.request
            expiry = datetime.fromtimestamp(unix_seconds(str(request.get("expires_at"))), UTC)
            _mark_notified(
                state,
                str(request.get("id")),
                str(request.get("task_id")),
                expiry,
                config.key_epoch,
                now,
            )
        state["pending_confirmations"] = []
        state["last_digest"] = _run_record(
            now, sent=True, id=digest_id, items=len(items), confirmations=len(owed), raised=raised
        )
        _save(store, state, now)
    return _digest_output(digest_id, items, len(owed), raised, omitted=omitted)


def _digest_output(
    digest_id: str | None,
    items: list[render.DigestItem],
    confirmations: int,
    raised: int,
    *,
    reason: str | None = None,
    omitted: int = 0,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "sent": digest_id is not None,
        "digest_id": digest_id,
        "items": [
            {
                "number": item.number,
                "request_id": str(item.request.get("id", "")),
                "task_id": str(item.request.get("task_id", "")),
                "gate": str(item.request.get("gate", "")),
                "section": item.section,
            }
            for item in items
        ],
        "confirmations": confirmations,
        "raised": raised,
        "omitted": omitted,
    }
    if reason is not None:
        output["reason"] = reason
    return output


def _digest_requests(
    config: Config,
    core: dict[str, Any],
    listing: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
) -> list[tuple[str, dict[str, Any]]]:
    """The open requests the digest lists, each once, as `(section, request)`.

    Proposed approvals come first, then reminders (requests expiring within core's window, or
    mailed under an older key epoch and so needing a fresh token), then the other requests the
    digest holds: review approvals, and in `daily` mode every gate but `block_triage` that no
    mail has carried yet.
    """
    live = {
        str(request.get("id")): request
        for request in _records(listing.get("requests"))
        if request.get("state") == "open" and not _expired(request, now)
    }
    for request in _records(core.get("requests")) + _records(core.get("expiring")):
        if request.get("state") == "open" and not _expired(request, now):
            live[str(request.get("id"))] = request
    notified: dict[str, Any] = state["notified"]

    def rekeyed(request: dict[str, Any]) -> bool:
        epoch = (notified.get(str(request.get("id"))) or {}).get("epoch")
        return isinstance(epoch, int) and epoch != config.key_epoch

    expiring = {str(request.get("id")) for request in _records(core.get("expiring"))}
    sections: dict[str, list[dict[str, Any]]] = {"proposed": [], "reminder": [], "held": []}
    for request_id, request in live.items():
        gate = str(request.get("gate", ""))
        if gate == "proposed_approval":
            sections["proposed"].append(request)
        elif request_id in expiring or rekeyed(request):
            sections["reminder"].append(request)
        elif gate == "review_approval" or (
            config.digest_mode == "daily"
            and gate != schedule.IMMEDIATE_GATE
            and request_id not in notified
        ):
            sections["held"].append(request)
    for requests in sections.values():
        requests.sort(key=lambda r: (str(r.get("raised_at", "")), str(r.get("id", ""))))
    return [(name, request) for name, requests in sections.items() for request in requests]


def _digest_item(
    deps: Deps,
    config: Config,
    key: str,
    number: int,
    section: str,
    request: dict[str, Any],
) -> render.DigestItem:
    request_id = str(request.get("id", ""))
    task_id = str(request.get("task_id", ""))
    try:
        token = mint(key, request_id, task_id, config.key_epoch, str(request.get("expires_at", "")))
    except TokenError as error:
        raise ToolError("token_unmintable", str(error)) from None
    return render.DigestItem(
        number, section, request, _task(deps, task_id, DIGEST_TASK_FIELDS), token
    )


def _confirmation(entry: dict[str, Any]) -> render.Confirmation:
    return render.Confirmation(
        request_id=str(entry["request_id"]),
        task_id=str(entry["task_id"]),
        title=str(entry["title"]),
        answer=str(entry["answer"]),
        label=str(entry["label"]),
        task_status=str(entry["task_status"]),
    )


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
            _observe(state, kind="transport", reason="imap", at=now.isoformat())
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
    if message.digest_ids:
        return _handle_digest(run, uid, sender, message)
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


# --- digest replies -----------------------------------------------------------------------


@dataclass
class _Choice:
    """One item of a digest reply, read against core's record of its request."""

    number: int
    request_id: str
    token: str
    answer: str
    request: dict[str, Any]
    earlier: bool = False


def _handle_digest(run: _Poll, uid: str, sender: str, message: inbound.Inbound) -> dict[str, Any]:
    """Apply a reply to a digest mail item by item (design §6, §7).

    The whole reply is judged before anything is submitted: an ambiguous, out-of-range or
    unparseable reply, or an item that does not take the named word, applies nothing, spends
    no token and gets one re-listing mail. Applied items are not mailed one by one; they are
    queued and confirmed together in the next digest.
    """
    if len(message.digest_ids) != 1 or message.tokens or message.request_ids:
        return _drop(run, uid, sender, "malformed_digest", message)
    (digest_id,) = message.digest_ids
    numbered: dict[int, tuple[str, str]] = {}
    for number, request_id, token in message.digest_items:
        if number in numbered:
            return _drop(run, uid, sender, "malformed_digest", message)
        numbered[number] = (request_id, token)
    if not numbered:
        reason = (
            "the reply carries none of the digest's orbit-item lines; reply to the digest "
            "mail itself and keep its quoted text"
        )
        return _digest_refused(run, uid, sender, digest_id, reason, numbered, "refused_unparseable")
    if message.text is None:
        reply = grammar.DigestReply("unparseable", (), "", "the reply has no readable text part")
    else:
        reply = grammar.parse_digest(message.text, set(numbered))
    if reply.outcome != "answers":
        outcome = "refused_not_allowed" if reply.outcome == "not_allowed" else "refused_unparseable"
        return _digest_refused(run, uid, sender, digest_id, reply.reason, numbered, outcome)

    chosen: list[_Choice] = []
    failed: list[tuple[int, str, str]] = []
    for number, verb in reply.choices:
        request_id, token = numbered[number]
        try:
            request = _show(run.deps, request_id)
        except CoreRefusal as refusal:
            failed.append((number, request_id, refusal.code))
            continue
        earlier = _applied_by(request, token)
        answer = _answer_for(request, verb)
        if answer is None and not earlier:
            reason = (
                f"item {number} does not take `{verb}`; the words it takes are listed in the digest"
            )
            return _digest_refused(
                run, uid, sender, digest_id, reason, numbered, "refused_not_allowed"
            )
        resolved = str(request["resolution"].get("answer_id", "")) if earlier else answer
        chosen.append(_Choice(number, request_id, token, resolved or "", request, earlier))

    applied: list[dict[str, Any]] = []
    refused = list(failed)
    for choice in chosen:
        if not choice.earlier:
            submission: dict[str, Any] = {
                "id": choice.request_id,
                "channel": run.config.channel,
                "answer": choice.answer,
                "token": choice.token,
                "sender": sender,
                "auth_results": message.verdicts.header,
            }
            if reply.note:
                submission["note"] = reply.note
            try:
                _call_raw(run.deps, "orbit.decision.submit", submission)
            except CoreRefusal as refusal:
                code = refusal.code
                _observe(
                    run.state,
                    kind="refusal",
                    reason=code,
                    at=run.now.isoformat(),
                    sender=sender,
                    request_id=choice.request_id,
                )
                if code in SILENT_REFUSALS and not applied and not refused:
                    return _drop(run, uid, sender, code, message, counted=False)
                if code not in MAILED_REFUSALS and code not in SILENT_REFUSALS:
                    raise _Halt(
                        ToolError(
                            "submit_refused",
                            f"core refused the submission for {choice.request_id} with {code}; "
                            "the channel needs operator attention and the reply stays unseen",
                            detail={"refusal": code},
                        )
                    ) from None
                refused.append((choice.number, choice.request_id, code))
                continue
        _queue_confirmation(run, choice)
        applied.append(
            {
                "number": choice.number,
                "request_id": choice.request_id,
                "outcome": "applied_earlier" if choice.earlier else "applied",
                "answer": choice.answer,
            }
        )
    mail = _digest_item_refusals(run, sender, digest_id, len(applied), refused, chosen)
    _file(run, uid)
    items = applied + [
        {"number": number, "request_id": rid, "outcome": "refused", "refusal": code}
        for number, rid, code in refused
    ]
    items.sort(key=lambda item: int(item["number"]))
    outcome = "applied" if not refused else ("applied_partial" if applied else "refused")
    extra: dict[str, Any] = {"digest_id": digest_id, "items": items}
    if mail:
        extra["mail"] = mail
    return _result(uid, outcome, None, sender, **extra)


def _applied_by(request: dict[str, Any], token: str) -> bool:
    """Whether this reply's own earlier submission already landed (see `_handle`)."""
    resolution = request.get("resolution")
    return (
        request.get("state") == "answered"
        and isinstance(resolution, dict)
        and fingerprint(token) in str(resolution.get("provenance", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    )


def _answer_for(request: dict[str, Any], word: str) -> str | None:
    """The request's answer id that the digest word names, or None when it has none."""
    for answer in _records(request.get("answers")):
        if grammar.normalize(str(answer.get("id", ""))) == word:
            return str(answer.get("id"))
    return None


def _queue_confirmation(run: _Poll, choice: _Choice) -> None:
    current = _show_quietly(run.deps, choice.request_id) or {**choice.request, "state": "answered"}
    task_id = str(choice.request.get("task_id", ""))
    task = _task(run.deps, task_id)
    label = next(
        (
            str(answer.get("label", ""))
            for answer in _records(current.get("answers"))
            if answer.get("id") == choice.answer
        ),
        choice.answer,
    )
    queue_confirmation(
        run.state,
        {
            "request_id": choice.request_id,
            "task_id": render.line(task_id, 64),
            "title": render.line(task.get("title") or "(untitled task)", 120),
            "answer": render.line(choice.answer, 64),
            "label": render.line(label or choice.answer),
            "task_status": render.line(task.get("status") or "", 40),
            "at": run.now.isoformat(),
        },
    )


def _digest_refused(
    run: _Poll,
    uid: str,
    sender: str,
    digest_id: str,
    reason: str,
    numbered: dict[int, tuple[str, str]],
    outcome: str,
) -> dict[str, Any]:
    """Refuse a whole digest reply: a re-listing mail (loop guard and hourly rate limit)."""
    entry = run.state["parse_refusals"].get(digest_id) or {}
    count = int(entry.get("count", 0)) + 1
    run.state["parse_refusals"][digest_id] = {"count": count, "at": run.now.isoformat()}
    if count > PARSE_REFUSAL_MAILS:
        _observe(
            run.state, kind="loop_guard", reason="loop_guard", at=run.now.isoformat(), sender=sender
        )
        mail = "loop_guard"
    else:
        mail = _rate_limited(
            run,
            digest_id,
            sender,
            lambda: render.digest_refusal_mail(
                run.config, sender, digest_id, reason, _relisted(run, numbered), run.now
            ),
        )
    run.mailbox.mark_seen(uid, flagged=True)
    return _result(uid, outcome, None, sender, digest_id=digest_id, reason=reason, mail=mail)


def _relisted(run: _Poll, numbered: dict[int, tuple[str, str]]) -> list[render.DigestItem]:
    """The digest's items as the reply's own `orbit-item:` lines name them, read afresh."""
    items: list[render.DigestItem] = []
    for number, (request_id, token) in sorted(numbered.items())[:DIGEST_MAX_ITEMS]:
        request = _show_quietly(run.deps, request_id) or {"id": request_id, "state": "unknown"}
        task = _task(run.deps, str(request.get("task_id", "")), DIGEST_TASK_FIELDS)
        open_ = request.get("state") == "open"
        items.append(render.DigestItem(number, "listed", request, task, token if open_ else None))
    return items


def _digest_item_refusals(
    run: _Poll,
    sender: str,
    digest_id: str,
    applied: int,
    refused: list[tuple[int, str, str]],
    chosen: list[_Choice],
) -> str | None:
    """One mail for the items core refused; None when every item landed."""
    if not refused:
        return None
    tasks = {choice.request_id: str(choice.request.get("task_id", "")) for choice in chosen}
    rows = [(number, tasks.get(rid, "?"), rid, code) for number, rid, code in refused]
    to = run.config.notify_to if any(code == "token_consumed" for _, _, code in refused) else sender
    return _rate_limited(
        run,
        digest_id,
        to,
        lambda: render.digest_item_refusals_mail(run.config, to, digest_id, applied, rows),
    )


def _rate_limited(run: _Poll, key: str, to: str, build: Callable[[], Any]) -> str:
    """Send `build()` unless a refusal mail for `key` went out within the last hour."""
    last = run.state["refusal_mail"].get(key)
    if isinstance(last, dict):
        try:
            then = datetime.fromisoformat(str(last.get("at")))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        except ValueError:
            then = None
        if then is not None and run.now - then < REFUSAL_MAIL_INTERVAL:
            return "rate_limited"
    mail = _send(run, build(), sender=to, request_id=key)
    if mail == "sent":
        run.state["refusal_mail"][key] = {"at": run.now.isoformat()}
    return mail


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
        sender=sender,
        request_id=request_id,
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
    _observe(
        run.state,
        kind="refusal",
        reason=code,
        at=run.now.isoformat(),
        sender=sender,
        request_id=request_id,
    )
    if code in SILENT_REFUSALS:
        return _drop(run, uid, sender, code, message, counted=False)
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
        _observe(
            run.state,
            kind="loop_guard",
            reason="loop_guard",
            at=run.now.isoformat(),
            sender=sender,
            request_id=request_id,
        )
        return "loop_guard"
    return _rate_limited_refusal(run, request, sender, "answer_not_allowed", reason)


def _rate_limited_refusal(
    run: _Poll, request: dict[str, Any], to: str, code: str, detail: str
) -> str:
    return _rate_limited(
        run,
        str(request.get("id", "")),
        to,
        lambda: render.refusal_mail(run.config, to, request, code, detail),
    )


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
    *,
    counted: bool = True,
) -> dict[str, Any]:
    """Leave the mail in the inbox, seen, with no reply and no core submission."""
    verdicts = message.verdicts.summary() if message is not None else {}
    shown = render.line(sender, 120) if sender else None
    record = {"at": run.now.isoformat(), "sender": shown, "reason": reason, **verdicts}
    record_drop(run.state, record)
    if counted:
        _observe(
            run.state,
            kind="drop",
            reason=reason,
            at=run.now.isoformat(),
            sender=sender,
            request_id=_one_request_id(message),
        )
    _log({"event": "dropped", "uid": uid, **record})
    run.mailbox.mark_seen(uid)
    return _result(uid, "dropped", None, shown, reason=reason)


def _file(run: _Poll, uid: str) -> None:
    run.mailbox.mark_seen(uid)
    run.mailbox.file_processed(uid)


def _send(
    run: _Poll, message: Any, *, sender: str | None = None, request_id: str | None = None
) -> str:
    try:
        run.outbox.send(message)
    except TransportError as error:
        run.mail_failures += 1
        _observe(
            run.state,
            kind="transport",
            reason="smtp",
            at=run.now.isoformat(),
            sender=sender,
            request_id=request_id,
        )
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


def _observe(
    state: dict[str, Any],
    *,
    kind: str,
    reason: str,
    at: str,
    sender: str | None = None,
    request_id: str | None = None,
) -> None:
    """Record one counter and one event. The sender is an address, never a body or a header."""
    shown = redact.text(render.line(sender, 120)) if sender else None
    record_event(
        state,
        kind=kind,
        reason=reason,
        at=at,
        sender=shown or None,
        request_id=request_id,
    )


def _one_request_id(message: inbound.Inbound | None) -> str | None:
    if message is None:
        return None
    found = [item for item in message.request_ids if REQUEST_ID.fullmatch(item)]
    if len(found) != 1:
        return None
    return found[0]


def _log(fields: dict[str, Any]) -> None:
    line = json.dumps({"plugin": "decision-mail", **fields}, sort_keys=True)
    print(redact.text(line), file=sys.stderr)
