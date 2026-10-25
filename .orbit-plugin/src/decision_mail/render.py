"""Outbound mail: the decision request (design §6), confirmations (§8) and refusals (§5).

Every field taken from a request or task record is reduced to single-line text before it
reaches a header or a body line, so record text can never inject a header or a fake
transport line.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from decision_mail.config import Config
from decision_mail.grammar import WORDS, normalize
from decision_mail.tokens import TokenError, unix_seconds

GATE_SUMMARY = {
    "block_triage": (
        "The task is blocked and triage proposed ways forward. Choosing an option applies "
        "its replacement text and returns the task to the backlog."
    ),
    "pilot_admission": (
        "Task-pilot admission is holding the task on its findings. Your verdict decides "
        "whether it may be admitted."
    ),
    "proposed_approval": "The task is proposed and waits for approval before it can run.",
    "review_approval": "The task is in review and waits for your approval.",
}

INSTRUCTION = (
    "Reply with just the number (or word); anything after the first line is kept as your note."
)

# What each refusal tells the human, keyed by core's refusal code (design §5).
REFUSAL_TEXT = {
    "token_consumed": (
        "A reply presented a token that was already used, or one minted before the "
        "channel key was rotated. Nothing was applied again; any earlier answer stands. "
        "If you did not send this reply, rotate the channel key."
    ),
    "request_expired": (
        "The request expired before your answer arrived, so nothing was applied. Answer "
        "from the dashboard instead; if the task is still held, a fresh request will be "
        "mailed with a new token."
    ),
    "request_superseded": (
        "The hold changed before your answer arrived, so nothing was applied. Decide on "
        "the current hold through its own request or the dashboard."
    ),
    "answer_not_allowed": (
        "Your reply did not name one of the allowed answers, so nothing was applied and "
        "the token is still valid. Reply again with one of the answers below until the "
        "request expires."
    ),
    "channel_revoked": (
        "The email decision channel is revoked, so nothing was applied. Open requests "
        "wait to be re-mailed once the channel is registered again; the dashboard still "
        "answers them."
    ),
    "token_invalid": (
        "The token in your reply does not verify for this request (the channel key may "
        "have changed since the mail was sent), so nothing was applied. Answer from the "
        "dashboard, or wait for the request to be re-mailed."
    ),
}


def line(value: Any, limit: int = 200) -> str:
    """Untrusted record text as one printable line, bounded."""
    text = " ".join(str(value).split())
    text = "".join(char for char in text if char.isprintable())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def request_mail(
    config: Config,
    request: Mapping[str, Any],
    task: Mapping[str, Any],
    token: str,
    now: datetime | None = None,
) -> EmailMessage:
    request_id = line(request.get("id", ""), 64)
    task_id = line(request.get("task_id", ""), 64)
    title = line(task.get("title") or "(untitled task)", 120)
    gate = str(request.get("gate", ""))
    answers = _answers(request)
    body = [
        f"orbit-token: {token}",
        f"orbit-request: {request_id}",
        "",
        f"Orbit is holding {task_id} for your decision.",
        "",
        f"Task:    {title} ({task_id})",
        f"Status:  {line(task.get('status', 'unknown'), 40)}",
        f"Request: {request_id}, {expiry(request, now)}",
        "",
        GATE_SUMMARY.get(gate, f"The task is held at the {line(gate, 40)} gate."),
        "",
        "Answers:",
        *_listing(answers),
        "",
        INSTRUCTION,
    ]
    body += _dashboard(config, task_id, request_id)
    message = _message(config, config.notify_to, body, "auto-generated")
    message["Subject"] = f"[orbit] decision needed: {title} ({task_id}) [{request_id}]"
    message["Reply-To"] = config.reply_address(token)
    message["X-Orbit-Decision-Request"] = request_id
    return message


def confirmation_mail(
    config: Config,
    to: str,
    request: Mapping[str, Any],
    answer_id: str,
    task_status: str | None,
) -> EmailMessage:
    request_id = line(request.get("id", ""), 64)
    task_id = line(request.get("task_id", ""), 64)
    label = next(
        (label for answer, label, _ in _answers(request) if answer == answer_id), answer_id
    )
    body = [
        f"Applied: {line(label)} ({line(answer_id, 64)}) on {task_id}.",
        "",
        f"Request {request_id} now reads {line(request.get('state', 'answered'), 40)}.",
    ]
    if task_status:
        body.append(f"Task {task_id} is now {line(task_status, 40)}.")
    body += _dashboard(config, task_id, request_id)
    message = _message(config, to, body, "auto-replied")
    message["Subject"] = f"[orbit] decision applied: {task_id} [{request_id}]"
    return message


def refusal_mail(
    config: Config,
    to: str,
    request: Mapping[str, Any],
    code: str,
    detail: str = "",
) -> EmailMessage:
    request_id = line(request.get("id", ""), 64)
    task_id = line(request.get("task_id", ""), 64)
    body = [
        f"Nothing was applied to {task_id} for request {request_id} ({code}).",
        "",
        REFUSAL_TEXT.get(code, "The answer was refused, so nothing was applied."),
    ]
    if detail:
        body += ["", line(detail, 400)]
    if code == "answer_not_allowed":
        body += ["", "Answers:", *_listing(_answers(request)), "", INSTRUCTION]
    body += _dashboard(config, task_id, request_id)
    message = _message(config, to, body, "auto-replied")
    message["Subject"] = f"[orbit] decision not applied: {task_id} [{request_id}]"
    return message


def expiry(request: Mapping[str, Any], now: datetime | None) -> str:
    """`expires <time>`, with the time left counted from `now` (the send time) when given."""
    stamp = line(request.get("expires_at", ""), 40)
    left = remaining(str(request.get("expires_at", "")), now) if now is not None else None
    return f"expires {stamp}" + (f" ({left})" if left else "")


def remaining(expires_at: str, now: datetime) -> str | None:
    """`in 2d 4h`, `in 5h 30m` or `in 12m`, counted from `now`; None when unreadable."""
    try:
        seconds = unix_seconds(expires_at) - int(now.astimezone(UTC).timestamp())
    except TokenError:
        return None
    if seconds <= 0:
        return "already expired"
    minutes = seconds // 60
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {max(mins, 1)}m"


def _answers(request: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """`(id, label, consequence)` for each allowed answer, in the order core lists them."""
    answers: Sequence[Any] = request.get("answers") or []
    return [
        (
            str(answer.get("id", "")),
            line(answer.get("label", "")),
            line(answer.get("consequence", ""), 300),
        )
        for answer in answers
        if isinstance(answer, dict)
    ]


def _listing(answers: list[tuple[str, str, str]]) -> list[str]:
    """The numbered answers; a reply's number indexes this listing."""
    rows: list[str] = []
    for number, (answer_id, label, consequence) in enumerate(answers, start=1):
        word = normalize(answer_id)
        alias = f" (or reply `{word}`)" if word in WORDS else ""
        rows.append(f"  {number}. {label}{alias}")
        if consequence:
            rows.append(f"     {consequence}")
    return rows


def _dashboard(config: Config, task_id: str, request_id: str) -> list[str]:
    if not config.dashboard_url:
        return ["", "The dashboard's decision list also shows this request."]
    return ["", f"Dashboard: {config.dashboard_link(task_id, request_id)}"]


def _message(config: Config, to: str, body: list[str], auto: str) -> EmailMessage:
    message = EmailMessage()
    local, domain = config.address.rsplit("@", 1)
    message["From"] = Address(config.from_name, local, domain)
    message["To"] = to
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=config.domain)
    message["Auto-Submitted"] = auto
    message.set_content("\n".join(body) + "\n")
    return message


# --- digest (design §6, §7) ---------------------------------------------------------------

SECTION_TITLES = {
    "proposed": "Proposed tasks awaiting approval",
    "reminder": "Reminders: open requests nearing expiry or to be re-sent",
    "held": "Also waiting for your decision",
}

DIGEST_INSTRUCTION = (
    "Reply with the answer word and the item numbers, for example `approve 1,3` and then "
    '`reject 2` on its own line. A bare `approve` is refused: "all listed" is never '
    "assumed. Anything after the answer lines is kept as your note."
)


@dataclass(frozen=True)
class DigestItem:
    """One numbered line of a digest. `token` is None when the request is no longer open."""

    number: int
    section: str
    request: Mapping[str, Any]
    task: Mapping[str, Any]
    token: str | None


@dataclass(frozen=True)
class Confirmation:
    """An answer applied from an earlier digest reply, as plugin state keeps it."""

    request_id: str
    task_id: str
    title: str
    answer: str
    label: str
    task_status: str


def pilot_state(task: Mapping[str, Any]) -> str:
    """Where the task-pilot stands, from the task record's `pilot_warnings` and `readiness`."""
    warnings = task.get("pilot_warnings")
    if isinstance(warnings, list) and warnings:
        kinds = sorted({line(w.get("kind", "?"), 40) for w in warnings if isinstance(w, dict)})  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        return f"held on {len(warnings)} pilot warning(s)" + (
            f": {', '.join(kinds)}" if kinds else ""
        )  # pyright: ignore[reportUnknownArgumentType]
    readiness = task.get("readiness")
    if isinstance(readiness, dict):
        gaps = readiness.get("gaps")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        blocking = sorted(
            {
                line(gap.get("code", "?"), 40)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                for gap in gaps  # pyright: ignore[reportUnknownVariableType]
                if isinstance(gap, dict) and gap.get("severity") == "blocking"  # pyright: ignore[reportUnknownMemberType]
            }
            if isinstance(gaps, list)
            else set()
        )
        if blocking:
            return f"not ready: {', '.join(blocking)}"
        if readiness.get("ready") is True:  # pyright: ignore[reportUnknownMemberType]
            return "no pilot warnings"
    return "not assessed"


def summary(task: Mapping[str, Any]) -> str:
    """The first prose line of the task's description, as a one-line summary."""
    for raw in str(task.get("description") or "").splitlines():
        text = raw.strip().lstrip("-*> ").strip()
        if text and not text.startswith("#"):
            return line(text, 160)
    return "(no description)"


def digest_mail(
    config: Config,
    digest_id: str,
    items: Sequence[DigestItem],
    confirmations: Sequence[Confirmation],
    now: datetime,
    omitted: int = 0,
) -> EmailMessage:
    counts = {name: sum(1 for item in items if item.section == name) for name in SECTION_TITLES}
    body = _digest_head(digest_id, items)
    body += [
        "",
        f"Orbit decision digest: {len(items)} to decide, {len(confirmations)} applied since "
        "the last digest.",
    ]
    if confirmations:
        body += ["", "Applied since the last digest:"]
        for done in confirmations:
            status = f"; task is now {line(done.task_status, 40)}" if done.task_status else ""
            body.append(
                f"  - {line(done.task_id, 64)} {line(done.title, 120)}: "
                f"{line(done.label)} ({line(done.answer, 64)}){status}"
            )
    for name, title in SECTION_TITLES.items():
        if counts[name]:
            body += [
                "",
                f"{title}:",
                *_digest_listing(config, [i for i in items if i.section == name], now),
            ]
    if omitted:
        body += ["", f"{omitted} more request(s) did not fit; the next digest lists them."]
    if items:
        body += ["", DIGEST_INSTRUCTION]
    body += _digest_dashboard()
    message = _message(config, config.notify_to, body, "auto-generated")
    message["Subject"] = (
        f"[orbit] decision digest {now.date().isoformat()}: {len(items)} to decide [{digest_id}]"
    )
    message["Reply-To"] = config.address
    return message


def digest_refusal_mail(
    config: Config,
    to: str,
    digest_id: str,
    reason: str,
    items: Sequence[DigestItem],
    now: datetime,
) -> EmailMessage:
    """Nothing in the reply was applied and no token was spent; the items are listed again."""
    body = _digest_head(digest_id, items)
    body += [
        "",
        f"Nothing was applied for digest {digest_id}: {line(reason, 400)}",
        "",
        "No token was used, so you can reply again until the requests expire.",
    ]
    if items:
        body += ["", "Items:", *_digest_listing(config, items, now), "", DIGEST_INSTRUCTION]
    body += _digest_dashboard()
    message = _message(config, to, body, "auto-replied")
    message["Subject"] = f"[orbit] digest reply not applied [{digest_id}]"
    message["Reply-To"] = config.address
    return message


def digest_item_refusals_mail(
    config: Config,
    to: str,
    digest_id: str,
    applied: int,
    refused: Sequence[tuple[int, str, str, str]],
) -> EmailMessage:
    """Items of a digest reply that core refused: `(number, task id, request id, code)`."""
    body = [
        f"Digest {digest_id}: {applied} answer(s) applied, {len(refused)} refused.",
        "",
        "Refused items (nothing was applied for them):",
    ]
    for number, task_id, request_id, code in refused:
        body.append(
            f"  {number}. {line(task_id, 64)} [{line(request_id, 64)}] ({line(code, 40)}): "
            f"{REFUSAL_TEXT.get(code, 'The answer was refused, so nothing was applied.')}"
        )
    body += _digest_dashboard()
    message = _message(config, to, body, "auto-replied")
    message["Subject"] = f"[orbit] digest answers not applied [{digest_id}]"
    return message


def _digest_head(digest_id: str, items: Sequence[DigestItem]) -> list[str]:
    """The transport lines a reply quotes back: the digest id and each open item's token."""
    head = [f"orbit-digest: {digest_id}"]
    head += [
        f"orbit-item: {item.number} {line(item.request.get('id', ''), 64)} {item.token}"
        for item in items
        if item.token
    ]
    return head


def _digest_listing(config: Config, items: Sequence[DigestItem], now: datetime) -> list[str]:
    rows: list[str] = []
    for item in items:
        request_id = line(item.request.get("id", ""), 64)
        task_id = line(item.request.get("task_id", ""), 64)
        title = line(item.task.get("title") or "(untitled task)", 120)
        gate = str(item.request.get("gate", ""))
        rows.append(f"  {item.number}. {title} ({task_id})")
        rows.append(f"     {summary(item.task)}")
        if gate == "proposed_approval":
            rows.append(f"     Pilot: {pilot_state(item.task)}")
        else:
            rows.append(f"     {GATE_SUMMARY.get(gate, f'Held at the {line(gate, 40)} gate.')}")
        state = str(item.request.get("state", "open"))
        if item.token is None:
            rows.append(
                f"     This request now reads {line(state, 40)}; it can no longer be answered."
            )
            continue
        words = [
            normalize(answer_id)
            for answer_id, _, _ in _answers(item.request)
            if normalize(answer_id) in WORDS
        ]
        reply = ", ".join(words) if words else "none by digest; use its own mail or the dashboard"
        rows.append(f"     Reply with: {reply}")
        rows.append(f"     Request {request_id}, {expiry(item.request, now)}")
        if config.dashboard_url:
            rows.append(f"     {config.dashboard_link(task_id, request_id)}")
    return rows


def _digest_dashboard() -> list[str]:
    return ["", "The dashboard's decision list shows every open request."]
