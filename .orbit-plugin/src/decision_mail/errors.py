"""The refusal a tool reports to Orbit as an error envelope."""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """A refusal reported to Orbit as an error envelope. Never carries a secret value."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail
