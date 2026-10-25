"""Read one inbound mail as untrusted data: who sent it, what the provider stamped, which
token and request it names, and its readable text (design §4 layers 4–5, §6).

Nothing here acts on the content. Headers and bodies are parsed with the standard library,
never followed, rendered or executed.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

from decision_mail.config import Config
from decision_mail.tokens import well_formed

MAX_MESSAGE_BYTES = 2 * 1024 * 1024

_REQUEST_ID = r"dr-[0-9a-f]{16}"
_SUBJECT_REQUEST = re.compile(rf"\[({_REQUEST_ID})\]")
_BODY_TOKEN = re.compile(r"^[>\s]*orbit-token:\s*([A-Za-z2-7]{26})\s*$", re.MULTILINE)
_BODY_REQUEST = re.compile(rf"^[>\s]*orbit-request:\s*({_REQUEST_ID})\s*$", re.MULTILINE)
_DIGEST_ID = r"dg-[0-9a-f]{16}"
_SUBJECT_DIGEST = re.compile(rf"\[({_DIGEST_ID})\]")
_BODY_DIGEST = re.compile(rf"^[>\s]*orbit-digest:\s*({_DIGEST_ID})\s*$", re.MULTILINE)
_BODY_ITEM = re.compile(
    rf"^[>\s]*orbit-item:\s*(\d{{1,3}})\s+({_REQUEST_ID})\s+([A-Za-z2-7]{{26}})\s*$",
    re.MULTILINE,
)
_COMMENT = re.compile(r"\([^()]*\)")
_RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")


@dataclass(frozen=True)
class Verdicts:
    """What the selected provider stamp says, judged for the sender's domain."""

    header: str | None
    dkim: str
    spf: str
    dmarc: str

    @property
    def passes(self) -> bool:
        return self.dkim == "pass" and self.spf == "pass" and self.dmarc != "fail"

    def summary(self) -> dict[str, str]:
        return {"dkim": self.dkim, "spf": self.spf, "dmarc": self.dmarc}


@dataclass(frozen=True)
class Inbound:
    message_id: str
    senders: tuple[str, ...]
    envelope_sender: str | None
    addressed_to_channel: bool
    tokens: frozenset[str]
    request_ids: frozenset[str]
    text: str | None
    verdicts: Verdicts
    # A digest mail's id and its `orbit-item:` lines `(number, request id, token)`; a reply
    # to a single-request mail carries neither.
    digest_ids: frozenset[str] = frozenset()
    digest_items: tuple[tuple[int, str, str], ...] = ()

    @property
    def sender(self) -> str | None:
        return self.senders[0] if len(self.senders) == 1 else None


def read(raw: bytes, config: Config) -> Inbound:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if not isinstance(message, EmailMessage):
        raise TypeError("the default policy did not produce an EmailMessage")
    senders = tuple(
        address.lower() for _, address in getaddresses(_headers(message, "From")) if address
    )
    envelope = _envelope_sender(message)
    recipients = [
        address.lower()
        for name in _RECIPIENT_HEADERS
        for _, address in getaddresses(_headers(message, name))
        if address
    ]
    ours = [address for address in recipients if _is_channel_address(address, config)]
    text = _plain_text(message)
    tokens = {tag for address in ours if (tag := _plus_tag(address)) and well_formed(tag)}
    if text is not None:
        tokens.update(match.upper() for match in _BODY_TOKEN.findall(text))
    request_ids = set(_SUBJECT_REQUEST.findall(" ".join(_headers(message, "Subject"))))
    if text is not None:
        request_ids.update(_BODY_REQUEST.findall(text))
    digest_ids = set(_SUBJECT_DIGEST.findall(" ".join(_headers(message, "Subject"))))
    items: set[tuple[int, str, str]] = set()
    if text is not None:
        digest_ids.update(_BODY_DIGEST.findall(text))
        items.update((int(n), rid, tag.upper()) for n, rid, tag in _BODY_ITEM.findall(text))
    sender_domain = senders[0].rsplit("@", 1)[-1] if len(senders) == 1 else ""
    stamp = select_stamp(_headers(message, "Authentication-Results"), config.authserv_id)
    return Inbound(
        message_id=" ".join(_headers(message, "Message-ID"))[:200],
        senders=senders,
        envelope_sender=envelope,
        addressed_to_channel=bool(ours),
        tokens=frozenset(tokens),
        request_ids=frozenset(request_ids),
        text=text,
        verdicts=judge(stamp, sender_domain),
        digest_ids=frozenset(digest_ids),
        digest_items=tuple(sorted(items)),
    )


def select_stamp(headers: list[str], authserv_id: str) -> str | None:
    """The topmost Authentication-Results whose authserv-id is the configured provider's.

    Headers are in message order, so the first match is the provider's own stamp; any
    below it with the same id were in the message before delivery and are ignored.
    """
    for header in headers:
        value = " ".join(header.split())
        if _authserv_id(value) == authserv_id.lower():
            return value
    return None


def judge(stamp: str | None, sender_domain: str) -> Verdicts:
    """DKIM and SPF must pass aligned with the sender's domain; DMARC must not fail."""
    if stamp is None or not sender_domain:
        return Verdicts(stamp, "none", "none", "none")
    dkim = spf = dmarc = "none"
    for method, result, props in _results(stamp):
        if method == "dkim":
            domain = props.get("header.d") or _domain(props.get("header.i", ""))
            if result == "pass" and _aligned(domain, sender_domain):
                dkim = "pass"
            elif dkim != "pass":
                dkim = result if result != "pass" else "unaligned"
        elif method == "spf":
            domain = _domain(props.get("smtp.mailfrom", ""))
            if result == "pass" and _aligned(domain, sender_domain):
                spf = "pass"
            elif spf != "pass":
                spf = result if result != "pass" else "unaligned"
        elif method == "dmarc" and dmarc != "fail":
            dmarc = result
    return Verdicts(stamp, dkim, spf, dmarc)


def _results(stamp: str) -> list[tuple[str, str, dict[str, str]]]:
    text = stamp
    while _COMMENT.search(text):
        text = _COMMENT.sub(" ", text)
    results: list[tuple[str, str, dict[str, str]]] = []
    for clause in text.split(";")[1:]:
        words = clause.split()
        if not words or "=" not in words[0]:
            continue
        method, result = words[0].split("=", 1)
        props: dict[str, str] = {}
        for word in words[1:]:
            if "=" in word:
                key, value = word.split("=", 1)
                props.setdefault(key.lower(), value.strip('"').lower())
        results.append((method.split("/")[0].lower(), result.lower(), props))
    return results


def _authserv_id(value: str) -> str:
    head = value.split(";", 1)[0].split()
    return head[0].lower() if head else ""


def _aligned(domain: str, sender_domain: str) -> bool:
    """Relaxed alignment: the same domain, or one a subdomain of the other."""
    domain = domain.lower().strip(".")
    sender_domain = sender_domain.lower().strip(".")
    if "." not in domain or "." not in sender_domain:
        return False  # never align on a bare top-level label
    return (
        domain == sender_domain
        or domain.endswith("." + sender_domain)
        or sender_domain.endswith("." + domain)
    )


def _domain(value: str) -> str:
    return value.rsplit("@", 1)[-1] if value else ""


def _headers(message: EmailMessage, name: str) -> list[str]:
    return [str(value) for value in message.get_all(name, [])]


def _envelope_sender(message: EmailMessage) -> str | None:
    values = _headers(message, "Return-Path")
    if not values:
        return None
    return parseaddr(values[0])[1].lower()


def _is_channel_address(address: str, config: Config) -> bool:
    if "@" not in address:
        return False
    local, domain = address.rsplit("@", 1)
    if domain != config.domain:
        return False
    return local == config.local_part or local.startswith(config.local_part + "+")


def _plus_tag(address: str) -> str | None:
    local = address.rsplit("@", 1)[0]
    if "+" not in local:
        return None
    return local.split("+", 1)[1].upper()


def _plain_text(message: EmailMessage) -> str | None:
    """The text/plain body (the plain alternative of an HTML mail); attachments ignored."""
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, ValueError, AssertionError):
        return None
    return content if isinstance(content, str) else None
