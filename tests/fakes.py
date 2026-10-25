"""Fakes for SMTP, IMAP and Orbit core. No test opens a socket or a real mailbox.

The SMTP and IMAP fakes stand in for `smtplib`/`imaplib` client objects, so the plugin's
transport code runs unchanged above them. `FakeCore` judges `orbit.decision.submit` the
way core does (`decision_channel.rs`), with its own token mint, and refuses with core's
message shape (`<kind>: <code>: …`).
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import hmac
import imaplib
import json
import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from decision_mail.orbit_api import CallbackError, CoreRefusal
from decision_mail.tools import Deps

KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
OPERATOR = "daniel@example.com"
MAILBOX = "orbit@example.org"
AUTHSERV = "mx.example.org"
SMTP_PASSWORD = "smtp-pass-fixture"
IMAP_PASSWORD = "imap-pass-fixture"


def core_token(key_hex: str, request: dict[str, Any], epoch: int) -> str:
    """The documented construction, computed independently of the plugin's mint."""
    expires = datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00"))
    message = f"{request['id']}|{request['task_id']}|{epoch}|{int(expires.timestamp())}"
    mac = hmac.new(bytes.fromhex(key_hex), message.encode(), hashlib.sha256).digest()
    return base64.b32encode(mac).decode().rstrip("=")[:26]


def config(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "address": MAILBOX,
        "notify_to": OPERATOR,
        "allowed_senders": [OPERATOR],
        "authserv_id": AUTHSERV,
        "smtp_host": "smtp.example.org",
        "imap_host": "imap.example.org",
        "key_epoch": 3,
        # The ORB-18304 tests mail every request as it is raised; digest tests choose a mode.
        "digest_mode": "immediate",
        "dashboard_url": "http://dk-server-2:7300/tasks/{task_id}",
    }
    values.update(overrides)
    return values


def secrets(
    smtp: str = SMTP_PASSWORD, imap: str = IMAP_PASSWORD, key: str = KEY_HEX
) -> dict[str, Any]:
    return {
        "smtp_password": {"value": smtp, "version": "v1"},
        "imap_password": {"value": imap, "version": "v1"},
        "channel_key": {"value": key, "version": "v1"},
    }


def envelope(tool: str, tool_input: dict[str, Any] | None = None, **context: Any) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "tool": f"decision-mail.{tool}",
            "input": tool_input or {},
            "context": context,
        }
    )


# --- SMTP ---------------------------------------------------------------------------------


@dataclass
class SmtpServer:
    password: str = SMTP_PASSWORD
    sent: list[EmailMessage] = field(default_factory=lambda: [])
    fail_send: Exception | None = None

    def client(self, _config: Any) -> SmtpClient:
        return SmtpClient(self)


class SmtpClient:
    def __init__(self, server: SmtpServer) -> None:
        self.server = server

    def login(self, user: str, password: str) -> None:
        if password != self.server.password:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed")

    def send_message(self, msg: EmailMessage) -> None:
        if self.server.fail_send is not None:
            raise self.server.fail_send
        self.server.sent.append(msg)

    def quit(self) -> None:
        pass


# --- IMAP ---------------------------------------------------------------------------------


@dataclass
class Stored:
    raw: bytes
    folder: str = "INBOX"
    flags: set[str] = field(default_factory=lambda: set())


@dataclass
class ImapServer:
    password: str = IMAP_PASSWORD
    messages: dict[int, Stored] = field(default_factory=lambda: {})
    folders: set[str] = field(default_factory=lambda: {"INBOX"})
    capabilities: tuple[str, ...] = ("IMAP4REV1", "MOVE", "UIDPLUS")
    fail_login: bool = False

    def deliver(self, raw: bytes) -> int:
        uid = max(self.messages, default=0) + 1
        self.messages[uid] = Stored(raw)
        return uid

    def in_folder(self, folder: str) -> list[int]:
        return [uid for uid, stored in self.messages.items() if stored.folder == folder]

    def client(self, _config: Any) -> ImapClient:
        return ImapClient(self)


def _unquote(folder: str) -> str:
    return folder[1:-1] if folder.startswith('"') and folder.endswith('"') else folder


class ImapClient:
    def __init__(self, server: ImapServer) -> None:
        self.server = server
        self.capabilities = server.capabilities
        self.selected: str | None = None

    def login(self, user: str, password: str) -> tuple[str, list[Any]]:
        if self.server.fail_login or password != self.server.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]:
        self.selected = _unquote(mailbox)
        return "OK", [str(len(self.server.in_folder(self.selected))).encode()]

    def create(self, mailbox: str) -> tuple[str, list[Any]]:
        name = _unquote(mailbox)
        if name in self.server.folders:
            return "NO", [b"[ALREADYEXISTS] Mailbox exists"]
        self.server.folders.add(name)
        return "OK", [b"CREATE completed"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        stored = self.server.messages
        if command == "SEARCH":
            uids = [
                str(uid)
                for uid in self.server.in_folder(self.selected or "")
                if "\\Seen" not in stored[uid].flags
            ]
            return "OK", [" ".join(uids).encode()]
        uid = int(args[0])
        message = stored.get(uid)
        if message is None or message.folder != self.selected:
            return "NO", [b"no such message"]
        if command == "FETCH":
            assert args[1] == "(BODY.PEEK[])", args
            header = f"{uid} (UID {uid} BODY[] {{{len(message.raw)}}}".encode()
            return "OK", [(header, message.raw), b")"]
        if command == "STORE":
            assert args[1] == "+FLAGS", args
            message.flags.update(args[2].strip("()").split())
            return "OK", [b"STORE completed"]
        if command in ("MOVE", "COPY"):
            folder = _unquote(args[1])
            if folder not in self.server.folders:
                return "NO", [b"[TRYCREATE] no such mailbox"]
            if command == "MOVE":
                message.folder = folder
            else:
                copy = Stored(message.raw, folder, set(message.flags))
                stored[max(stored) + 1] = copy
            return "OK", [b"done"]
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self) -> tuple[str, list[Any]]:
        return "BYE", [b"logging out"]


# --- Orbit core ---------------------------------------------------------------------------


@dataclass
class FakeCore:
    """In-memory decision requests and tasks, with core's submit checks."""

    key_hex: str = KEY_HEX
    epoch: int = 3
    previous_epoch: int | None = None
    channel: str = "email"
    revoked: bool = False
    allowed_senders: tuple[str, ...] = (OPERATOR,)
    requests: dict[str, dict[str, Any]] = field(default_factory=lambda: {})
    tasks: dict[str, dict[str, Any]] = field(default_factory=lambda: {})
    consumed: set[str] = field(default_factory=lambda: set())
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=lambda: [])
    fail: dict[str, CallbackError] = field(default_factory=lambda: {})
    refuse_submit: str | None = None
    # Core's clock for the digest's raise times and its 24-hour expiry window.
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 29, 9, 0, tzinfo=UTC))
    digest_enabled: bool = True

    def add(self, request: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        self.requests[request["id"]] = request
        self.tasks[request["task_id"]] = task
        return request

    def token(self, request_id: str, epoch: int | None = None) -> str:
        return core_token(self.key_hex, self.requests[request_id], epoch or self.epoch)

    def submits(self) -> list[dict[str, Any]]:
        return [payload for tool, payload in self.calls if tool == "orbit.decision.submit"]

    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, json.loads(json.dumps(payload))))
        if tool in self.fail:
            raise self.fail[tool]
        if tool == "orbit.decision.list":
            open_requests = [r for r in self.requests.values() if r["state"] == "open"]
            return {"requests": open_requests, "count": len(open_requests)}
        if tool == "orbit.decision.show":
            return dict(self._request(payload["id"]))
        if tool == "orbit.task.show":
            if payload["id"] not in self.tasks:
                raise CallbackError("task not found")
            return dict(self.tasks[payload["id"]])
        if tool == "orbit.decision.submit":
            return self._submit(payload)
        if tool == "orbit.decision.digest":
            return self._digest(payload)
        raise AssertionError(f"plugin called an ungranted tool {tool}")

    def _digest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """`orbit.decision.digest`: raise a proposed_approval per proposed task, return the set."""
        if payload["channel"] != self.channel:
            raise _refusal("policy denied", "channel_not_registered", "no such channel")
        if self.revoked:
            raise _refusal("policy denied", "channel_revoked", "the channel is revoked")
        if not self.digest_enabled:
            return {"requests": [], "raised": [], "expiring": [], "count": 0}
        raised: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        for task_id, task_record in list(self.tasks.items()):
            if task_record["status"] != "proposed":
                continue
            current = next(
                (
                    r
                    for r in self.requests.values()
                    if r["task_id"] == task_id
                    and r["gate"] == "proposed_approval"
                    and r["state"] == "open"
                ),
                None,
            )
            if current is None:
                current = approval_request(
                    f"dr-{len(self.requests) + 1:016x}",
                    task_id,
                    raised_at=self.now,
                )
                self.requests[current["id"]] = current
                raised.append(current)
            approvals.append(current)
        horizon = self.now + timedelta(hours=24)
        expiring = [
            r
            for r in self.requests.values()
            if r["state"] == "open"
            and datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00")) <= horizon
        ]
        requests = approvals + [r for r in expiring if r not in approvals]
        return {
            "requests": requests,
            "raised": raised,
            "expiring": expiring,
            "count": len(requests),
        }

    def _request(self, request_id: str) -> dict[str, Any]:
        if request_id not in self.requests:
            raise _refusal(
                "invalid input", "request_not_found", f"no decision request `{request_id}`"
            )
        return self.requests[request_id]

    def _submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.refuse_submit is not None:
            raise _refusal("policy denied", self.refuse_submit, "refused by the fixture")
        if payload["channel"] != self.channel:
            raise _refusal("policy denied", "channel_not_registered", "no such channel")
        if self.revoked:
            raise _refusal("policy denied", "channel_revoked", "the channel is revoked")
        sender = payload.get("sender")
        if not sender or sender.lower() not in self.allowed_senders:
            raise _refusal("policy denied", "sender_not_allowed", "sender not allowed")
        header = (payload.get("auth_results") or "").lower()
        if "dkim=pass" not in header or "spf=pass" not in header or "dmarc=fail" in header:
            raise _refusal("policy denied", "authentication_failed", "evidence failing")
        request = self._request(payload["id"])
        token = payload["token"].strip().upper()
        mark = hashlib.sha256(token.encode()).hexdigest()
        if token != core_token(self.key_hex, request, self.epoch):
            if self.previous_epoch is not None and token == core_token(
                self.key_hex, request, self.previous_epoch
            ):
                self.consumed.add(mark)
                raise _refusal("invalid input", "token_consumed", "minted under a rotated key")
            raise _refusal("policy denied", "token_invalid", "the token does not verify")
        if mark in self.consumed:
            raise _refusal("invalid input", "token_consumed", "already presented and judged")
        answer = payload["answer"]
        if answer not in [item["id"] for item in request["answers"]]:
            raise _refusal("invalid input", "answer_not_allowed", f"`{answer}` is not allowed")
        if answer == "evaluated" and not (payload.get("note") or "").strip():
            raise _refusal(
                "invalid input", "answer_not_allowed", "an `evaluated` verdict needs a note"
            )
        if request["state"] == "expired":
            self.consumed.add(mark)
            raise _refusal("invalid input", "request_expired", "the request expired")
        if request["state"] in ("answered", "superseded"):
            self.consumed.add(mark)
            raise _refusal("invalid input", "request_superseded", "no longer matches the hold")
        self.consumed.add(mark)
        request["state"] = "answered"
        request["resolution"] = {
            "request_id": request["id"],
            "answer_id": answer,
            "answered_by": "daniel",
            "provenance": f"decision-channel email (request {request['id']}, token sha256:{mark})",
            "note": payload.get("note"),
            "answered_at": "2026-10-26T09:00:00Z",
        }
        self.tasks[request["task_id"]]["status"] = "rejected" if answer == "reject" else "backlog"
        return dict(request)


def _refusal(kind: str, code: str, why: str) -> CoreRefusal:
    message = f"{kind}: {code}: {why}; nothing was applied"
    return CoreRefusal(code, message)


def pilot_request(request_id: str = "dr-0123456789abcdef", **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "id": request_id,
        "task_id": "ORB-42",
        "gate": "pilot_admission",
        "answers": [
            {"id": "approve_anyway", "label": "Approve anyway", "consequence": "Admit it."},
            {"id": "clear", "label": "Clear", "consequence": "The finding no longer applies."},
            {"id": "evaluated", "label": "Evaluated", "consequence": "Attach the evaluation."},
        ],
        "binding": {"kind": "pilot_operation", "operation_id": "op-1"},
        "raised_by": "task_pilot",
        "raised_at": "2026-10-28T12:00:00.123456789Z",
        "expires_at": "2026-10-31T12:00:00.123456789Z",
        "state": "open",
    }
    request.update(overrides)
    return request


def triage_request(request_id: str = "dr-0123456789abcdef", **overrides: Any) -> dict[str, Any]:
    request = pilot_request(request_id, gate="block_triage")
    request["answers"] = [
        {"id": "1", "label": "Drop criterion 2", "consequence": "The old API stays retired."},
        {"id": "2", "label": "Keep the API", "consequence": "The retirement is reverted."},
        {"id": "dismiss", "label": "Dismiss", "consequence": "Leave blocked."},
    ]
    request["binding"] = {"kind": "block_episode", "episode": "2026-10-28T11:00:00Z"}
    request.update(overrides)
    return request


def approval_request(
    request_id: str, task_id: str, *, raised_at: datetime, ttl_hours: int = 72, **overrides: Any
) -> dict[str, Any]:
    """A digest-raised proposed_approval request, as core raises it (answers approve/reject)."""
    request: dict[str, Any] = {
        "id": request_id,
        "task_id": task_id,
        "gate": "proposed_approval",
        "answers": [
            {"id": "approve", "label": "Approve", "consequence": "Promote it through task-pilot."},
            {"id": "reject", "label": "Reject", "consequence": "Reject the proposal."},
        ],
        "binding": {"kind": "task_revision", "revision": "spec-digest"},
        "raised_by": "digest",
        "raised_at": raised_at.isoformat().replace("+00:00", "Z"),
        "expires_at": (raised_at + timedelta(hours=ttl_hours)).isoformat().replace("+00:00", "Z"),
        "state": "open",
    }
    request.update(overrides)
    return request


def proposed_task(task_id: str, title: str, description: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": description,
        "status": "proposed",
        "readiness": {"ready": True, "gaps": []},
        "pilot_warnings": [],
    }
    record.update(overrides)
    return record


def task(status: str = "backlog") -> dict[str, Any]:
    return {"id": "ORB-42", "title": "Retire the old API", "status": status}


# --- inbound mail -------------------------------------------------------------------------


def stamp(dkim: str = "pass", spf: str = "pass", dmarc: str = "pass", **extra: str) -> str:
    domain = OPERATOR.split("@")[1]
    return (
        f"{extra.get('authserv', AUTHSERV)}; dkim={dkim} header.i=@{domain} header.s=s1 "
        f"header.b=abc; spf={spf} (example: domain of {OPERATOR} designates 192.0.2.1 as "
        f"permitted sender) smtp.mailfrom={OPERATOR}; dmarc={dmarc} (p=REJECT) "
        f"header.from={domain}"
    )


def reply(
    body: str,
    *,
    token: str | None,
    request_id: str | None = "dr-0123456789abcdef",
    sender: str = OPERATOR,
    stamps: tuple[str, ...] | None = None,
    attacker_stamps: tuple[str, ...] = (),
    body_token: bool = False,
    html_only: bool = False,
) -> bytes:
    """A reply as the provider delivers it: its own stamp on top, the sender's headers below."""
    message = EmailMessage(policy=email.policy.SMTP)
    message["Return-Path"] = f"<{sender}>"
    message["Delivered-To"] = MAILBOX
    for value in stamps if stamps is not None else (stamp(),):
        message["Authentication-Results"] = value
    # Headers the sender wrote come below the provider's stamp.
    for value in attacker_stamps:
        message["Authentication-Results"] = value
    message["From"] = f"Daniel <{sender}>"
    local, domain = MAILBOX.split("@")
    message["To"] = f"{local}+{token}@{domain}" if token else MAILBOX
    subject = "Re: [orbit] decision needed: Retire the old API (ORB-42)"
    message["Subject"] = f"{subject} [{request_id}]" if request_id else subject
    message["Message-ID"] = "<reply-1@example.com>"
    quoted = "\n\nOn Sat, Orbit wrote:\n"
    if body_token and token:
        quoted += f"> orbit-token: {token}\n"
    quoted += "> Orbit is holding ORB-42 for your decision.\n"
    if html_only:
        message.set_content(f"<p>{body}</p>", subtype="html")
    else:
        message.set_content(body + quoted)
    return message.as_bytes()


def digest_reply(
    body: str,
    digest_mail: str,
    *,
    digest_id: str,
    sender: str = OPERATOR,
    keep_lines: bool = True,
) -> bytes:
    """A reply to a digest mail: the answer on top, the original quoted below as clients do."""
    message = EmailMessage(policy=email.policy.SMTP)
    message["Return-Path"] = f"<{sender}>"
    message["Delivered-To"] = MAILBOX
    message["Authentication-Results"] = stamp()
    message["From"] = f"Daniel <{sender}>"
    message["To"] = MAILBOX
    message["Subject"] = f"Re: [orbit] decision digest 2026-10-29: 3 to decide [{digest_id}]"
    message["Message-ID"] = "<digest-reply-1@example.com>"
    original = digest_mail.splitlines()
    if not keep_lines:
        original = [line for line in original if not line.startswith("orbit-")]
    quoted = "\n".join(f"> {line}" for line in original)
    message.set_content(f"{body}\n\nOn Sat, Orbit wrote:\n{quoted}\n")
    return message.as_bytes()


def parse_sent(message: EmailMessage) -> EmailMessage:
    """A sent message as the recipient would parse it."""
    parsed = email.message_from_bytes(message.as_bytes(), policy=email.policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


def body_of(message: EmailMessage) -> str:
    part = parse_sent(message).get_body(preferencelist=("plain",))
    assert part is not None
    content = part.get_content()
    assert isinstance(content, str)
    return content


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 29, 9, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Rig:
    core: FakeCore
    smtp: SmtpServer
    imap: ImapServer
    clock: Clock
    state_dir: Path

    def deps(self) -> Deps:
        return Deps(
            orbit=self.core,
            smtp=self.smtp.client,
            imap=self.imap.client,
            state_dir=self.state_dir,
            now=self.clock,
        )

    def factory(self) -> Callable[[], Deps]:
        return self.deps
