# decision-mail runbook

Operator steps for the email decision channel on dk-server-2. The plugin is a transport:
it mails requests and submits authenticated replies through `orbit.decision.submit`. Core
applies the answer. Nothing in this runbook asks you to paste a password, a channel key or
a reply token into a file, a command argument or a chat. Commands that take a secret prompt
for it and do not echo it.

Placeholders:

| Placeholder | Stands for |
|---|---|
| `<sha>` | The commit you are installing. |
| `<mailbox@example.org>` | The dedicated channel mailbox. |
| `<you@example.com>` | Your own address, the one you reply from. |
| `<mx.example.net>` | The authserv-id your provider stamps. |
| `<token>` | A reply token. It belongs in a mail, never in this runbook or in a shell. |

Walking these steps against the live mailbox is the install check. It is not something a
worker or a test does.

## 1. Mailbox that stamps Authentication-Results

Use a mailbox that is not your personal inbox. The provider must add an
`Authentication-Results` header on delivery whose authserv-id you will configure as
`authserv_id`. The plugin reads only the topmost stamp with that id and requires
`dkim=pass` and `spf=pass`, and it refuses `dmarc=fail`. A provider that does not stamp
`Authentication-Results` cannot be the channel mailbox: skipping the check would reduce
authentication to "knows a token from a mail that might have been forwarded".

Send one ordinary message to the mailbox and read the delivered headers (the provider's
webmail, or the message source). You want a stamp of this shape, with your provider's id
and your domain:

```text
Authentication-Results: <mx.example.net>;
  dkim=pass header.i=@example.com;
  spf=pass smtp.mailfrom=<you@example.com>;
  dmarc=pass header.from=example.com
```

Note the authserv-id (the token before the first semicolon). That string is `authserv_id`.
If the header is missing, pick a different provider or turn the stamp on before going on.
Do not copy a real header into a ticket; the shape above is the whole of what you need to
recognise.

The mailbox should accept plus-addressing (`<local>+<token>@<domain>`). The token is also
in the body as `orbit-token: <token>`, which is the fallback when a provider strips the tag.

## 2. Install and grants

From a checkout of the commit you are installing:

```bash
out=$(make -s export)          # prints /tmp/decision-mail-plugin-<sha>
orbit plugin add "$out"
orbit plugin enable decision-mail \
  --grant 'fs={{plugin_state}}' \
  --grant network \
  --grant orbit_tools
```

`fs={{plugin_state}}` is the plugin's own bookkeeping directory. `network` is SMTP and
IMAP. `orbit_tools` is the callback grant: `orbit.decision.list`, `orbit.decision.show`,
`orbit.decision.submit` and `orbit.task.show`. The plugin never moves a task itself.

Enable only on the host. `enable --scope workspace` does not record grants.

## 3. Secrets

Three secrets, each set by prompting. There is no command-line argument for the value.

```bash
orbit plugin secret set decision-mail smtp_password
orbit plugin secret set decision-mail imap_password
orbit plugin secret list decision-mail
```

`channel_key` is not set here. Registration (the next section) mints it and mirrors it
into the plugin. `secret list` prints names and whether each is set, never a value.

## 4. Config

```bash
orbit config set plugins.decision-mail.address <mailbox@example.org>
orbit config set plugins.decision-mail.notify_to <you@example.com>
orbit config set plugins.decision-mail.allowed_senders '["<you@example.com>"]'
orbit config set plugins.decision-mail.authserv_id <mx.example.net>
orbit config set plugins.decision-mail.smtp_host <smtp.example.net>
orbit config set plugins.decision-mail.imap_host <imap.example.net>
```

Defaults cover ports (`465` SMTP, `993` IMAP), `ssl`, the folder names, `channel` (`email`)
and `key_epoch` (`1`). Set `smtp_username` or `imap_username` only when the login is not
the mailbox address. Set `dashboard_url` if mails should link at a task; `{task_id}` and
`{request_id}` are filled in.

Turn the channel on in the workspace config (`[decision_channel] enabled = true`) before
expecting requests to be raised. With it off, core raises nothing and there is nothing to
mail.

## 5. Register the channel and the sender allowlist

Registration is operator CLI only. An agent session and a plugin callback are refused, so
a running task cannot mint itself a human.

```bash
orbit decision channel register email \
  --human daniel \
  --plugin decision-mail \
  --allow-sender <you@example.com>
orbit decision channel list
```

Repeat `--allow-sender` for each address that may reply. Registration generates the
channel key, stores it where core verifies tokens, mirrors it into the plugin secret
`channel_key`, and starts key epoch 1. `channel list` shows the epoch and a fingerprint.
It does not show the key. Copy the epoch into config:

```bash
orbit config set plugins.decision-mail.key_epoch 1
```

Use the epoch `channel list` printed. The plugin cannot read the epoch itself; a stale
`key_epoch` mints tokens core will reject.

The plugin's `allowed_senders` and the channel's `--allow-sender` list are both checked.
A reply whose `From` (and `Return-Path`, when the message has one) is not on the plugin
list is dropped with no reply mail. A sender the plugin accepted but the channel did not
is refused by core as `sender_not_allowed` and also gets no reply mail.

## 6. Enable the routines

`orbit plugin enable` seeds two routines, switched off, as
`.orbit/routines/decision-mail-notify.yaml` and
`.orbit/routines/decision-mail-poll.yaml`.

`decision-mail-notify` fires when a decision request is raised. Before turning it on, set
`trigger.state.owner_machine` to this host (`orbit host list`) and `trigger.state.branch`
to the workspace's base branch. The seeded file says `set-owner-machine-before-enabling`
and `main` until you change them.

`decision-mail-poll` runs every five minutes and reads the mailbox.

In each file set `enabled: true`, then confirm the clock will see them:

```bash
orbit routine list
orbit routine show decision-mail-notify
orbit routine show decision-mail-poll
orbit clock status
```

`enabled: false` is how you turn one off again. `orbit routine pause <name>` holds it on
this machine only; `orbit routine resume <name>` clears that pause. The clock has to be
running or neither routine fires.

Check the plugin before relying on it:

```bash
orbit tool run decision-mail.status
```

`transport` should be `ready` and `missing` empty. Then answer one request from the inbox
and confirm the confirmation mail arrives. That live reply is the install check.

## 7. What you can see without the logs

`decision-mail.status` and `decision-mail.activity` report the same counters and the same
newest 20 events. The dashboard panel **Drops and refusals** (Plugins tab, on the
decision-mail card) is `activity` rendered as JSON. `status` also shows readiness, the
last notify, the last poll and the last error.

```bash
orbit tool run decision-mail.activity
```

Counters accumulate until the state file is replaced. They cover:

| Counter | When it moves |
|---|---|
| `drops.<reason>` | The plugin dropped a message before core: sender not allowlisted, envelope sender not allowlisted, authentication failed, malformed or missing token, unknown request, not addressed to the channel, unreadable, or too large. |
| `refusals.<code>` | Core refused a submission. Silent codes (`sender_not_allowed`, `authentication_failed`, `request_not_found`) get no reply mail. The others get the design's refusal mail, except a code that needs you (`decision_channel_disabled`, `channel_not_registered`, `caller_not_allowed`), which stops the poll and leaves the message unseen. |
| `transport.smtp` / `transport.imap` | A send failed, or the poll could not log in or read the mailbox. |
| `loop_guard` | A fourth unparseable reply for one request was not answered. The first three still get a re-listing. |

An event is `at`, `kind`, `reason`, `sender` and `request_id`. The newest 20 are kept;
the oldest is dropped first. The 21st does not erase a counter. Events never include the
body, the token, a secret or the `Authentication-Results` header. `activity` writes
nothing.

## 8. Rotate the key

Do this on any suspicion the key leaked, and at least yearly.

```bash
orbit decision channel rotate-key email
orbit decision channel list
```

Rotation generates a new key, mirrors it into `channel_key`, and bumps the epoch. Every
token minted under the old epoch stops verifying, including tokens in mail that is already
in an inbox. `channel list` shows the new epoch and a new fingerprint, not the key.

Then point the plugin at that epoch:

```bash
orbit config set plugins.decision-mail.key_epoch <epoch from channel list>
```

Open requests stay open. They are not re-mailed by themselves: notify records a request
it has already sent and will skip it. Replies that still carry the old token are refused
`token_consumed`. That refusal goes to `notify_to` (not to whoever sent the replay), at
most one mail per request per hour, and the original answer stands if one had already
landed. Nothing is applied for the old token.

Re-mail the open requests under the new epoch when you want them answerable from the
inbox again:

```bash
orbit tool run decision-mail.notify --input '{"resend": true}'
```

To re-mail one request, pass its id. The id is `dr-` plus 16 hex digits, from
`orbit decision list` or the original subject. Do not pass the token.

```bash
orbit tool run decision-mail.notify --input '{"request_ids": ["dr-<16 hex digits>"], "resend": true}'
```

Until that resend, the dashboard is how those requests get answered. A request that
expires while you wait is refused `request_expired` if someone replies late; core raises
a fresh one on the next hold, and notify mails that new id on its own.

## 9. Revoke the channel

```bash
orbit decision channel revoke email
```

Revocation is immediate. Outstanding tokens stop verifying and every submission is refused
`channel_revoked`. No later reply applies. The refusal mail tells the sender the channel
is revoked.

Stop the routines so the plugin stops sending and polling. Set `enabled: false` in
`.orbit/routines/decision-mail-notify.yaml` and `.orbit/routines/decision-mail-poll.yaml`,
or pause them:

```bash
orbit routine pause decision-mail-notify
orbit routine pause decision-mail-poll
```

Open requests remain on the dashboard and can still be answered there. Mail will not
apply them. The counters keep whatever `channel_revoked` refusals arrived before you
paused the poll. Revocation does not delete the mailbox or the plugin secrets; rotate or
remove those separately if the mailbox itself is suspect (`orbit plugin secret rm
decision-mail channel_key` after you no longer need the mirror).

## 10. SMTP or IMAP is down

Neither outage applies a decision wrongly. A request is never marked answered because a
send failed, and a message is never dropped because the mailbox could not be read.

**SMTP.** `decision-mail.notify` returns `smtp_failed` and is retryable. The request stays
open and un-mailed, so the routine's next attempt sends it. The status panel shows the
error, `transport.smtp` increments, and an event with the request id is appended. Mail
that did get a reply applied is already applied: if the confirmation or the refusal mail
then fails to send, the reply is still filed, `transport.smtp` increments, and the human
may not see the confirmation. Check the request on the dashboard; there is no second
confirmation tool. The counter is the record that the notice did not go out.

**IMAP.** `decision-mail.poll` returns `imap_failed` and is retryable. Messages stay
unseen in the inbox and are handled oldest-first on the next successful poll, with the
same token and binding checks as if the poll had not been late. A long outage means
decisions apply late, or the request expires and a late reply is refused
`request_expired`. It does not mean a stale reply applies against a hold that has moved.
`transport.imap` increments once per failed poll. When the mailbox is back, one successful
`orbit tool run decision-mail.poll` (or simply waiting for the five-minute routine) drains
the unseen mail. Watch `activity` afterwards: a burst of drops is a stamp or allowlist
problem, not the outage itself.
