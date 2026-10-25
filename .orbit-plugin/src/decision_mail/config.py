"""The `[plugins.decision-mail]` config and the declared secrets, read from the call context.

Mailbox host, port, address and the sender allowlist are config; passwords and the channel
key are secrets that arrive only in `context.secrets` (each `{"value", "version"}`). Nothing
here ever puts a secret value into a message, an error or an output.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any

# The names `spec.secrets` declares in plugin.yaml; tests hold the two equal.
SECRET_NAMES = ("smtp_password", "imap_password", "channel_key")

# Config keys with no default: a send or a poll refuses `not_configured` until they are set.
REQUIRED_KEYS = (
    "address",
    "notify_to",
    "allowed_senders",
    "authserv_id",
    "smtp_host",
    "imap_host",
)

DEFAULTS: dict[str, Any] = {
    "channel": "email",
    "key_epoch": 1,
    "plus_addressing": True,
    "smtp_port": 465,
    "smtp_security": "ssl",
    "imap_port": 993,
    "imap_security": "ssl",
    "imap_folder": "INBOX",
    "processed_folder": "Orbit/Processed",
    "max_messages": 10,
    "from_name": "Orbit",
    "dashboard_url": "",
}


class ConfigError(Exception):
    """Config or secrets are missing or invalid. `missing` holds names only."""

    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class Config:
    address: str
    notify_to: str
    allowed_senders: frozenset[str]
    authserv_id: str
    channel: str
    key_epoch: int
    plus_addressing: bool
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    imap_host: str
    imap_port: int
    imap_security: str
    imap_username: str
    imap_folder: str
    processed_folder: str
    max_messages: int
    from_name: str
    dashboard_url: str

    @property
    def local_part(self) -> str:
        return self.address.rsplit("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[1]

    def reply_address(self, token: str) -> str:
        """`<local>+<token>@<domain>` with plus-addressing, else the bare address."""
        if not self.plus_addressing:
            return self.address
        return f"{self.local_part}+{token}@{self.domain}"

    def dashboard_link(self, task_id: str, request_id: str) -> str:
        """The configured dashboard URL with `{task_id}` / `{request_id}` filled in."""
        return self.dashboard_url.replace("{task_id}", task_id).replace("{request_id}", request_id)


def secret(context: dict[str, Any], name: str) -> str | None:
    """A declared secret's value from the call context, or None when it is unset."""
    secrets = context.get("secrets")
    if not isinstance(secrets, dict):
        return None
    entry = secrets.get(name)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return value if isinstance(value, str) and value else None


def secret_presence(context: dict[str, Any]) -> dict[str, str]:
    return {name: "set" if secret(context, name) else "unset" for name in SECRET_NAMES}


def raw_config(context: dict[str, Any]) -> dict[str, Any]:
    config = context.get("config")
    merged = dict(DEFAULTS)
    if isinstance(config, dict):
        for key, value in config.items():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(key, str) and value is not None:
                merged[key] = value
    return merged


def missing_config(context: dict[str, Any]) -> list[str]:
    """Names of required config keys that are unset or empty."""
    merged = raw_config(context)
    return [key for key in REQUIRED_KEYS if not merged.get(key)]


def load(context: dict[str, Any], *, secrets: tuple[str, ...]) -> Config:
    """The validated config, refusing when a required key or one of `secrets` is unset."""
    missing = missing_config(context) + [name for name in secrets if not secret(context, name)]
    if missing:
        raise ConfigError(
            "decision-mail is not configured; set the missing config keys with "
            "`orbit config set plugins.decision-mail.<key>` and the missing secrets with "
            "`orbit plugin secret set decision-mail <name>`",
            missing,
        )
    merged = raw_config(context)
    address = _address(merged["address"], "address")
    allowed = merged["allowed_senders"]
    if isinstance(allowed, str):
        allowed = [allowed]
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError("allowed_senders must be a non-empty list of addresses", [])
    senders = frozenset(
        _address(item, "allowed_senders")
        for item in allowed  # pyright: ignore[reportUnknownVariableType]
    )
    return Config(
        address=address,
        notify_to=_address(merged["notify_to"], "notify_to"),
        allowed_senders=senders,
        authserv_id=_text(merged["authserv_id"], "authserv_id").lower(),
        channel=_text(merged["channel"], "channel"),
        key_epoch=_integer(merged["key_epoch"], "key_epoch", 1, 2**63 - 1),
        plus_addressing=bool(merged["plus_addressing"]),
        smtp_host=_text(merged["smtp_host"], "smtp_host"),
        smtp_port=_integer(merged["smtp_port"], "smtp_port", 1, 65535),
        smtp_security=_choice(merged["smtp_security"], "smtp_security", ("ssl", "starttls")),
        smtp_username=_text(merged.get("smtp_username") or address, "smtp_username"),
        imap_host=_text(merged["imap_host"], "imap_host"),
        imap_port=_integer(merged["imap_port"], "imap_port", 1, 65535),
        imap_security=_choice(merged["imap_security"], "imap_security", ("ssl", "starttls")),
        imap_username=_text(merged.get("imap_username") or address, "imap_username"),
        imap_folder=_text(merged["imap_folder"], "imap_folder"),
        processed_folder=_text(merged["processed_folder"], "processed_folder"),
        max_messages=_integer(merged["max_messages"], "max_messages", 1, 50),
        from_name=_text(merged["from_name"], "from_name"),
        dashboard_url=str(merged["dashboard_url"] or "").strip(),
    )


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip() or _has_control(value):
        raise ConfigError(f"config key {key} must be a non-empty single-line string", [])
    return value.strip()


def _address(value: Any, key: str) -> str:
    text = _text(value, key)
    _, parsed = parseaddr(text)
    if parsed != text or text.count("@") != 1 or not all(text.split("@")):
        raise ConfigError(f"config key {key} must hold bare mail addresses (user@domain)", [])
    return text.lower()


def _integer(value: Any, key: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"config key {key} must be an integer from {low} to {high}", [])
    return value


def _choice(value: Any, key: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ConfigError(f"config key {key} must be one of: {', '.join(choices)}", [])
    return str(value)


def _has_control(text: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in text)
