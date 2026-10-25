"""Tool handlers. Each takes validated input and the call context and returns output."""

from __future__ import annotations

from typing import Any

from decision_mail import __version__

# The names `spec.secrets` declares in plugin.yaml; tests hold the two equal.
SECRET_NAMES = ("smtp_password", "imap_password", "channel_key")


def status(_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Version, transport state and which declared secrets are set; never a value."""
    delivered = context.get("secrets")
    present = set(delivered) if isinstance(delivered, dict) else set()
    return {
        "version": __version__,
        "transport": "not_implemented",
        "secrets": {name: "set" if name in present else "unset" for name in SECRET_NAMES},
    }
