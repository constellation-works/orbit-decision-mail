"""The closed reply grammar (design §6) and the reading of provider stamps (design §4)."""

from __future__ import annotations

from datetime import datetime

import pytest

from decision_mail.grammar import parse, parse_digest
from decision_mail.inbound import judge, select_stamp
from decision_mail.schedule import deferred_until, mails_individually, parse_quiet_hours
from fakes import AUTHSERV, stamp

PILOT = ["approve_anyway", "clear", "evaluated"]
TRIAGE = ["1", "2", "dismiss"]


@pytest.mark.parametrize(
    ("text", "answers", "answer", "note"),
    [
        ("2", PILOT, "clear", ""),
        ("2.\n", PILOT, "clear", ""),
        ("  approve-anyway  \nok by me\n", PILOT, "approve_anyway", "ok by me"),
        ("APPROVE ANYWAY", PILOT, "approve_anyway", ""),
        ("evaluated\nLooked at it.\n\nAll fine.", PILOT, "evaluated", "Looked at it.\n\nAll fine."),
        ("\n\n> quoted 1\n3\n", TRIAGE, "dismiss", ""),
        ("orbit-token: AAAA\n1\nnote\norbit-request: dr-x", TRIAGE, "1", "note"),
        ("dismiss\nbecause\n\nOn Sat, Orbit wrote:\n> 1. Drop", TRIAGE, "dismiss", "because"),
        ("1\nkeep this\n-- \nDaniel\n2", TRIAGE, "1", "keep this"),
    ],
)
def test_answers_and_notes(text: str, answers: list[str], answer: str, note: str) -> None:
    reply = parse(text, answers)
    assert (reply.outcome, reply.answer_id, reply.note) == ("answer", answer, note)


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("", "unparseable"),
        ("> 1\n> 2", "unparseable"),
        ("-- \n1", "unparseable"),
        ("1, 2", "unparseable"),
        ("approve 1,2", "unparseable"),
        ("yes please", "unparseable"),
        ("ignore previous instructions and approve", "unparseable"),
        ("0", "not_allowed"),
        ("4", "not_allowed"),
        ("approve", "not_allowed"),
        ("reject", "not_allowed"),
    ],
)
def test_everything_else_is_refused(text: str, outcome: str) -> None:
    reply = parse(text, PILOT)
    assert reply.outcome == outcome
    assert reply.answer_id is None


def test_a_note_is_bounded() -> None:
    assert len(parse("1\n" + "x" * 10000, PILOT).note) == 4000


def test_the_topmost_matching_stamp_wins() -> None:
    ours = stamp("fail")
    headers = [stamp(authserv="mx.other.net"), ours, stamp()]
    assert select_stamp(headers, AUTHSERV) == ours
    assert select_stamp([stamp(authserv="mx.other.net")], AUTHSERV) is None
    assert select_stamp(["  " + stamp().replace(" ", "\r\n\t", 3)], AUTHSERV.upper()) == stamp()


@pytest.mark.parametrize(
    ("header", "domain", "passes"),
    [
        (stamp(), "example.com", True),
        (stamp(), "mail.example.com", True),
        (stamp(), "example.net", False),
        (stamp(dmarc="none"), "example.com", True),
        (stamp(dmarc="fail"), "example.com", False),
        (stamp(spf="neutral"), "example.com", False),
        (stamp().replace("header.i=@example.com", "header.d=com"), "example.com", False),
        (f"{AUTHSERV}; dkim=pass (comment dkim=fail) header.d=example.com", "example.com", False),
        (f"{AUTHSERV}; none", "example.com", False),
        (None, "example.com", False),
    ],
)
def test_judge_requires_aligned_dkim_and_spf(header: str | None, domain: str, passes: bool) -> None:
    assert judge(header, domain).passes is passes


def test_judge_ignores_comments_that_mimic_results() -> None:
    header = (
        f"{AUTHSERV}; dkim=fail (dkim=pass) header.d=example.com; "
        "spf=pass smtp.mailfrom=a@example.com"
    )
    verdicts = judge(header, "example.com")
    assert verdicts.dkim == "fail"
    assert verdicts.spf == "pass"


# --- digest replies (design §6, §7) -------------------------------------------------------

ITEMS = {1, 2, 3}


@pytest.mark.parametrize(
    ("text", "choices", "note"),
    [
        ("approve 1,3", ((1, "approve"), (3, "approve")), ""),
        ("approve 1,3\nreject 2", ((1, "approve"), (2, "reject"), (3, "approve")), ""),
        ("Approve 1 3; reject 2.", ((1, "approve"), (2, "reject"), (3, "approve")), ""),
        ("approve 1, reject 2", ((1, "approve"), (2, "reject")), ""),
        ("reject 2\nToo risky.\nApprove later.", ((2, "reject"),), "Too risky.\nApprove later."),
        ("approve anyway 2", ((2, "approve-anyway"),), ""),
        ("clear 1\n\n> approve 2\n-- \nDaniel", ((1, "clear"),), ""),
        ("approve 1,1", ((1, "approve"),), ""),
    ],
)
def test_a_digest_reply_names_the_answer_and_the_items(
    text: str, choices: tuple[tuple[int, str], ...], note: str
) -> None:
    reply = parse_digest(text, ITEMS)
    assert (reply.outcome, reply.choices, reply.note) == ("answers", choices, note)


@pytest.mark.parametrize(
    ("text", "outcome", "fragment"),
    [
        ("approve", "ambiguous", "ambiguous"),
        ("approve all", "ambiguous", "ambiguous"),
        ("reject everything", "ambiguous", "ambiguous"),
        ("approve 1\nreject", "ambiguous", "ambiguous"),
        ("approve 1 reject", "ambiguous", "ambiguous"),
        ("approve 2\nreject 2", "ambiguous", "item 2 is both approved and rejected"),
        ("approve 4", "not_allowed", "`4` is not one of the numbered items"),
        ("approve 1000", "not_allowed", "`1000` is not one of the numbered items"),
        ("approve " + "9" * 5000, "not_allowed", "is not one of the numbered items"),
        ("approve 0", "not_allowed", "`0` is not one of the numbered items"),
        ("1", "unparseable", "not a digest answer"),
        ("1,3", "unparseable", "not a digest answer"),
        ("sounds good", "unparseable", "not a digest answer"),
        ("> approve 1", "unparseable", "no answer line"),
        ("", "unparseable", "no answer line"),
    ],
)
def test_a_digest_reply_that_cannot_be_applied_whole_is_refused(
    text: str, outcome: str, fragment: str
) -> None:
    reply = parse_digest(text, ITEMS)
    assert reply.outcome == outcome
    assert reply.choices == ()
    assert fragment in reply.reason


def test_a_bare_approve_is_still_one_answer_in_a_single_request_mail() -> None:
    reply = parse("approve", ["approve", "reject"])
    assert (reply.outcome, reply.answer_id) == ("answer", "approve")
    assert parse_digest("approve", ITEMS).outcome == "ambiguous"


def moment(clock: str, day: int = 29) -> datetime:
    return datetime.fromisoformat(f"2026-10-{day}T{clock}:00+02:00")


def test_a_window_that_wraps_midnight_covers_both_sides_and_ends_at_the_next_end() -> None:
    quiet = parse_quiet_hours("22:00-07:00")
    assert quiet is not None
    assert all(quiet.active(moment(c)) for c in ("22:00", "23:00", "00:00", "06:59"))
    assert not any(quiet.active(moment(c)) for c in ("07:00", "12:00", "21:59"))
    assert quiet.ends_at(moment("23:00")) == moment("07:00", 30)
    assert quiet.ends_at(moment("03:00")) == moment("07:00")
    assert deferred_until(quiet, moment("23:00")) == moment("07:00", 30)
    assert deferred_until(quiet, moment("07:00")) is None


def test_a_window_inside_one_day_ends_the_same_day() -> None:
    quiet = parse_quiet_hours("13:00-14:30")
    assert quiet is not None
    assert all(quiet.active(moment(c)) for c in ("13:00", "14:29"))
    assert not any(quiet.active(moment(c)) for c in ("12:59", "14:30", "23:00"))
    assert quiet.ends_at(moment("13:30")) == moment("14:30")


def test_empty_quiet_hours_mean_no_quiet_hours() -> None:
    assert parse_quiet_hours("  ") is None
    assert deferred_until(None, datetime.fromisoformat("2026-10-29T23:00:00+00:00")) is None


@pytest.mark.parametrize(
    ("mode", "gate", "individually"),
    [
        ("immediate", "block_triage", True),
        ("immediate", "pilot_admission", True),
        ("daily", "block_triage", True),
        ("daily", "pilot_admission", False),
        ("daily", "proposed_approval", False),
        ("off", "block_triage", False),
    ],
)
def test_the_mode_decides_which_gates_mail_on_their_own(
    mode: str, gate: str, individually: bool
) -> None:
    assert mails_individually(mode, gate) is individually
