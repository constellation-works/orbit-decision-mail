"""The closed reply grammar of design §6, for single-request mails.

    reply  := answer                 # one answer: only a digest mail may carry several
    answer := <n> | <word>           # the printed number, or the answer word
    word   := approve | reject | approve-anyway | clear | evaluated | done | dismiss

A digest mail (design §6, §7) is the one place a reply names several items:

    reply  := clause ((";" | ",")? clause)*     # on the first line; more clause lines may follow
    clause := word <n> (("," | " ") <n>)*

`approve 1,3` and `reject 2` apply per item (the word is any answer word; `clear 4` works too).
A verb with no numbers (`approve`, `approve all`) is refused as ambiguous: "all listed" is
never inferred. `parse_digest` reads that grammar.

The grammar applies to the first non-quoted line of the reply's text; quoted history (`>`)
and everything below a `-- ` signature delimiter are ignored. Lines after the answer line
are the human's note. Words match answer ids case-insensitively and `-`/`_`/space
-insensitively. Nothing else is accepted: the text is data, never instructions.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

WORDS = frozenset({"approve", "reject", "approve-anyway", "clear", "evaluated", "done", "dismiss"})

# Lines the outbound mail carries for the transport, not for the human to answer with.
_TRANSPORT_LINE = re.compile(r"\s*orbit-(token|request)\s*:", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_-]+")
_MAX_NOTE = 4000

Outcome = Literal["answer", "unparseable", "not_allowed"]
DigestOutcome = Literal["answers", "unparseable", "ambiguous", "not_allowed"]

_ALL_WORDS = frozenset({"all", "everything", "both"})
_DIGEST_TOKEN = re.compile(r"[a-z]+(?:[-_][a-z]+)*|\d+|[,;]|\S")
_APPROVE_ANYWAY = re.compile(r"\bapprove[\s_]+anyway\b")
_NO_ITEM = 10**6
_MAX_CLAUSE_NUMBERS = 50


@dataclass(frozen=True)
class Reply:
    outcome: Outcome
    answer_id: str | None
    note: str
    reason: str


@dataclass(frozen=True)
class DigestReply:
    """Per-item choices `(number, verb)` in ascending item order, or why none were read."""

    outcome: DigestOutcome
    choices: tuple[tuple[int, str], ...]
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


AMBIGUOUS = (
    "an unqualified `approve` or `reject` is ambiguous in a digest; "
    "name the items, for example `approve 1,3`"
)


def parse_digest(text: str, items: Collection[int]) -> DigestReply:
    """Read the per-item answers of a digest reply against the printed item numbers.

    The leading lines that are all clauses are the answers and the first line that is not
    starts the note. Nothing is accepted unless every number names a printed item and no
    item is both approved and rejected, so a refused reply applies nothing at all.
    """
    lines = _unquoted_lines(text)
    if lines is None:
        return DigestReply("unparseable", (), "", "the reply has no answer line")
    first, rest = lines
    pending = [first, *rest]
    choices: dict[int, str] = {}
    used = 0
    for index, raw in enumerate(pending):
        kind, pairs = _digest_line(raw)
        if kind == "ambiguous":
            return DigestReply("ambiguous", (), _note(rest), AMBIGUOUS)
        if kind == "bad":
            if index == 0:
                reason = "the first line is not a digest answer such as `approve 1,3`"
                return DigestReply("unparseable", (), _note(rest), reason)
            break
        for number, verb in pairs:
            if choices.setdefault(number, verb) != verb:
                reason = f"item {number} is both approved and rejected"
                return DigestReply("ambiguous", (), _note(rest), reason)
        used = index + 1
    note = _note(pending[used:])
    if len(choices) > _MAX_CLAUSE_NUMBERS:
        return DigestReply("unparseable", (), note, "too many items in one reply")
    for number in sorted(choices):
        if number not in items:
            reason = f"`{number}` is not one of the numbered items"
            return DigestReply("not_allowed", (), note, reason)
    return DigestReply("answers", tuple(sorted(choices.items())), note, "")


def _digest_line(raw: str) -> tuple[Literal["ok", "ambiguous", "bad"], list[tuple[int, str]]]:
    """The `(number, verb)` pairs on one line, `ambiguous` for a verb with no items, or `bad`."""
    text = _APPROVE_ANYWAY.sub("approve-anyway", raw.strip().rstrip(".!").lower())
    pairs: list[tuple[int, str]] = []
    verb: str | None = None
    named = 0
    for token in _DIGEST_TOKEN.findall(text):
        if normalize(token) in WORDS:
            if verb is not None and not named:
                return "ambiguous", []
            verb, named = normalize(token), 0
        elif verb is not None and token in _ALL_WORDS:
            return "ambiguous", []
        elif verb is not None and token.isdigit():
            # A number this long names no item; it is not parsed so a hostile text cannot cost.
            pairs.append((int(token) if len(token) <= 6 else _NO_ITEM, verb))
            named += 1
        elif verb is None or token not in (",", ";"):
            return "bad", []
    if verb is None:
        return "bad", []
    if not named:
        return "ambiguous", []
    return "ok", pairs
