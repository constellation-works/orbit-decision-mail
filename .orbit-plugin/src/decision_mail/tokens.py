"""The per-request reply token, minted exactly as core mints it (design §4 layer 1).

`token = base32(HMAC-SHA256(key, "<request_id>|<task_id>|<epoch>|<expires_at_unix>"))`,
RFC 4648 alphabet without padding, truncated to 26 characters. The key is the hex-decoded
`channel_key` secret and the epoch is the channel's current key epoch. Core verifies; the
plugin mints only to render the token into the mail it sends.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime

TOKEN_LEN = 26
TOKEN_PATTERN = re.compile(r"[A-Z2-7]{26}")

# RFC 3339 as core serializes `expires_at`: seconds, optional fraction of any length,
# and `Z` or a numeric offset.
_RFC3339 = re.compile(
    r"(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?P<zone>Z|[+-]\d{2}:\d{2})",
    re.IGNORECASE,
)


class TokenError(ValueError):
    """The key or a request field cannot produce a token. Never carries key material."""


def mint(key_hex: str, request_id: str, task_id: str, epoch: int, expires_at: str) -> str:
    """The token core mints for one request under one key epoch."""
    key = _key_bytes(key_hex)
    message = f"{request_id}|{task_id}|{epoch}|{unix_seconds(expires_at)}".encode()
    mac = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b32encode(mac).decode("ascii").rstrip("=")[:TOKEN_LEN]


def well_formed(token: str) -> bool:
    return TOKEN_PATTERN.fullmatch(token) is not None


def fingerprint(token: str) -> str:
    """SHA-256 hex of the token, the form core records as provenance."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def unix_seconds(timestamp: str) -> int:
    """Whole seconds since the epoch, floored, as chrono's `timestamp()` gives them."""
    match = _RFC3339.fullmatch(timestamp.strip())
    if match is None:
        raise TokenError("expires_at is not an RFC 3339 timestamp")
    zone = match["zone"].upper().replace("Z", "+00:00")
    moment = datetime.fromisoformat(match["base"] + zone)
    return int(moment.astimezone(UTC).timestamp())


def _key_bytes(key_hex: str) -> bytes:
    if len(key_hex) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", key_hex) or not key_hex:
        raise TokenError("the channel_key secret is not hex; rotate the channel key")
    return bytes.fromhex(key_hex)
