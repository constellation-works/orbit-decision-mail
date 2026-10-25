"""SMTP out and IMAP in, over the standard library only, always over TLS.

The clients are built by factories so tests substitute fakes for `smtplib`/`imaplib`
objects and never open a socket; everything above the factory runs unchanged. A transport
error carries the server's or socket's description, never a credential.
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from types import TracebackType
from typing import Any, Protocol

from decision_mail.config import Config

SOCKET_TIMEOUT_SECONDS = 30.0


class TransportError(Exception):
    """A mail server could not be reached, refused login, or refused a command."""


class SmtpClient(Protocol):
    def login(self, user: str, password: str) -> Any: ...
    def send_message(self, msg: EmailMessage) -> Any: ...
    def quit(self) -> Any: ...


class ImapClient(Protocol):
    capabilities: tuple[str, ...]

    def login(self, user: str, password: str) -> tuple[str, list[Any]]: ...
    def select(self, mailbox: str = ..., readonly: bool = ...) -> tuple[str, list[Any]]: ...
    def create(self, mailbox: str) -> tuple[str, list[Any]]: ...
    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...
    def logout(self) -> tuple[str, list[Any]]: ...


SmtpFactory = Callable[[Config], SmtpClient]
ImapFactory = Callable[[Config], ImapClient]


def connect_smtp(config: Config) -> SmtpClient:
    context = ssl.create_default_context()
    if config.smtp_security == "ssl":
        return smtplib.SMTP_SSL(
            config.smtp_host, config.smtp_port, timeout=SOCKET_TIMEOUT_SECONDS, context=context
        )
    client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=SOCKET_TIMEOUT_SECONDS)
    client.starttls(context=context)
    return client


def connect_imap(config: Config) -> ImapClient:
    context = ssl.create_default_context()
    if config.imap_security == "ssl":
        return imaplib.IMAP4_SSL(
            config.imap_host,
            config.imap_port,
            ssl_context=context,
            timeout=SOCKET_TIMEOUT_SECONDS,
        )
    client = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=SOCKET_TIMEOUT_SECONDS)
    client.starttls(ssl_context=context)
    return client


class Outbox:
    """One SMTP session; `send` delivers a message from the channel mailbox."""

    def __init__(self, config: Config, password: str, factory: SmtpFactory) -> None:
        self._config = config
        self._password = password
        self._factory = factory
        self._client: SmtpClient | None = None

    def __enter__(self) -> Outbox:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._client is not None:
            try:
                self._client.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self._client = None

    def send(self, message: EmailMessage) -> None:
        try:
            if self._client is None:
                client = self._factory(self._config)
                self._client = client
                client.login(self._config.smtp_username, self._password)
            self._client.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
            raise TransportError(f"smtp: {_describe(error)}") from None


class Mailbox:
    """One IMAP session on the configured folder."""

    def __init__(self, config: Config, password: str, factory: ImapFactory) -> None:
        self._config = config
        self._password = password
        self._factory = factory
        self._client: ImapClient | None = None
        self._folder_ready = False

    def __enter__(self) -> Mailbox:
        try:
            client = self._factory(self._config)
            self._client = client
            _ok(client.login(self._config.imap_username, self._password), "login")
            _ok(client.select(_quote(self._config.imap_folder)), "select")
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as error:
            self._logout()
            raise TransportError(f"imap: {_describe(error)}") from None
        except TransportError:
            self._logout()
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._logout()

    def unseen(self, limit: int) -> list[str]:
        """UIDs of unseen messages, oldest (lowest UID) first, at most `limit`."""
        data = self._command("SEARCH", None, "UNSEEN")
        uids = sorted(
            {int(word) for chunk in data if isinstance(chunk, bytes) for word in chunk.split()}
        )
        return [str(uid) for uid in uids[:limit]]

    def fetch(self, uid: str) -> bytes | None:
        """The raw message, without setting `\\Seen`."""
        for item in self._command("FETCH", uid, "(BODY.PEEK[])"):
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):  # pyright: ignore[reportUnknownArgumentType]
                return item[1]
        return None

    def mark_seen(self, uid: str, *, flagged: bool = False) -> None:
        flags = "(\\Seen \\Flagged)" if flagged else "(\\Seen)"
        self._command("STORE", uid, "+FLAGS", flags)

    def file_processed(self, uid: str) -> None:
        """Move the message to the processed folder (copy when the server cannot move)."""
        folder = _quote(self._config.processed_folder)
        client = self._require()
        if not self._folder_ready:
            try:
                client.create(folder)
            except (imaplib.IMAP4.error, OSError):
                pass  # it usually exists already; MOVE/COPY below reports a real problem
            self._folder_ready = True
        if "MOVE" in client.capabilities:
            self._command("MOVE", uid, folder)
        else:
            self._command("COPY", uid, folder)

    def _command(self, command: str, *args: Any) -> list[Any]:
        client = self._require()
        try:
            return _ok(client.uid(command, *args), command.lower())
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as error:
            raise TransportError(f"imap {command.lower()}: {_describe(error)}") from None

    def _require(self) -> ImapClient:
        if self._client is None:
            raise TransportError("imap: the session is not open")
        return self._client

    def _logout(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self._client = None


def _ok(response: tuple[str, list[Any]], what: str) -> list[Any]:
    status, data = response
    if status != "OK":
        detail = b" ".join(chunk for chunk in data if isinstance(chunk, bytes))
        raise TransportError(f"imap {what} refused: {detail.decode(errors='replace')[:200]}")
    return data


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {' '.join(str(error).split())[:200]}"
