"""Outbound mail: the decision request (design §6), confirmations (§8) and refusals (§5).

Every field taken from a request or task record is reduced to single-line text before it
reaches a header or a body line, so record text can never inject a header or a fake
transport line.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from decision_mail.config import Config
from decision_mail.grammar import WORDS, normalize

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
        f"Request: {request_id}, expires {line(request.get('expires_at', ''), 40)}",
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
