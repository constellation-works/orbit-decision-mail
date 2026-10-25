"""The closed reply grammar of design §6, for single-request mails.

    reply  := answer                 # one answer: only a digest mail may carry several
    answer := <n> | <word>           # the printed number, or the answer word
    word   := approve | reject | approve-anyway | clear | evaluated | done | dismiss

The grammar applies to the first non-quoted line of the reply's text; quoted history (`>`)
and everything below a `-- ` signature delimiter are ignored. Lines after the answer line
are the human's note. Words match answer ids case-insensitively and `-`/`_`/space
-insensitively. Nothing else is accepted: the text is data, never instructions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

WORDS = frozenset({"approve", "reject", "approve-anyway", "clear", "evaluated", "done", "dismiss"})

# Lines the outbound mail carries for the transport, not for the human to answer with.
_TRANSPORT_LINE = re.compile(r"\s*orbit-(token|request)\s*:", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_-]+")
_MAX_NOTE = 4000

Outcome = Literal["answer", "unparseable", "not_allowed"]


@dataclass(frozen=True)
class Reply:
    outcome: Outcome
    answer_id: str | None
    note: str
    reason: str


def normalize(word: str) -> str:
    return _SEPARATORS.sub("-", word.strip().lower()).strip("-")


def parse(text: str, answer_ids: Sequence[str]) -> Reply:
    """Read one answer and the note from a reply's text against the printed answers."""
    lines = _unquoted_lines(text)
    if lines is None:
        return Reply("unparseable", None, "", "the reply has no answer line")
    first, rest = lines
    note = _note(rest)
    answer = first.strip().rstrip(".!").strip()
    word = normalize(answer)
    if re.fullmatch(r"\d{1,3}", word):
        number = int(word)
        if 1 <= number <= len(answer_ids):
            return Reply("answer", answer_ids[number - 1], note, "")
        return Reply("not_allowed", None, note, f"`{number}` is not one of the numbered answers")
    if word in WORDS:
        for answer_id in answer_ids:
            if normalize(answer_id) == word:
                return Reply("answer", answer_id, note, "")
        return Reply("not_allowed", None, note, f"`{word}` is not an answer to this request")
    if "," in answer or re.fullmatch(r"[a-z-]+-\d[\d,\s-]*", word):
        reason = "a single-request mail takes exactly one answer"
        return Reply("unparseable", None, note, reason)
    return Reply("unparseable", None, note, "the first line is not a number or an answer word")


def _unquoted_lines(text: str) -> tuple[str, list[str]] | None:
    """The answer line and the unquoted lines after it, up to quoting or a signature."""
    first: str | None = None
    rest: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if line == "--" or raw.startswith("-- "):
            break
        if line.lstrip().startswith(">"):
            if first is None:
                continue
            break
        if first is None:
            if not line.strip() or _TRANSPORT_LINE.match(line) or line.endswith("wrote:"):
                continue
            first = line
        else:
            rest.append(line)
    if first is None:
        return None
    return first, rest


def _note(lines: list[str]) -> str:
    kept = list(lines)
    while kept and not kept[-1].strip():
        kept.pop()
    # The attribution line a client writes above the quoted original ("On …, X wrote:").
    if kept and kept[-1].strip().endswith("wrote:"):
        kept.pop()
    note = "\n".join(line for line in kept if not _TRANSPORT_LINE.match(line)).strip()
    return note[:_MAX_NOTE]
