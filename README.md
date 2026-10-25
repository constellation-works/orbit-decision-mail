# orbit-decision-mail

Email transport plugin for Orbit's human decision channel — mails decision requests on held
work and turns authenticated replies into decisions, so held work can be answered from an
inbox.

When Orbit holds work for a human (a blocked task's `propose`/`report` choice, a task-pilot
admission hold, a proposed task awaiting approval), it records a decision request. This plugin
mails each request from a dedicated mailbox over SMTP, polls that mailbox over IMAP from a cron
routine, authenticates each reply (single-use token, sender allowlist, DKIM/SPF/DMARC) and
submits the answer through `orbit.decision.submit`. Core applies it and keeps the authority;
the plugin is only a transport.

The design is Orbit's
[`docs/design/human-decision-channel/2_design.md`](https://github.com/constellation-works/orbit/blob/agent-main/docs/design/human-decision-channel/2_design.md);
§10 lists the slices. Operators follow [docs/runbook.md](docs/runbook.md) from the mailbox
through registration, key rotation, revocation and outage recovery.

## Layout

- `.orbit-plugin/` is the installable plugin and the only copy of the code: `plugin.yaml`,
  the `bin/decision-mail` launcher, `schemas/`, `src/decision_mail/` and the conformance
  goldens in `tests/conformance/`.
- `.orbit-plugin/definitions/` holds the notify and poll activities, their jobs
  (`decision_request_pipeline`, `decision_mail_poll_pipeline`) and the two routines, seeded
  disabled.
- `tests/` holds the pytest suite, with fakes for SMTP, IMAP and Orbit core in
  `tests/fakes.py`; the root `pyproject.toml` carries dev tools only.

The runtime is the Python standard library. The launcher uses `uv` to find (or install into
plugin state) Python 3.12 or newer and runs the package from the plugin root.

## Develop

```bash
make check                       # uv lock --check, ruff, basedpyright strict, pytest
out=$(make -s export)            # fresh export of HEAD's plugin tree
orbit plugin validate "$out"
orbit plugin test "$out" --grant fs,network
```

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `decision-mail.status` | read-only | Version, `ready`/`not_configured`, missing config keys and secrets (names only), the configured channel and key epoch, and recent activity from plugin state (last notify, last poll, last error, recent drops, cumulative counters and the newest 20 events). Contacts no server. |
| `decision-mail.activity` | read-only | The same counters and newest 20 events, and nothing else. This is what the dashboard panel "Drops and refusals" renders. Contacts no server and writes nothing. |
| `decision-mail.notify` | mutating | Lists open decision requests (`orbit.decision.list`) and mails each one not mailed yet: subject `[orbit] decision needed: <title> (<task>) [<request>]`, `Reply-To: <local>+<token>@<domain>`, `orbit-token:`/`orbit-request:` lines, the numbered answers and the dashboard link. Input `request_ids` limits it to some requests, `resend: true` mails them again. A send failure fails the call (retryable). |
| `decision-mail.poll` | mutating | Reads unseen mail (oldest first, at most `max_messages`), authenticates it and submits each answer to `orbit.decision.submit`, then sends a confirmation or a refusal and files the mail. |
| `decision-mail.digest` | mutating | Sends the daily digest: one mail listing proposed tasks (title, summary, pilot state), then requests held for the digest, then requests expiring within 24 hours, each numbered with `orbit-item:` transport lines carrying its request id and token. Core's `orbit.decision.digest` raises the missing `proposed_approval` and `review_approval` requests first, so their expiry counts from this send. Batched confirmations of earlier digest answers ride in the same mail. Nothing to list and nothing owed sends no mail. At most 50 items; the rest are counted as `omitted`. |
| `decision-mail.parse-reply` | read-only | Applies the reply grammar to `text` against the printed `answers`; reads and sends nothing. |

No tool takes or returns a password, the channel key or a token. Every response and log
line is scrubbed of the delivered secret values as a last line of defence.

Counters accumulate for the life of the state file: drops by reason, core refusals by code,
SMTP and IMAP transport errors, and loop-guard suppressions. The newest 20 events sit beside
them. An event keeps the sender address, the reason, the request id and the time. It does not
keep a body, a token, a secret or a header. A 21st event evicts the oldest. `status` and
`activity` both report the figures; the runbook says where an operator looks.

### Batching, quiet hours and the digest

Both settings are plugin config under `[plugins.decision-mail]` (set them with `orbit config
set plugins.decision-mail.<key>`), not core `[decision_channel]` settings. Core stays
transport-agnostic and raises requests the same way whatever these say.

- `digest_mode = immediate`: `notify` mails every request as it is raised. The digest still
  lists proposed tasks and expiring requests.
- `digest_mode = daily` (default): `notify` mails `block_triage` requests immediately, since
  a blocked task is stopped work, and holds every other gate for the digest.
- `digest_mode = off`: `notify` and `digest` send nothing; answers on the dashboard or CLI
  still work.
- `quiet_hours = HH:MM-HH:MM` (host-local, 24-hour, may wrap midnight, empty for none):
  while the window is open, `notify` and `digest` send nothing and fail with the retryable
  `quiet_hours` error naming the end of the window, so a state-trigger member is retried
  rather than settled without a mail. With `22:00-07:00`, a 23:00 send goes out at 07:00 and
  the expiry the mail prints counts from 07:00. Core fixes the real expiry when it raises the
  request, so the plugin renders the remaining time at send time and skips a request that
  expired while it waited.

The digest routine (`decision-mail-digest`, cron `0 8 * * *`) is seeded disabled. Core
routines reject a `dedupe` key, so `policy.overlap: forbid` is what stops a second digest
starting while one runs (the equivalent of `skip_if_open`).

A digest reply names items by number on its first unquoted line: `approve 1,3` approves
items 1 and 3 only, `reject 2` rejects item 2, and clauses combine (`approve 1,3; reject 2`).
Any answer word works (`clear 4`). A bare `approve`, `approve all`, an out-of-range number,
an item both approved and rejected, or more than 50 numbers is refused whole with a mail
re-listing the items; it consumes no token and submits nothing. A valid reply submits each
named item through `orbit.decision.submit` with that item's token; core re-checks each one.
Several digest answers landing in one poll produce one confirmation, listed in the next
digest rather than as a mail each. A refusal mail is limited to one per digest per hour and
the unparseable-reply loop guard applies as for single requests.

### How poll treats a reply

1. Dropped with no reply mail and no submission, left seen in the inbox and listed under
   `status` → `activity.recent_drops`: mail not addressed to the channel, a sender (From,
   and Return-Path when present) not on `allowed_senders`, a topmost `authserv_id` stamp
   without aligned `dkim=pass` and `spf=pass` or with `dmarc=fail` (a header below the
   provider's stamp is never read), no single well-formed token, or no single known
   request id. A forged sender never learns the mailbox is live.
2. Unparseable (not one number or answer word on the first unquoted line): a refusal mail
   re-listing the answers, no submission, the token stays valid; the mail is left seen and
   flagged. After three such mails for one request the loop guard sends no more.
3. Otherwise the answer, the lines after it as the note, the sender and the raw
   Authentication-Results header go to `orbit.decision.submit`. Core re-checks everything
   and applies the answer; poll mails a confirmation and moves the reply to
   `processed_folder`.
4. A core refusal (`token_consumed`, `request_expired`, `request_superseded`,
   `answer_not_allowed`, `channel_revoked`, `token_invalid`) gets the design §5 refusal
   mail, at most one per request per hour (`token_consumed` goes to `notify_to`), and the
   reply is filed processed. Mail is never deleted. A refusal that needs the operator
   (`decision_channel_disabled`, `channel_not_registered`, `caller_not_allowed`) or a
   callback that did not answer stops the poll and leaves the reply unseen for the next
   tick.

## Config

Set with `orbit config set plugins.decision-mail.<key> <value>`; `schemas/config.json`
documents each key.

| Key | Default | Meaning |
|---|---|---|
| `address` | required | The channel mailbox (user@domain); requests come from it and replies arrive at it. |
| `notify_to` | required | Where requests (and reused-token warnings) are mailed. |
| `allowed_senders` | required | Addresses whose replies are accepted. |
| `authserv_id` | required | The provider's Authentication-Results id, e.g. `mx.google.com`. |
| `smtp_host`, `imap_host` | required | Mail servers. |
| `smtp_port`, `smtp_security` | `465`, `ssl` | Or `587`, `starttls`. |
| `imap_port`, `imap_security` | `993`, `ssl` | Or `143`, `starttls`. |
| `smtp_username`, `imap_username` | `address` | Logins. |
| `imap_folder`, `processed_folder` | `INBOX`, `Orbit/Processed` | Where replies arrive and where handled ones go. |
| `channel` | `email` | The registered channel name. |
| `key_epoch` | `1` | The channel's key epoch from `orbit decision channel list`; tokens are minted under it. |
| `plus_addressing` | `true` | Put the token in `Reply-To` as `<local>+<token>@<domain>`; the body line carries it too. |
| `dashboard_url` | empty | Link closing each mail; `{task_id}` and `{request_id}` are filled in. |
| `max_messages` | `10` | Replies one poll handles. |
| `from_name` | `Orbit` | Display name on sent mail. |
| `digest_mode` | `daily` | `immediate`, `daily` or `off`; see "Batching, quiet hours and the digest". Plugin config, not core `[decision_channel]`. |
| `quiet_hours` | empty | Host-local `HH:MM-HH:MM` window (may wrap midnight) during which nothing is mailed; sends wait for its end. |

Secrets (`orbit plugin secret set decision-mail <name>`): `smtp_password`, `imap_password`
and `channel_key`. Registering the channel mirrors `channel_key` for you.

## Install (operator)

On dk-server-2:

1. Export and install a released commit:

   ```bash
   orbit plugin add /tmp/decision-mail-plugin-<sha>
   orbit plugin enable decision-mail --grant 'fs={{plugin_state}}' --grant network \
     --grant orbit_tools
   ```

2. Set the mailbox config keys above and the two passwords:

   ```bash
   orbit plugin secret set decision-mail smtp_password
   orbit plugin secret set decision-mail imap_password
   ```

3. Enable the channel (`[decision_channel] enabled = true` in the workspace config) and
   register it; registration mints the key, mirrors it into `channel_key` and starts epoch 1:

   ```bash
   orbit decision channel register email --human daniel --plugin decision-mail \
     --allow-sender <your-address>
   orbit decision channel list        # set key_epoch to the epoch shown
   ```

   After `orbit decision channel rotate-key email`, set `key_epoch` to the new epoch so new
   mails carry valid tokens.

4. The mailbox provider must stamp `Authentication-Results` with `authserv_id` on delivery;
   replies without that stamp are dropped.

5. Enable the seeded routines. `decision-mail-notify` is a `decision_requested` state
   trigger: first set its `trigger.state.owner_machine` (`orbit host list`) and
   `trigger.state.branch`. `decision-mail-poll` runs every five minutes. `decision-mail-digest` (daily at 08:00,
`overlap: forbid`) is seeded disabled; enable it when `digest_mode` is `daily` or
`immediate` and you want proposed tasks and expiring requests mailed.

6. Check `orbit tool run decision-mail.status` reads `ready`, then answer one request
   from your inbox and confirm the confirmation mail arrives.

`orbit plugin secret list decision-mail` shows which secrets are set.

## License

MIT; see [LICENSE.md](LICENSE.md).
