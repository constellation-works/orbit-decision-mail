"""The closed reply grammar (design §6) and the reading of provider stamps (design §4)."""

from __future__ import annotations

import pytest

from decision_mail.grammar import parse
from decision_mail.inbound import judge, select_stamp
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
