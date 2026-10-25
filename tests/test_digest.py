"""The digest, multi-answer digest replies, batching and quiet hours (task ORB-18307).

Everything runs over fake SMTP, IMAP and core with a fixed clock: no socket, no mailbox, no
real time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from decision_mail.orbit_api import CallbackError
from decision_mail.state import MAX_PENDING
from fakes import (
    OPERATOR,
    Clock,
    FakeCore,
    ImapServer,
    Rig,
    SmtpServer,
    approval_request,
    body_of,
    config,
    digest_reply,
    envelope,
    parse_sent,
    pilot_request,
    proposed_task,
    reply,
    secrets,
    task,
    triage_request,
)

RID = "dr-0123456789abcdef"
PROCESSED = "Orbit/Processed"
NINE = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(FakeCore(), SmtpServer(), ImapServer(), Clock(), tmp_path / "state")


def at(rig: Rig, moment: datetime) -> None:
    rig.clock.now = moment
    rig.core.now = moment


def run(rig: Rig, tool: str, tool_input: dict[str, Any] | None = None, **cfg: Any) -> Any:
    cfg.setdefault("digest_mode", "daily")
    raw = envelope(tool, tool_input, config=config(**cfg), secrets=secrets())
    from decision_mail.protocol import serve

    return json.loads(serve(raw, rig.factory()))


def ok(rig: Rig, tool: str, tool_input: dict[str, Any] | None = None, **cfg: Any) -> Any:
    response = run(rig, tool, tool_input, **cfg)
    assert response["ok"] is True, response
    return response["output"]


def propose(rig: Rig) -> None:
    """Three proposed tasks, the first one held by a pilot warning."""
    rig.core.tasks["ORB-1"] = proposed_task(
        "ORB-1",
        "Add the digest",
        "## Outcome\n\nSend one daily mail.\nMore detail follows.",
        pilot_warnings=[{"kind": "scope", "text": "touches too much"}],
    )
    rig.core.tasks["ORB-2"] = proposed_task("ORB-2", "Fix typos", "A trivial fix.")
    rig.core.tasks["ORB-3"] = proposed_task("ORB-3", "Rename a flag", "Rename `--old` to `--new`.")


def item_lines(mail: str) -> dict[int, tuple[str, str]]:
    found: dict[int, tuple[str, str]] = {}
    for line in mail.splitlines():
        if line.startswith("orbit-item: "):
            number, request_id, token = line.removeprefix("orbit-item: ").split()
            found[int(number)] = (request_id, token)
    return found


def digest_id_of(mail: str) -> str:
    (line,) = [line for line in mail.splitlines() if line.startswith("orbit-digest: ")]
    return line.removeprefix("orbit-digest: ")


def send_digest(rig: Rig) -> tuple[str, str]:
    """Run the digest tool and return the mail body and its digest id."""
    ok(rig, "digest")
    body = body_of(rig.smtp.sent[-1])
    return body, digest_id_of(body)


def answer(rig: Rig, text: str, body: str, digest_id: str, **kwargs: Any) -> int:
    return rig.imap.deliver(digest_reply(text, body, digest_id=digest_id, **kwargs))


def poll(rig: Rig, **cfg: Any) -> dict[str, Any]:
    output: dict[str, Any] = ok(rig, "poll", **cfg)
    return output


# --- criterion 1: the digest tool ---------------------------------------------------------


def test_digest_lists_proposed_tasks_then_expiring_requests_each_with_its_token(rig: Rig) -> None:
    propose(rig)
    # A block-triage request that expires within core's 24-hour window.
    rig.core.add(triage_request(expires_at="2026-10-29T20:00:00Z"), task("blocked"))

    output = ok(rig, "digest")

    (sent,) = rig.smtp.sent
    mail = parse_sent(sent)
    body = body_of(sent)
    assert mail["To"] == OPERATOR
    assert mail["Reply-To"] == "orbit@example.org"
    assert mail["Auto-Submitted"] == "auto-generated"
    assert mail["Subject"] == (
        f"[orbit] decision digest 2026-10-29: 4 to decide [{output['digest_id']}]"
    )
    assert output["sent"] is True
    assert output["raised"] == 3
    assert [(i["number"], i["task_id"], i["section"]) for i in output["items"]] == [
        (1, "ORB-1", "proposed"),
        (2, "ORB-2", "proposed"),
        (3, "ORB-3", "proposed"),
        (4, "ORB-42", "reminder"),
    ]
    # Each item carries the token core would verify for that request.
    items = item_lines(body)
    assert sorted(items) == [1, 2, 3, 4]
    for number, (request_id, token) in items.items():
        assert token == rig.core.token(request_id), number
        assert token not in json.dumps(output)
    assert items[4][0] == RID

    lines = body.splitlines()
    proposed = lines.index("Proposed tasks awaiting approval:")
    reminders = lines.index("Reminders: open requests nearing expiry or to be re-sent:")
    assert proposed < reminders
    listing = lines[proposed:reminders]
    assert "  1. Add the digest (ORB-1)" in listing
    assert "     Send one daily mail." in listing  # the heading line is skipped
    assert "     Pilot: held on 1 pilot warning(s): scope" in listing
    assert "     Pilot: no pilot warnings" in listing
    assert "  3. Rename a flag (ORB-3)" in listing
    assert "  4. Retire the old API (ORB-42)" in lines[reminders:]
    # Expiry counts from the send.
    assert "     Request dr-0000000000000002, expires 2026-11-01T09:00:00Z (in 3d 0h)" in listing
    assert any("expires 2026-10-29T20:00:00Z (in 11h 0m)" in line for line in lines[reminders:])
    assert "Reply with: approve, reject" in body
    assert "`approve 1,3`" in body
    assert "Dashboard" not in body  # the per-item links carry it
    assert "http://dk-server-2:7300/tasks/ORB-1" in body


def test_a_second_digest_raises_nothing_new_and_lists_the_same_requests(rig: Rig) -> None:
    propose(rig)
    first = ok(rig, "digest")
    second = ok(rig, "digest")
    assert first["raised"] == 3 and second["raised"] == 0
    assert [i["request_id"] for i in first["items"]] == [i["request_id"] for i in second["items"]]
    assert len(rig.smtp.sent) == 2


def test_a_digest_with_nothing_to_list_sends_no_mail(rig: Rig) -> None:
    output = ok(rig, "digest")
    assert output == {
        "sent": False,
        "digest_id": None,
        "reason": "nothing_to_list",
        "items": [],
        "confirmations": 0,
        "raised": 0,
        "omitted": 0,
    }
    assert rig.smtp.sent == []
    assert ("orbit.decision.digest", {"channel": "email"}) in rig.core.calls


def test_a_digest_with_the_channel_disabled_in_core_sends_no_mail(rig: Rig) -> None:
    propose(rig)
    rig.core.digest_enabled = False
    assert ok(rig, "digest")["reason"] == "nothing_to_list"
    assert rig.smtp.sent == []


def test_a_digest_that_core_refuses_fails_without_mailing(rig: Rig) -> None:
    propose(rig)
    rig.core.revoked = True
    response = run(rig, "digest")
    assert response["ok"] is False
    assert response["error"]["code"] == "orbit_refused"
    assert response["error"]["detail"] == {"refusal": "channel_revoked"}
    assert rig.smtp.sent == []


def test_a_digest_send_failure_is_retryable_and_keeps_the_confirmations(rig: Rig) -> None:
    propose(rig)
    rig.smtp.fail_send = OSError("connection reset")
    response = run(rig, "digest")
    assert response["error"]["code"] == "smtp_failed"
    assert response["error"]["retryable"] is True
    rig.smtp.fail_send = None
    assert ok(rig, "digest")["sent"] is True
    assert len(rig.smtp.sent) == 1


def test_a_digest_caps_its_items_and_leaves_the_rest_for_the_next(rig: Rig) -> None:
    for number in range(1, 53):
        rig.core.tasks[f"ORB-{number}"] = proposed_task(f"ORB-{number}", f"Task {number}", "x")
    output = ok(rig, "digest")
    assert len(output["items"]) == 50
    assert output["omitted"] == 2
    assert "2 more request(s) did not fit" in body_of(rig.smtp.sent[0])


def test_the_digest_renders_record_text_as_single_lines(rig: Rig) -> None:
    rig.core.tasks["ORB-1"] = proposed_task(
        "ORB-1", "evil\r\nBcc: victim@example.net", "line one\r\nTo: other@example.net"
    )
    ok(rig, "digest")
    mail = parse_sent(rig.smtp.sent[0])
    assert mail["Bcc"] is None
    assert "orbit-item: 2" not in body_of(rig.smtp.sent[0])
    assert "  1. evil Bcc: victim@example.net (ORB-1)" in body_of(rig.smtp.sent[0])


# --- criterion 2: digest replies ----------------------------------------------------------


def test_approve_1_3_and_reject_2_submit_per_item(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    items = item_lines(body)
    answer(rig, "approve 1,3\nreject 2\nThe typo fix is not worth a task.", body, digest_id)

    output = poll(rig)

    submits = rig.core.submits()
    assert [(s["id"], s["answer"], s["token"]) for s in submits] == [
        (items[1][0], "approve", items[1][1]),
        (items[2][0], "reject", items[2][1]),
        (items[3][0], "approve", items[3][1]),
    ]
    assert {s["note"] for s in submits} == {"The typo fix is not worth a task."}
    assert {s["sender"] for s in submits} == {OPERATOR}
    assert [rig.core.tasks[t]["status"] for t in ("ORB-1", "ORB-2", "ORB-3")] == [
        "backlog",
        "rejected",
        "backlog",
    ]
    assert output["counts"] == {"applied": 1, "refused": 0, "dropped": 0}
    (message,) = output["messages"]
    assert message["outcome"] == "applied"
    assert message["digest_id"] == digest_id
    assert [(i["number"], i["outcome"], i["answer"]) for i in message["items"]] == [
        (1, "applied", "approve"),
        (2, "applied", "reject"),
        (3, "applied", "approve"),
    ]
    assert len(rig.smtp.sent) == 1  # no per-item confirmation mail
    assert [m.folder for m in rig.imap.messages.values()] == [PROCESSED]


def test_only_the_named_items_are_submitted(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    answer(rig, "approve 2", body, digest_id)
    poll(rig)
    assert [s["id"] for s in rig.core.submits()] == [item_lines(body)[2][0]]
    assert rig.core.tasks["ORB-1"]["status"] == "proposed"
    assert rig.core.tasks["ORB-3"]["status"] == "proposed"


def test_a_digest_word_other_than_approve_reaches_the_item_that_takes_it(rig: Rig) -> None:
    propose(rig)
    rig.core.add(pilot_request(), task("held"))
    body, digest_id = send_digest(rig)
    items = item_lines(body)
    number = next(n for n, (rid, _) in items.items() if rid == RID)
    answer(rig, f"clear {number}", body, digest_id)
    poll(rig)
    assert [(s["id"], s["answer"]) for s in rig.core.submits()] == [(RID, "clear")]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("approve", "ambiguous"),
        ("approve all", "ambiguous"),
        ("Approve.", "ambiguous"),
        ("approve 7", "`7` is not one of the numbered items"),
        ("approve 0", "`0` is not one of the numbered items"),
        ("approve 1,9", "`9` is not one of the numbered items"),
        ("approve 1\nreject 1", "item 1 is both approved and rejected"),
        ("looks good", "not a digest answer"),
        ("2", "not a digest answer"),
        ("clear 1", "item 1 does not take `clear`"),
    ],
)
def test_a_refused_digest_reply_relists_and_submits_nothing(
    rig: Rig, text: str, fragment: str
) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    uid = answer(rig, text, body, digest_id)

    output = poll(rig)

    assert rig.core.submits() == []  # nothing submitted, so no token reached core
    assert rig.core.consumed == set()
    (message,) = output["messages"]
    assert message["outcome"].startswith("refused_")
    assert fragment in message["reason"]
    assert message["mail"] == "sent"
    relisting = rig.smtp.sent[-1]
    assert parse_sent(relisting)["Subject"] == f"[orbit] digest reply not applied [{digest_id}]"
    text_body = body_of(relisting)
    assert fragment in text_body or fragment == "ambiguous"
    assert "Nothing was applied" in text_body
    assert "No token was used" in text_body
    # The same items, numbers and tokens, so the corrected reply works.
    assert item_lines(text_body) == item_lines(body)
    assert "  2. Fix typos (ORB-2)" in text_body
    stored = rig.imap.messages[uid]
    assert stored.folder == "INBOX" and "\\Flagged" in stored.flags


def test_a_corrected_reply_applies_after_a_refused_one(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    answer(rig, "approve", body, digest_id)
    poll(rig)
    relisting = body_of(rig.smtp.sent[-1])
    rig.imap.messages[1].flags.add("\\Seen")
    answer(rig, "approve 1", relisting, digest_id)
    output = poll(rig)
    assert output["counts"]["applied"] == 1
    assert [s["id"] for s in rig.core.submits()] == [item_lines(body)[1][0]]


def test_digest_refusal_mails_stop_after_three_and_wait_an_hour(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    mails: list[str] = []
    for _ in range(5):
        answer(rig, "approve", body, digest_id)
        output = poll(rig)
        mails.append(output["messages"][0]["mail"])
        rig.clock.now = rig.clock.now.replace(minute=rig.clock.now.minute + 1)
    # One mail an hour per digest, so the rest are rate limited before the guard counts four.
    assert mails[0] == "sent"
    assert mails[1:3] == ["rate_limited", "rate_limited"]
    assert mails[3] == "loop_guard"
    assert len(rig.smtp.sent) == 2


def test_a_digest_reply_without_the_item_lines_is_refused_with_no_listing(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    answer(rig, "approve 1", body, digest_id, keep_lines=False)
    # The subject still names the digest, so the reply is answered, not dropped.
    output = poll(rig)
    (message,) = output["messages"]
    assert message["outcome"] == "refused_unparseable"
    assert "none of the digest's orbit-item lines" in message["reason"]
    assert rig.core.submits() == []
    assert "orbit-item:" not in body_of(rig.smtp.sent[-1])


def test_a_digest_reply_that_also_carries_a_single_request_token_is_dropped(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    raw = digest_reply("approve 1", body + f"\norbit-token: {'A' * 26}", digest_id=digest_id)
    rig.imap.deliver(raw)
    output = poll(rig)
    assert output["messages"][0]["reason"] == "malformed_digest"
    assert rig.core.submits() == []


def test_a_digest_reply_from_a_forged_sender_is_dropped_silently(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    answer(rig, "approve 1,2,3", body, digest_id, sender="mallory@example.net")
    output = poll(rig)
    assert output["messages"][0]["reason"] == "sender_not_allowed"
    assert rig.core.submits() == []
    assert len(rig.smtp.sent) == 1


def test_an_item_core_refuses_does_not_stop_the_others(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    items = item_lines(body)
    rig.core.requests[items[2][0]]["state"] = "expired"
    answer(rig, "approve 1,2,3", body, digest_id)

    output = poll(rig)

    (message,) = output["messages"]
    assert message["outcome"] == "applied_partial"
    assert [(i["number"], i["outcome"]) for i in message["items"]] == [
        (1, "applied"),
        (2, "refused"),
        (3, "applied"),
    ]
    assert message["items"][1]["refusal"] == "request_expired"
    assert message["mail"] == "sent"
    refusal = rig.smtp.sent[-1]
    assert parse_sent(refusal)["Subject"] == f"[orbit] digest answers not applied [{digest_id}]"
    assert "2 answer(s) applied, 1 refused" in body_of(refusal)
    assert "2. ORB-2" in body_of(refusal)
    assert output["counts"] == {"applied": 1, "refused": 0, "dropped": 0}


def test_a_halted_digest_reply_resumes_without_applying_an_item_twice(rig: Rig) -> None:
    class Flaky(FakeCore):
        submits_seen = 0

        def _submit(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.submits_seen += 1
            if self.submits_seen == 2:
                raise CallbackError("orbit tool run was killed", unknown_outcome=True)
            return super()._submit(payload)

    rig = Rig(Flaky(), rig.smtp, rig.imap, rig.clock, rig.state_dir)
    propose(rig)
    body, digest_id = send_digest(rig)
    uid = answer(rig, "approve 1,2", body, digest_id)

    first = run(rig, "poll")
    assert first["error"]["code"] == "orbit_callback_failed"
    assert first["error"]["retryable"] is True
    assert rig.imap.messages[uid].folder == "INBOX"
    assert "\\Seen" not in rig.imap.messages[uid].flags

    output = poll(rig)

    (message,) = output["messages"]
    assert [(i["number"], i["outcome"]) for i in message["items"]] == [
        (1, "applied_earlier"),
        (2, "applied"),
    ]
    assert rig.imap.messages[uid].folder == PROCESSED
    assert len(rig.core.consumed) == 2
    # Each applied item is confirmed once in the next digest.
    ok(rig, "digest")
    assert body_of(rig.smtp.sent[-1]).count("Applied: Approve") == 0
    assert body_of(rig.smtp.sent[-1]).count("Approve (approve)") == 2


# --- criterion 3: batching and quiet hours ------------------------------------------------


def two_requests(rig: Rig) -> None:
    rig.core.add(triage_request(), task("blocked"))
    other = pilot_request("dr-aaaaaaaaaaaaaaaa", task_id="ORB-43")
    rig.core.add(other, {**task("held"), "id": "ORB-43", "title": "Other task"})


def test_immediate_mails_each_request_as_raised(rig: Rig) -> None:
    two_requests(rig)
    output = ok(rig, "notify", digest_mode="immediate")
    assert [s["gate"] for s in output["sent"]] == ["block_triage", "pilot_admission"]
    assert len(rig.smtp.sent) == 2


def test_daily_mails_block_triage_at_once_and_holds_the_other_gates(rig: Rig) -> None:
    two_requests(rig)
    output = ok(rig, "notify", digest_mode="daily")
    assert [s["gate"] for s in output["sent"]] == ["block_triage"]
    assert output["skipped"] == [{"request_id": "dr-aaaaaaaaaaaaaaaa", "reason": "held_for_digest"}]
    assert len(rig.smtp.sent) == 1
    assert RID in parse_sent(rig.smtp.sent[0])["Subject"]
    # Held, not lost: the digest carries it, and then notify has nothing left to mail.
    ok(rig, "digest", digest_mode="daily")
    digest = body_of(rig.smtp.sent[-1])
    assert "Also waiting for your decision:" in digest
    assert "  1. Other task (ORB-43)" in digest
    again = ok(rig, "notify", digest_mode="daily")
    assert again["sent"] == []
    assert len(rig.smtp.sent) == 2


def test_off_sends_no_request_mail_and_no_digest(rig: Rig) -> None:
    two_requests(rig)
    propose(rig)
    notified = ok(rig, "notify", digest_mode="off")
    assert notified["sent"] == []
    assert {s["reason"] for s in notified["skipped"]} == {"mode_off"}
    digest = ok(rig, "digest", digest_mode="off")
    assert digest["sent"] is False and digest["reason"] == "mode_off"
    assert rig.smtp.sent == []
    assert rig.core.calls == [("orbit.decision.list", {})]  # the digest never asked core


def test_a_reply_to_earlier_mail_is_still_read_when_batching_is_off(rig: Rig) -> None:
    rig.core.add(triage_request(), task("blocked"))
    rig.imap.deliver(reply("3", token=rig.core.token(RID)))
    output = poll(rig, digest_mode="off")
    assert output["counts"]["applied"] == 1


def test_quiet_hours_defer_a_2300_send_to_0700_and_expiry_counts_from_0700(rig: Rig) -> None:
    # Raised 22:30, valid for 72 hours: 2026-11-01 22:30.
    request = pilot_request(raised_at="2026-10-29T22:30:00Z", expires_at="2026-11-01T22:30:00Z")
    rig.core.add(request, task("held"))
    at(rig, datetime(2026, 10, 29, 23, 0, tzinfo=UTC))

    response = run(rig, "notify", quiet_hours="22:00-07:00", digest_mode="immediate")

    assert response["ok"] is False
    assert response["error"]["code"] == "quiet_hours"
    assert response["error"]["retryable"] is True
    assert response["error"]["detail"] == {"deferred": 1, "until": "2026-10-30T07:00:00+00:00"}
    assert rig.smtp.sent == []

    at(rig, datetime(2026, 10, 29, 23, 30, tzinfo=UTC))
    assert run(rig, "notify", quiet_hours="22:00-07:00", digest_mode="immediate")["ok"] is False
    assert rig.smtp.sent == []

    at(rig, datetime(2026, 10, 30, 6, 59, tzinfo=UTC))
    assert run(rig, "notify", quiet_hours="22:00-07:00", digest_mode="immediate")["ok"] is False

    at(rig, datetime(2026, 10, 30, 7, 0, tzinfo=UTC))
    output = ok(rig, "notify", quiet_hours="22:00-07:00", digest_mode="immediate")
    assert len(output["sent"]) == 1
    body = body_of(rig.smtp.sent[0])
    # 07:00 on the 30th to 22:30 on 1 November is 2 days and 15 hours; from 23:00 it was 2d 23h.
    assert "expires 2026-11-01T22:30:00Z (in 2d 15h)" in body


def test_a_request_that_expires_while_deferred_is_never_mailed(rig: Rig) -> None:
    rig.core.add(
        pilot_request(expires_at="2026-10-30T05:00:00Z", raised_at="2026-10-27T05:00:00Z"),
        task("held"),
    )
    at(rig, datetime(2026, 10, 30, 7, 0, tzinfo=UTC))
    # Core would read it as expired; a stale listing must not mail a dead token either.
    output = ok(rig, "notify", quiet_hours="22:00-07:00", digest_mode="immediate")
    assert output["sent"] == []
    assert output["skipped"] == [{"request_id": RID, "reason": "expired"}]
    assert rig.smtp.sent == []


def test_quiet_hours_with_nothing_to_send_do_not_fail_the_run(rig: Rig) -> None:
    at(rig, datetime(2026, 10, 29, 23, 0, tzinfo=UTC))
    output = ok(rig, "notify", quiet_hours="22:00-07:00")
    assert output == {"sent": [], "skipped": []}


def test_the_digest_waits_out_the_quiet_hours_before_raising_anything(rig: Rig) -> None:
    propose(rig)
    at(rig, datetime(2026, 10, 29, 23, 0, tzinfo=UTC))

    response = run(rig, "digest", quiet_hours="22:00-07:00")

    assert response["error"]["code"] == "quiet_hours"
    assert response["error"]["retryable"] is True
    assert rig.core.calls == []  # nothing raised, so nothing is timed from the wrong moment
    assert rig.smtp.sent == []

    at(rig, datetime(2026, 10, 30, 7, 0, tzinfo=UTC))
    ok(rig, "digest", quiet_hours="22:00-07:00")
    # Raised at 07:00, so the 72 hours count from the send.
    assert "expires 2026-11-02T07:00:00Z (in 3d 0h)" in body_of(rig.smtp.sent[0])


def test_quiet_hours_read_the_hosts_clock_and_may_not_wrap(rig: Rig) -> None:
    propose(rig)
    local = datetime.fromisoformat("2026-10-29T13:30:00-07:00")
    at(rig, local)
    response = run(rig, "digest", quiet_hours="13:00-14:00")
    assert response["error"]["detail"] == {"until": "2026-10-29T14:00:00-07:00"}
    at(rig, datetime.fromisoformat("2026-10-29T14:00:00-07:00"))
    assert ok(rig, "digest", quiet_hours="13:00-14:00")["sent"] is True


@pytest.mark.parametrize("value", ["22:00", "25:00-07:00", "22:00-07:60", "07:00-07:00", "night"])
def test_invalid_quiet_hours_are_a_config_error(rig: Rig, value: str) -> None:
    response = run(rig, "notify", quiet_hours=value)
    assert response["error"]["code"] == "not_configured"
    assert "quiet_hours" in response["error"]["message"]


def test_an_unknown_batching_mode_is_a_config_error(rig: Rig) -> None:
    response = run(rig, "digest", digest_mode="weekly")
    assert response["error"]["code"] == "not_configured"
    assert "digest_mode" in response["error"]["message"]


def test_the_digest_re_mails_open_requests_after_a_key_rotation(rig: Rig) -> None:
    rig.core.add(triage_request(), task("blocked"))
    ok(rig, "notify", digest_mode="daily", key_epoch=3)
    old = rig.core.token(RID, 3)
    rig.core.epoch = 4
    ok(rig, "digest", digest_mode="daily", key_epoch=4)
    body = body_of(rig.smtp.sent[-1])
    ((request_id, token),) = item_lines(body).values()
    assert request_id == RID
    assert token == rig.core.token(RID, 4) != old
    # Mailed under the current epoch now, so the next digest does not repeat it.
    assert ok(rig, "digest", digest_mode="daily", key_epoch=4)["reason"] == "nothing_to_list"


def test_an_old_pending_request_is_not_re_mailed_in_the_same_epoch(rig: Rig) -> None:
    rig.core.add(triage_request(), task("blocked"))
    ok(rig, "notify", digest_mode="daily")
    assert ok(rig, "digest", digest_mode="daily")["reason"] == "nothing_to_list"


# --- criterion 4: batched confirmations ---------------------------------------------------


def test_several_digest_answers_in_one_poll_are_confirmed_once_in_the_next_digest(
    rig: Rig,
) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    answer(rig, "approve 1,3", body, digest_id)
    rig.imap.deliver(digest_reply("reject 2", body, digest_id=digest_id))

    output = poll(rig)

    assert [m["outcome"] for m in output["messages"]] == ["applied", "applied"]
    assert len(rig.smtp.sent) == 1  # the poll mailed no confirmation of its own
    assert len(rig.core.submits()) == 3

    result = ok(rig, "digest")

    assert result["sent"] is True and result["items"] == []
    assert result["confirmations"] == 3
    assert len(rig.smtp.sent) == 2  # one batched mail for all three answers
    batch = body_of(rig.smtp.sent[-1])
    assert "Orbit decision digest: 0 to decide, 3 applied since the last digest." in batch
    assert "Applied since the last digest:" in batch
    assert "  - ORB-1 Add the digest: Approve (approve); task is now backlog" in batch
    assert "  - ORB-2 Fix typos: Reject (reject); task is now rejected" in batch
    assert "  - ORB-3 Rename a flag: Approve (approve); task is now backlog" in batch
    assert "orbit-item:" not in batch and "Reply with" not in batch
    # Delivered once, then forgotten.
    assert ok(rig, "digest")["reason"] == "nothing_to_list"
    assert len(rig.smtp.sent) == 2


def test_confirmations_ride_along_with_the_digest_that_has_items(rig: Rig) -> None:
    propose(rig)
    body, digest_id = send_digest(rig)
    answer(rig, "approve 1", body, digest_id)
    poll(rig)
    status = ok(rig, "status")
    assert status["activity"]["pending_confirmations"] == 1
    rig.core.tasks["ORB-4"] = proposed_task("ORB-4", "A new proposal", "Fresh.")

    result = ok(rig, "digest")

    assert result["confirmations"] == 1
    batch = body_of(rig.smtp.sent[-1])
    assert "Orbit decision digest: 3 to decide, 1 applied" in batch
    assert batch.index("Applied since the last digest:") < batch.index(
        "Proposed tasks awaiting approval:"
    )
    assert ok(rig, "status")["activity"]["pending_confirmations"] == 0
    assert ok(rig, "status")["activity"]["last_digest"]["confirmations"] == 1


def test_a_single_request_reply_still_gets_its_own_confirmation(rig: Rig) -> None:
    rig.core.add(triage_request(), task("blocked"))
    rig.imap.deliver(reply("3", token=rig.core.token(RID)))
    poll(rig)
    assert "Applied: Dismiss (dismiss) on ORB-42." in body_of(rig.smtp.sent[0])
    assert ok(rig, "status")["activity"]["pending_confirmations"] == 0


def test_the_pending_confirmations_are_bounded(rig: Rig) -> None:
    from decision_mail.state import empty, queue_confirmation

    state = empty()
    for number in range(MAX_PENDING + 5):
        queue_confirmation(
            state,
            {
                "request_id": f"dr-{number:016x}",
                "task_id": "ORB-1",
                "title": "t",
                "answer": "approve",
                "label": "Approve",
                "task_status": "backlog",
                "at": NINE.isoformat(),
            },
        )
    assert len(state["pending_confirmations"]) == MAX_PENDING
    assert state["pending_confirmations"][0]["request_id"] == f"dr-{5:016x}"


def test_approval_requests_are_the_only_ones_a_digest_asks_core_to_raise(rig: Rig) -> None:
    propose(rig)
    rig.core.add(approval_request(RID, "ORB-1", raised_at=NINE), rig.core.tasks["ORB-1"])
    ok(rig, "digest")
    assert [tool for tool, _ in rig.core.calls if tool.startswith("orbit.decision.")] == [
        "orbit.decision.digest",
        "orbit.decision.list",
    ]


def test_digest_poll_notify_and_status_outputs_satisfy_their_schemas(rig: Rig) -> None:
    import jsonschema

    plugin = Path(__file__).resolve().parents[1] / ".orbit-plugin" / "schemas"

    def check(tool: str, output: Any) -> None:
        schema = json.loads((plugin / f"{tool}.response.json").read_text())
        jsonschema.validate(output, schema)

    propose(rig)
    two_requests(rig)
    check("notify", ok(rig, "notify"))
    sent = ok(rig, "digest")
    check("digest", sent)
    body = body_of(rig.smtp.sent[-1])
    items = item_lines(body)
    rig.core.requests[items[2][0]]["state"] = "expired"
    answer(rig, "approve 1,2", body, sent["digest_id"])
    answer(rig, "approve", body, sent["digest_id"])
    check("poll", poll(rig))
    check("status", ok(rig, "status"))
    check("digest", ok(rig, "digest"))
    check("digest", ok(rig, "digest", digest_mode="off"))
