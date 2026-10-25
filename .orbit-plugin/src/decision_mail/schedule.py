"""When a mail may go out: the batching mode and the quiet hours (design §8).

Both are plugin config (`digest_mode`, `quiet_hours`), so core stays transport-agnostic.
Times are host-local: the clock the plugin is given carries the host's offset, and a quiet
window is read on that clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

MODES = ("immediate", "daily", "off")
# A blocked task is stopped work, so `daily` still mails its request at once (design §8).
IMMEDIATE_GATE = "block_triage"

_WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class QuietHours:
    """A daily window `[start, end)` that may wrap midnight (`22:00-07:00`)."""

    start: time
    end: time

    def active(self, now: datetime) -> bool:
        clock = now.timetz().replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= clock < self.end
        return clock >= self.start or clock < self.end

    def ends_at(self, now: datetime) -> datetime:
        """The end of the window `now` is inside: the next time the clock reads `end`."""
        end = datetime.combine(now.date(), self.end, tzinfo=now.tzinfo)
        return end if end > now else end + timedelta(days=1)


def parse_quiet_hours(text: str) -> QuietHours | None:
    """`HH:MM-HH:MM`, or None for an empty value. Raises ValueError for anything else."""
    value = text.strip()
    if not value:
        return None
    match = _WINDOW.fullmatch(value)
    if match is None:
        raise ValueError("quiet_hours must read HH:MM-HH:MM (24-hour, host-local) or be empty")
    start = time(int(match[1]), int(match[2]))
    end = time(int(match[3]), int(match[4]))
    if start == end:
        raise ValueError("quiet_hours must start and end at different times")
    return QuietHours(start, end)


def mails_individually(mode: str, gate: str) -> bool:
    """Whether `notify` mails a request of this gate on its own, as it is raised."""
    if mode == "immediate":
        return True
    return mode == "daily" and gate == IMMEDIATE_GATE


def deferred_until(quiet: QuietHours | None, now: datetime) -> datetime | None:
    """The end of the quiet window when `now` is inside it, else None."""
    if quiet is None or not quiet.active(now):
        return None
    return quiet.ends_at(now)
