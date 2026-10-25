"""notify and poll end to end over fake SMTP, IMAP and core (task ORB-18304 criteria 1–4)."""

from __future__ import annotations

import json
import smtplib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from decision_mail import inbound
from decision_mail.orbit_api import CallbackError
from decision_mail.protocol import serve
from decision_mail.render import REFUSAL_TEXT
from decision_mail.tokens import fingerprint
from fakes import (
    AUTHSERV,
    KEY_HEX,
    MAILBOX,
    OPERATOR,
    Clock,
    FakeCore,
    ImapServer,
    Rig,
    SmtpServer,
    body_of,
    config,
    core_token,
    envelope,
    parse_sent,
    pilot_request,
    reply,
    secrets,
    stamp,
    task,
    triage_request,
)

RID = "dr-0123456789abcdef"
PROCESSED = "Orbit/Processed"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(FakeCore(), SmtpServer(), ImapServer(), Clock(), tmp_path / "state")


def run(rig: Rig, tool: str, tool_input: dict[str, Any] | None = None, **cfg: Any) -> Any:
    raw = envelope(tool, tool_input, config=config(**cfg), secrets=secrets())
    return json.loads(serve(raw, rig.factory()))


def poll(rig: Rig, **cfg: Any) -> dict[str, Any]:
    response = run(rig, "poll", **cfg)
    assert response["ok"] is True, response
    output: dict[str, Any] = response["output"]
    return output


def only_message(output: dict[str, Any]) -> dict[str, Any]:
    (message,) = output["messages"]
    result: dict[str, Any] = message
    return result


# --- criterion 1: notify ------------------------------------------------------------------


def test_notify_mails_one_request_with_the_core_token(rig: Rig) -> None:
    rig.core.add(triage_request(), task("blocked"))
    response = run(rig, "notify")

    assert response == {
        "ok": True,
        "output": {
            "sent": [{"request_id": RID, "task_id": "ORB-42", "gate": "block_triage"}],
            "skipped": [],
        },
    }
    (sent,) = rig.smtp.sent
    mail = parse_sent(sent)
    token = rig.core.token(RID)
    # Computed outside the plugin from the documented construction (tests/test_tokens.py).
    assert token == "TZMGKITEFF35TIXW6EFN6IN6DR"
    assert mail["Subject"] == f"[orbit] decision needed: Retire the old API (ORB-42) [{RID}]"
    assert mail["To"] == OPERATOR
    assert mail["Reply-To"] == f"orbit+{token}@example.org"
    assert mail["Auto-Submitted"] == "auto-generated"
    lines = body_of(sent).splitlines()
    assert lines[:2] == [f"orbit-token: {token}", f"orbit-request: {RID}"]
    listing = lines[lines.index("Answers:") + 1 :]
    assert listing[:6] == [
        "  1. Drop criterion 2",
        "     The old API stays retired.",
        "  2. Keep the API",
        "     The retirement is reverted.",
        "  3. Dismiss (or reply `dismiss`)",
        "     Leave blocked.",
    ]
    assert "Reply with just the number (or word)" in body_of(sent)
    assert lines[-1] == "Dashboard: http://dk-server-2:7300/tasks/ORB-42"
    # The tool output carries no token; only the mail does.
    assert token not in json.dumps(response)


def test_notify_mails_each_request_once(rig: Rig) -> None:
    rig.core.add(triage_request(), task())
    run(rig, "notify")
    again = run(rig, "notify")
    assert again["output"] == {
        "sent": [],
        "skipped": [{"request_id": RID, "reason": "already_notified"}],
    }
    assert len(rig.smtp.sent) == 1
    resent = run(rig, "notify", {"request_ids": [RID], "resend": True})
    assert len(resent["output"]["sent"]) == 1
    assert len(rig.smtp.sent) == 2


def test_notify_skips_closed_and_unknown_requests(rig: Rig) -> None:
    rig.core.add(pilot_request(state="answered"), task())
    response = run(rig, "notify", {"request_ids": [RID, "dr-ffffffffffffffff"]})
    assert response["output"]["sent"] == []
    assert sorted(item["request_id"] for item in response["output"]["skipped"]) == [
        RID,
        "dr-ffffffffffffffff",
    ]
    assert rig.smtp.sent == []


def test_notify_without_a_dashboard_url_points_at_the_decision_list(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    run(rig, "notify", dashboard_url="")
    assert "The dashboard's decision list also shows this request." in body_of(rig.smtp.sent[0])


def test_notify_send_failure_is_retryable_and_not_recorded(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.smtp.fail_send = smtplib.SMTPServerDisconnected("connection reset")
    response = run(rig, "notify")
    assert response["ok"] is False
    assert response["error"]["code"] == "smtp_failed"
    assert response["error"]["retryable"] is True
    rig.smtp.fail_send = None
    assert len(run(rig, "notify")["output"]["sent"]) == 1


def test_notify_still_mails_when_the_task_cannot_be_read(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.core.tasks.clear()
    assert len(run(rig, "notify")["output"]["sent"]) == 1
    assert "(untitled task) (ORB-42)" in parse_sent(rig.smtp.sent[0])["Subject"]


def test_notify_record_text_cannot_inject_headers(rig: Rig) -> None:
    rig.core.add(pilot_request(), {**task(), "title": "evil\r\nBcc: victim@example.net"})
    run(rig, "notify")
    mail = parse_sent(rig.smtp.sent[0])
    assert mail["Bcc"] is None
    assert "evil Bcc: victim@example.net" in mail["Subject"]


# --- criterion 2: poll applies an authenticated reply ------------------------------------


def test_poll_submits_an_authenticated_reply_and_confirms(rig: Rig) -> None:
    rig.core.add(pilot_request(), task("held"))
    token = rig.core.token(RID)
    note = "The finding was fixed upstream.\nSee commit abc123."
    uid = rig.imap.deliver(reply(f"2\n{note}\n", token=token))

    output = poll(rig)

    assert rig.core.submits() == [
        {
            "id": RID,
            "channel": "email",
            "answer": "clear",
            "token": token,
            "sender": OPERATOR,
            "auth_results": stamp(),
            "note": note,
        }
    ]
    assert output["counts"] == {"applied": 1, "refused": 0, "dropped": 0}
    assert only_message(output) == {
        "uid": str(uid),
        "outcome": "applied",
        "request_id": RID,
        "sender": OPERATOR,
        "answer": "clear",
        "mail": "sent",
    }
    (confirmation,) = rig.smtp.sent
    mail = parse_sent(confirmation)
    assert mail["To"] == OPERATOR
    assert mail["Subject"] == f"[orbit] decision applied: ORB-42 [{RID}]"
    assert mail["Auto-Submitted"] == "auto-replied"
    assert "Applied: Clear (clear) on ORB-42." in body_of(confirmation)
    assert "Task ORB-42 is now backlog." in body_of(confirmation)
    stored = rig.imap.messages[uid]
    assert stored.folder == PROCESSED
    assert "\\Seen" in stored.flags
    assert token not in json.dumps(output)


def test_poll_reads_a_word_answer_and_a_body_token(rig: Rig) -> None:
    rig.core.add(triage_request(), task())
    token = rig.core.token(RID)
    rig.imap.deliver(reply("Dismiss\n", token=token, body_token=True))
    rig.imap.deliver(reply("dismiss", token=None, body_token=False, request_id=RID))
    output = poll(rig, plus_addressing=False)
    assert rig.core.submits()[0]["answer"] == "dismiss"
    assert [m["outcome"] for m in output["messages"]] == ["applied", "dropped"]
    assert output["messages"][1]["reason"] == "malformed_token"


def test_poll_copies_when_the_server_cannot_move(rig: Rig) -> None:
    rig.imap.capabilities = ("IMAP4REV1",)
    rig.core.add(pilot_request(), task())
    uid = rig.imap.deliver(reply("1", token=rig.core.token(RID)))
    poll(rig)
    assert "\\Seen" in rig.imap.messages[uid].flags
    assert [s.folder for s in rig.imap.messages.values()] == ["INBOX", PROCESSED]
    assert poll(rig)["messages"] == []


def test_poll_recovers_a_submission_that_landed_before_filing(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    token = rig.core.token(RID)
    uid = rig.imap.deliver(reply("1", token=token))
    # Core applied this token earlier; the poll stopped before filing the mail.
    rig.core.call(
        "orbit.decision.submit",
        {
            "id": RID,
            "channel": "email",
            "answer": "approve_anyway",
            "token": token,
            "sender": OPERATOR,
            "auth_results": stamp(),
        },
    )
    rig.core.calls.clear()

    output = poll(rig)

    assert only_message(output)["outcome"] == "applied_earlier"
    assert rig.core.submits() == []
    assert rig.imap.messages[uid].folder == PROCESSED
    assert "Applied: Approve anyway" in body_of(rig.smtp.sent[0])


# --- criterion 3: plugin-side refusals ----------------------------------------------------


def _forged_sender() -> bytes:
    return reply("1", token="A" * 26, sender="mallory@example.net")


DROPS: dict[str, tuple[str, Any]] = {
    "non_allowlisted_sender": ("sender_not_allowed", _forged_sender),
    "two_from_addresses": (
        "sender_not_allowed",
        lambda token: reply("1", token=token, sender=f"{OPERATOR}, mallory@example.net"),
    ),
    "dkim_fail": ("authentication_failed", lambda t: reply("1", token=t, stamps=(stamp("fail"),))),
    "spf_softfail": (
        "authentication_failed",
        lambda t: reply("1", token=t, stamps=(stamp(spf="softfail"),)),
    ),
    "dmarc_fail": (
        "authentication_failed",
        lambda t: reply("1", token=t, stamps=(stamp(dmarc="fail"),)),
    ),
    "attacker_header_below_the_stamp": (
        "authentication_failed",
        lambda t: reply(
            "1", token=t, stamps=(stamp("fail", "fail", "fail"),), attacker_stamps=(stamp(),)
        ),
    ),
    "only_another_servers_stamp": (
        "authentication_failed",
        lambda t: reply("1", token=t, stamps=(stamp(authserv="mx.attacker.net"),)),
    ),
    "dkim_pass_for_another_domain": (
        "authentication_failed",
        lambda t: reply(
            "1",
            token=t,
            stamps=(stamp().replace("header.i=@example.com", "header.i=@attacker.net"),),
        ),
    ),
    "malformed_token": ("malformed_token", lambda t: reply("1", token="NOTATOKEN")),
    "no_token": ("malformed_token", lambda t: reply("1", token=None)),
    "no_request_id": ("unknown_request", lambda t: reply("1", token=t, request_id=None)),
}


@pytest.mark.parametrize("case", sorted(DROPS))
def test_plugin_refusals_drop_without_a_core_call_or_reply(rig: Rig, case: str) -> None:
    reason, build = DROPS[case]
    rig.core.add(pilot_request(), task())
    raw = _forged_sender() if build is _forged_sender else build(rig.core.token(RID))
    uid = rig.imap.deliver(raw)

    output = poll(rig)

    assert only_message(output)["outcome"] == "dropped"
    assert only_message(output)["reason"] == reason
    assert rig.core.calls == []
    assert rig.smtp.sent == []
    stored = rig.imap.messages[uid]
    assert stored.folder == "INBOX"
    assert stored.flags == {"\\Seen"}
    status = run(rig, "status")["output"]
    assert status["activity"]["recent_drops"][-1]["reason"] == reason


def test_an_unknown_request_id_is_dropped_without_a_submit(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    other = "dr-ffffffffffffffff"
    uid = rig.imap.deliver(reply("1", token=rig.core.token(RID), request_id=other))
    output = poll(rig)
    assert only_message(output)["reason"] == "unknown_request"
    # Only the read-only lookup that finds the id unknown; nothing is submitted.
    assert rig.core.calls == [("orbit.decision.show", {"id": other})]
    assert rig.smtp.sent == []
    assert rig.imap.messages[uid].folder == "INBOX"


def test_mail_not_addressed_to_the_channel_is_dropped(rig: Rig) -> None:
    raw = reply("1", token="A" * 26).replace(b"Delivered-To: orbit@example.org\r\n", b"")
    raw = raw.replace(f"orbit+{'A' * 26}@example.org".encode(), b"someone@example.org")
    rig.imap.deliver(raw)
    assert only_message(poll(rig))["reason"] == "not_addressed_to_channel"
    assert rig.core.calls == []


def test_an_unparseable_reply_gets_a_relisting_refusal_and_no_submit(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    uid = rig.imap.deliver(reply("maybe later, let me think", token=rig.core.token(RID)))

    output = poll(rig)

    assert only_message(output)["outcome"] == "refused_unparseable"
    assert only_message(output)["mail"] == "sent"
    assert rig.core.submits() == []
    (refusal,) = rig.smtp.sent
    assert parse_sent(refusal)["To"] == OPERATOR
    assert parse_sent(refusal)["Subject"] == f"[orbit] decision not applied: ORB-42 [{RID}]"
    body = body_of(refusal)
    assert "the token is still valid" in body
    assert "  1. Approve anyway" in body and "  3. Evaluated (or reply `evaluated`)" in body
    assert rig.imap.messages[uid].flags == {"\\Seen", "\\Flagged"}
    assert rig.imap.messages[uid].folder == "INBOX"


def test_a_number_off_the_list_is_refused_locally(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.imap.deliver(reply("7", token=rig.core.token(RID)))
    output = poll(rig)
    assert only_message(output)["outcome"] == "refused_not_allowed"
    assert rig.core.submits() == []
    assert "`7` is not one of the numbered answers" in body_of(rig.smtp.sent[0])


def test_an_html_only_reply_is_unparseable(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.imap.deliver(reply("1", token=rig.core.token(RID), html_only=True))
    assert only_message(poll(rig))["outcome"] == "refused_unparseable"
    assert rig.core.submits() == []


def test_the_fourth_unparseable_reply_sends_no_mail(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    token = rig.core.token(RID)
    mails: list[str] = []
    for _ in range(4):
        rig.imap.deliver(reply("what?", token=token))
        mails.append(only_message(poll(rig))["mail"])
        rig.clock.now += timedelta(hours=2)
    assert mails == ["sent", "sent", "sent", "loop_guard"]
    assert len(rig.smtp.sent) == 3
    assert rig.core.submits() == []


# --- criterion 4: core refusals -----------------------------------------------------------


def _consumed(core: FakeCore) -> str:
    token = core.token(RID)
    core.consumed.add(fingerprint(token))
    return token


def _rotated(core: FakeCore) -> str:
    core.previous_epoch = 2
    return core.token(RID, epoch=2)


def _set_state(state: str) -> Any:
    def arrange(core: FakeCore) -> str:
        core.requests[RID]["state"] = state
        return core.token(RID)

    return arrange


def _revoked(core: FakeCore) -> str:
    core.revoked = True
    return core.token(RID)


def _other_key(core: FakeCore) -> str:
    return core_token("ff" * 32, core.requests[RID], core.epoch)


CORE_REFUSALS: dict[str, tuple[Any, str]] = {
    "token_consumed": (_consumed, "1"),
    "request_expired": (_set_state("expired"), "1"),
    "request_superseded": (_set_state("superseded"), "1"),
    "answer_not_allowed": (lambda core: core.token(RID), "evaluated"),
    "channel_revoked": (_revoked, "1"),
    "token_invalid": (_other_key, "1"),
}


@pytest.mark.parametrize("code", sorted(CORE_REFUSALS))
def test_each_core_refusal_mails_its_explanation_and_files_the_reply(rig: Rig, code: str) -> None:
    arrange, answer = CORE_REFUSALS[code]
    rig.core.add(pilot_request(), task())
    token = arrange(rig.core)
    uid = rig.imap.deliver(reply(answer, token=token))

    output = poll(rig, notify_to="ops@example.com")

    assert len(rig.core.submits()) == 1
    assert only_message(output)["outcome"] == "refused"
    assert only_message(output)["refusal"] == code
    assert only_message(output)["mail"] == "sent"
    (refusal,) = rig.smtp.sent
    mail = parse_sent(refusal)
    # A reused token may mean a stolen one: the operator hears, not the reply's sender.
    assert mail["To"] == ("ops@example.com" if code == "token_consumed" else OPERATOR)
    assert mail["Subject"] == f"[orbit] decision not applied: ORB-42 [{RID}]"
    body = body_of(refusal)
    assert f"({code})" in body
    assert " ".join(REFUSAL_TEXT[code].split()[:6]) in " ".join(body.split())
    stored = rig.imap.messages[uid]
    assert stored.folder == PROCESSED
    assert "\\Seen" in stored.flags
    assert len(rig.imap.messages) == 1  # filed, never deleted


def test_the_rotated_epoch_token_is_reported_consumed(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.imap.deliver(reply("1", token=_rotated(rig.core)))
    assert only_message(poll(rig))["refusal"] == "token_consumed"


def test_refusal_mail_is_rate_limited_per_request_per_hour(rig: Rig) -> None:
    rig.core.add(pilot_request(state="expired"), task())
    token = rig.core.token(RID)
    mails: list[str] = []
    for minutes in (0, 30, 61):
        rig.clock.now += timedelta(minutes=minutes)
        rig.imap.deliver(reply("1", token=token))
        mails.append(only_message(poll(rig))["mail"])
    assert mails == ["sent", "rate_limited", "sent"]
    assert len(rig.smtp.sent) == 2
    assert all(stored.folder == PROCESSED for stored in rig.imap.messages.values())


def test_a_core_authentication_refusal_is_dropped_silently(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.core.allowed_senders = ()
    uid = rig.imap.deliver(reply("1", token=rig.core.token(RID)))
    assert only_message(poll(rig))["reason"] == "sender_not_allowed"
    assert rig.smtp.sent == []
    assert rig.imap.messages[uid].folder == "INBOX"


def test_a_configuration_refusal_halts_and_leaves_the_reply_unseen(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.core.refuse_submit = "decision_channel_disabled"
    uid = rig.imap.deliver(reply("1", token=rig.core.token(RID)))
    response = run(rig, "poll")
    assert response["ok"] is False
    assert response["error"]["code"] == "submit_refused"
    assert rig.imap.messages[uid].flags == set()
    assert rig.smtp.sent == []
    last = run(rig, "status")["output"]["activity"]["last_error"]
    assert last["code"] == "submit_refused"


def test_an_unknown_submit_outcome_halts_and_retries_next_tick(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    token = rig.core.token(RID)
    uid = rig.imap.deliver(reply("1", token=token))
    rig.core.fail["orbit.decision.submit"] = CallbackError("timed out", unknown_outcome=True)
    response = run(rig, "poll")
    assert response["error"]["code"] == "orbit_callback_failed"
    assert response["error"]["retryable"] is True
    assert "may have been applied" in response["error"]["message"]
    assert rig.imap.messages[uid].flags == set()
    rig.core.fail.clear()
    assert only_message(poll(rig))["outcome"] == "applied"


def test_an_imap_login_failure_is_retryable(rig: Rig) -> None:
    rig.imap.fail_login = True
    response = run(rig, "poll")
    assert response["error"]["code"] == "imap_failed"
    assert response["error"]["retryable"] is True


def test_a_failed_confirmation_still_files_the_applied_reply(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    uid = rig.imap.deliver(reply("1", token=rig.core.token(RID)))
    rig.smtp.fail_send = smtplib.SMTPDataError(451, b"try later")
    output = poll(rig)
    assert only_message(output)["mail"] == "failed"
    assert rig.imap.messages[uid].folder == PROCESSED
    assert run(rig, "status")["output"]["activity"]["last_error"]["code"] == "smtp_failed"


def test_a_mail_the_parser_chokes_on_is_dropped_not_retried_forever(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def choke(raw: bytes, cfg: Any) -> Any:
        raise ValueError("malformed header")

    monkeypatch.setattr(inbound, "read", choke)
    uid = rig.imap.deliver(b"From: x\r\n\r\nbody")
    assert only_message(poll(rig))["reason"] == "unreadable_message"
    assert rig.imap.messages[uid].flags == {"\\Seen"}
    assert rig.core.calls == [] and rig.smtp.sent == []


def test_poll_honours_max_messages_oldest_first(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    uids = [rig.imap.deliver(reply("what?", token="A" * 26)) for _ in range(3)]
    output = poll(rig, max_messages=2)
    assert [m["uid"] for m in output["messages"]] == [str(uid) for uid in uids[:2]]


def test_status_reports_ready_and_activity(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    run(rig, "notify")
    output = run(rig, "status")["output"]
    assert output["transport"] == "ready"
    assert output["missing"] == []
    assert output["key_epoch"] == 3
    assert output["activity"]["notified_requests"] == 1
    assert output["activity"]["last_notify"]["sent"] == 1


def test_status_reports_an_invalid_config_without_values(rig: Rig) -> None:
    output = run(rig, "status", allowed_senders=["not an address"])["output"]
    assert output["transport"] == "not_configured"
    assert "allowed_senders" in output["config_error"]


def test_fixture_constants_are_consistent() -> None:
    assert MAILBOX.endswith("@example.org") and AUTHSERV in stamp()
    assert KEY_HEX == FakeCore().key_hex
