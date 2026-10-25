"""Poll and notify record a counter and a bounded event for every drop, refusal and outage.

Events keep the sender, the reason, the request id and the time. They never keep a body,
a token, a secret or a header. The list keeps the newest `MAX_EVENTS` and evicts the oldest.
"""

from __future__ import annotations

import json
import smtplib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from decision_mail import inbound
from decision_mail.protocol import serve
from decision_mail.state import MAX_EVENTS, SCHEMA_VERSION
from decision_mail.tokens import fingerprint
from fakes import (
    IMAP_PASSWORD,
    KEY_HEX,
    OPERATOR,
    SMTP_PASSWORD,
    Clock,
    FakeCore,
    ImapServer,
    Rig,
    SmtpServer,
    config,
    core_token,
    envelope,
    pilot_request,
    reply,
    secrets,
    stamp,
    task,
)

RID = "dr-0123456789abcdef"
BODY = "BODY-SHOULD-NOT-BE-RECORDED please retire nothing"
ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".orbit-plugin"

# Every reason `_drop` emits from the plugin's own checks, before core.
DROP_REASONS = (
    "sender_not_allowed",
    "envelope_sender_not_allowed",
    "authentication_failed",
    "malformed_token",
    "unknown_request",
    "not_addressed_to_channel",
    "unreadable_message",
    "message_too_large",
)

# Every core code the plugin classifies, plus the operator-attention codes that halt a poll.
CORE_CODES = (
    "token_consumed",
    "request_expired",
    "request_superseded",
    "answer_not_allowed",
    "channel_revoked",
    "token_invalid",
    "sender_not_allowed",
    "authentication_failed",
    "request_not_found",
    "decision_channel_disabled",
    "channel_not_registered",
    "caller_not_allowed",
)


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


def observed(rig: Rig) -> dict[str, Any]:
    """What `activity` reports, which is the same counters and events `status` reports."""
    activity = run(rig, "activity")
    assert activity["ok"] is True, activity
    status = run(rig, "status")["output"]["activity"]
    report: dict[str, Any] = activity["output"]
    assert status["counters"] == report["counters"]
    assert status["events"] == report["events"]
    assert status["bound"] == report["bound"] == MAX_EVENTS
    return report


def assert_safe(event: dict[str, Any], *forbidden: str) -> None:
    assert set(event) == {"at", "kind", "reason", "sender", "request_id"}
    blob = json.dumps(event)
    secrets = (
        SMTP_PASSWORD,
        IMAP_PASSWORD,
        KEY_HEX,
        "Authentication-Results",
        "header.b=",
        *forbidden,
    )
    for secret in secrets:
        assert secret not in blob, secret


def _deliver(rig: Rig, raw: bytes) -> None:
    rig.imap.deliver(raw)


def _drop_raw(rig: Rig, reason: str) -> bytes:
    token = "A" * 26
    if reason == "sender_not_allowed":
        return reply(BODY, token=token, sender="mallory@example.net")
    if reason == "envelope_sender_not_allowed":
        raw = reply(BODY, token=token)
        needle = f"Return-Path: <{OPERATOR}>".encode()
        assert needle in raw
        return raw.replace(needle, b"Return-Path: <mallory@example.net>", 1)
    if reason == "authentication_failed":
        return reply(BODY, token=token, stamps=(stamp("fail"),))
    if reason == "malformed_token":
        return reply(BODY, token="NOTATOKEN")
    if reason == "unknown_request":
        return reply(BODY, token=token, request_id=None)
    if reason == "not_addressed_to_channel":
        raw = reply(BODY, token=token)
        raw = raw.replace(b"Delivered-To: orbit@example.org\r\n", b"")
        return raw.replace(f"orbit+{token}@example.org".encode(), b"someone@example.org")
    if reason == "message_too_large":
        return b"x" * (inbound.MAX_MESSAGE_BYTES + 1)
    raise AssertionError(reason)


@pytest.mark.parametrize("reason", DROP_REASONS)
def test_each_drop_reason_counts_and_appends_a_safe_event(
    rig: Rig, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.core.add(pilot_request(), task())
    if reason == "unreadable_message":

        def choke(raw: bytes, cfg: Any) -> Any:
            raise ValueError(f"malformed header {BODY}")

        monkeypatch.setattr(inbound, "read", choke)
        _deliver(rig, reply(BODY, token="A" * 26))
    else:
        _deliver(rig, _drop_raw(rig, reason))

    message = poll(rig)["messages"][0]
    assert message["outcome"] == "dropped"
    assert message["reason"] == reason
    report = observed(rig)
    assert report["counters"]["drops"] == {reason: 1}
    assert report["counters"]["refusals"] == {}
    assert report["counters"]["transport"] == {"smtp": 0, "imap": 0}
    assert report["counters"]["loop_guard"] == 0
    (event,) = report["events"]
    assert event["kind"] == "drop"
    assert event["reason"] == reason
    assert event["at"] == rig.clock.now.isoformat()
    assert_safe(event, BODY, "A" * 26, "NOTATOKEN")
    if reason == "sender_not_allowed":
        assert event["sender"] == "mallory@example.net"
    if reason in ("message_too_large", "unreadable_message", "unknown_request"):
        assert event["request_id"] is None


def test_an_unknown_request_id_is_kept_on_the_drop_event(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    other = "dr-ffffffffffffffff"
    _deliver(rig, reply(BODY, token=rig.core.token(RID), request_id=other))
    poll(rig)
    (event,) = observed(rig)["events"]
    assert event["reason"] == "unknown_request"
    assert event["request_id"] == other
    assert_safe(event, BODY, rig.core.token(RID))


def _arrange_core(core: FakeCore, code: str) -> tuple[str, str]:
    """Return the reply token and the answer word that makes core refuse `code`."""
    core.add(pilot_request(), task())
    if code == "token_consumed":
        token = core.token(RID)
        core.consumed.add(fingerprint(token))
        return token, "1"
    if code == "request_expired":
        core.requests[RID]["state"] = "expired"
        return core.token(RID), "1"
    if code == "request_superseded":
        core.requests[RID]["state"] = "superseded"
        return core.token(RID), "1"
    if code == "answer_not_allowed":
        return core.token(RID), "evaluated"
    if code == "channel_revoked":
        core.revoked = True
        return core.token(RID), "1"
    if code == "token_invalid":
        return core_token("ff" * 32, core.requests[RID], core.epoch), "1"
    if code == "sender_not_allowed":
        core.allowed_senders = ()
        return core.token(RID), "1"
    if code in (
        "authentication_failed",
        "request_not_found",
        "decision_channel_disabled",
        "channel_not_registered",
        "caller_not_allowed",
    ):
        core.refuse_submit = code
        return core.token(RID), "1"
    raise AssertionError(code)


@pytest.mark.parametrize("code", CORE_CODES)
def test_each_core_refusal_code_counts_and_appends_a_safe_event(rig: Rig, code: str) -> None:
    token, answer = _arrange_core(rig.core, code)
    _deliver(rig, reply(answer, token=token, request_id=RID))
    response = run(rig, "poll")
    halts = code in ("decision_channel_disabled", "channel_not_registered", "caller_not_allowed")
    if halts:
        assert response["ok"] is False
        assert response["error"]["code"] == "submit_refused"
    else:
        assert response["ok"] is True, response
    report = observed(rig)
    assert report["counters"]["refusals"] == {code: 1}
    assert report["counters"]["drops"] == {}
    (event,) = report["events"]
    assert event == {
        "at": rig.clock.now.isoformat(),
        "kind": "refusal",
        "reason": code,
        "sender": OPERATOR,
        "request_id": RID,
    }
    assert_safe(event, BODY, token)


def test_notify_smtp_failure_counts_a_transport_event_and_retries(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    rig.smtp.fail_send = smtplib.SMTPServerDisconnected(f"reset {SMTP_PASSWORD}")
    response = run(rig, "notify")
    assert response["ok"] is False
    assert response["error"]["code"] == "smtp_failed"
    report = observed(rig)
    assert report["counters"]["transport"] == {"smtp": 1, "imap": 0}
    (event,) = report["events"]
    assert event["kind"] == "transport"
    assert event["reason"] == "smtp"
    assert event["sender"] is None
    assert event["request_id"] == RID
    assert_safe(event, SMTP_PASSWORD)
    # The request was not recorded as mailed, so the retry sends it and does not count again.
    rig.smtp.fail_send = None
    assert len(run(rig, "notify")["output"]["sent"]) == 1
    assert observed(rig)["counters"]["transport"] == {"smtp": 1, "imap": 0}


def test_poll_imap_failure_counts_a_transport_event(rig: Rig) -> None:
    rig.imap.fail_login = True
    response = run(rig, "poll")
    assert response["ok"] is False
    assert response["error"]["code"] == "imap_failed"
    report = observed(rig)
    assert report["counters"]["transport"] == {"smtp": 0, "imap": 1}
    (event,) = report["events"]
    assert event["kind"] == "transport"
    assert event["reason"] == "imap"
    assert event["sender"] is None
    assert event["request_id"] is None
    assert_safe(event, IMAP_PASSWORD)


def test_poll_smtp_failure_counts_a_transport_event(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    token = rig.core.token(RID)
    _deliver(rig, reply("1", token=token))
    rig.smtp.fail_send = smtplib.SMTPDataError(451, f"later {SMTP_PASSWORD}".encode())
    output = poll(rig)
    assert output["messages"][0]["mail"] == "failed"
    report = observed(rig)
    assert report["counters"]["transport"] == {"smtp": 1, "imap": 0}
    (event,) = report["events"]
    assert event["kind"] == "transport"
    assert event["reason"] == "smtp"
    assert event["sender"] == OPERATOR
    assert event["request_id"] == RID
    assert_safe(event, token, SMTP_PASSWORD)


def test_the_fourth_unparseable_reply_counts_a_loop_guard_suppression(rig: Rig) -> None:
    rig.core.add(pilot_request(), task())
    token = rig.core.token(RID)
    for _ in range(3):
        _deliver(rig, reply(BODY, token=token))
        assert poll(rig)["messages"][0]["mail"] == "sent"
        rig.clock.now += timedelta(hours=2)
    assert observed(rig)["counters"]["loop_guard"] == 0
    assert observed(rig)["events"] == []
    _deliver(rig, reply(BODY, token=token))
    assert poll(rig)["messages"][0]["mail"] == "loop_guard"
    report = observed(rig)
    assert report["counters"]["loop_guard"] == 1
    assert report["counters"]["refusals"] == {}
    (event,) = report["events"]
    assert event["kind"] == "loop_guard"
    assert event["reason"] == "loop_guard"
    assert event["sender"] == OPERATOR
    assert event["request_id"] == RID
    assert_safe(event, BODY, token)


def test_events_past_the_bound_evict_the_oldest_first(rig: Rig) -> None:
    total = MAX_EVENTS + 1
    for index in range(total):
        _deliver(rig, reply("1", token="A" * 26, sender=f"drop{index}@example.net"))
    output = poll(rig, max_messages=total)
    assert len(output["messages"]) == total
    report = observed(rig)
    assert report["counters"]["drops"]["sender_not_allowed"] == total
    assert len(report["events"]) == MAX_EVENTS
    assert report["events"][0]["sender"] == "drop1@example.net"
    assert report["events"][-1]["sender"] == f"drop{MAX_EVENTS}@example.net"
    assert all(event["sender"] != "drop0@example.net" for event in report["events"])
    # One more poll evicts the previous head and keeps the count.
    _deliver(rig, reply("1", token="A" * 26, sender="drop-last@example.net"))
    poll(rig, max_messages=1)
    again = observed(rig)
    assert again["counters"]["drops"]["sender_not_allowed"] == total + 1
    assert len(again["events"]) == MAX_EVENTS
    assert again["events"][0]["sender"] == "drop2@example.net"
    assert again["events"][-1]["sender"] == "drop-last@example.net"


def test_a_version_1_state_file_loads_and_the_next_save_stamps_the_current_schema(rig: Rig) -> None:
    rig.state_dir.mkdir()
    path = rig.state_dir / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "notified": {
                    RID: {
                        "at": "2026-10-29T09:00:00+00:00",
                        "task_id": "ORB-42",
                        "expires_at": "2026-10-31T12:00:00+00:00",
                    }
                },
            }
        )
    )
    assert observed(rig)["counters"]["drops"] == {}
    assert json.loads(path.read_text())["schema_version"] == 1
    _deliver(rig, reply("1", token="A" * 26, sender="mallory@example.net"))
    poll(rig)
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == SCHEMA_VERSION == 3
    assert RID in saved["notified"]
    assert saved["counters"]["drops"]["sender_not_allowed"] == 1
    blob = path.read_text()
    assert SMTP_PASSWORD not in blob
    assert KEY_HEX not in blob


def test_activity_reads_state_and_does_not_create_it(rig: Rig) -> None:
    assert run(rig, "activity")["ok"] is True
    assert not rig.state_dir.exists()


def test_the_bound_is_named_in_the_manifest_and_the_docs() -> None:
    manifest = (PLUGIN / "plugin.yaml").read_text()
    readme = (ROOT / "README.md").read_text()
    runbook = (ROOT / "docs" / "runbook.md").read_text()
    schema = json.loads((PLUGIN / "schemas" / "activity.response.json").read_text())
    assert schema["properties"]["bound"]["const"] == MAX_EVENTS
    assert f"newest {MAX_EVENTS}" in manifest
    assert f"newest {MAX_EVENTS}" in readme
    assert f"newest {MAX_EVENTS}" in runbook
    assert "docs/runbook.md" in readme
    for phrase in (
        "Authentication-Results",
        "orbit plugin add",
        "orbit plugin enable",
        "orbit plugin secret set",
        "--allow-sender",
        "decision-mail-notify",
        "decision-mail-poll",
        "rotate-key",
        "orbit decision channel revoke",
        "smtp_failed",
        "imap_failed",
    ):
        assert phrase in runbook, phrase
