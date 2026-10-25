# Agent guidance

- This repository is `orbit-decision-mail`: the email transport plugin (`decision-mail`) for
  Orbit's human decision channel. Python, standard library only at runtime; `uv` for dev
  tooling. The spec is Orbit's `docs/design/human-decision-channel/2_design.md`; where this
  repo and the design differ, the design wins until it is amended.
- The plugin is a transport, not an authority. It never applies a decision itself: every
  answer goes through `orbit.decision.submit`, and core re-checks the token and the
  authentication verdicts. Never add a path that moves a task directly.
- Credentials never cross the tool boundary. No tool accepts or returns a password, key or
  token; mailbox credentials and the channel key arrive only as declared plugin secrets in
  `context.secrets`. Never log a secret value or put one in an error `detail`.
- Mail is untrusted data. Message bodies and headers are parsed, never executed or
  followed, and only the closed answer grammar of design §6 is accepted.
- Tests never touch the network or a real mailbox: use fakes for SMTP and IMAP. Live mailbox
  checks belong to the operator on dk-server-2.
- `.orbit-plugin/` is the installable plugin and the only copy of the code. A tool change
  updates its schema, its goldens and `tests/test_plugin_tree.py` together. No symlinks in
  the plugin tree. Keep `dependencies = []`; a runtime dependency needs a design note first.
- `agent-main` is the base branch and changes land on it directly. Daniel owns releases,
  `orbit plugin add/upgrade/enable` with grants, the mailbox and its secrets, and channel
  registration.
- Run `make check` before handing off.

## Tools, config and operator steps

README.md has the full tables; keep them and this summary in step with the manifest.

- Tools: `status` (read-only: readiness, missing keys/secrets by name, activity),
  `notify` (mutating: mails each open request once), `poll` (mutating: authenticates
  replies, submits through `orbit.decision.submit`, mails a confirmation or refusal, files
  the mail), `parse-reply` (read-only: the reply grammar on a given text).
- Callbacks (`permissions.orbit_tools`): `orbit.decision.list`, `orbit.decision.show`,
  `orbit.decision.submit`, `orbit.task.show`. They run as `orbit tool run … --input-file`
  with the inherited fd-3 callback session and never with `--model`/`--agent`.
- Required config keys: `address`, `notify_to`, `allowed_senders`, `authserv_id`,
  `smtp_host`, `imap_host`. Defaults cover ports/security, usernames, folders, `channel`
  (`email`), `key_epoch` (`1`), `plus_addressing`, `dashboard_url`, `max_messages` and
  `from_name`. Secrets: `smtp_password`, `imap_password`, `channel_key`.
- The plugin cannot read the channel's key epoch, so `key_epoch` is config the operator
  copies from `orbit decision channel list` after `register` and every `rotate-key`.
- Plugin state (`$ORBIT_PLUGIN_STATE/mail/state.json`, under an flock) records what was
  mailed, refusal-mail times (one per request per hour), the unparseable-reply loop guard
  and recent drops. It never holds a secret or a token.
- Operator steps (Daniel): `orbit plugin add` a fresh export; `orbit plugin enable
  decision-mail --grant 'fs={{plugin_state}}' --grant network --grant orbit_tools`; `orbit
  plugin secret set decision-mail smtp_password|imap_password`; set the config keys with
  `orbit config set plugins.decision-mail.<key>`; `[decision_channel] enabled = true`;
  `orbit decision channel register email --human daniel --plugin decision-mail
  --allow-sender <address>` (mirrors `channel_key`); set `key_epoch`; set the
  `decision-mail-notify` routine's `owner_machine` and `branch`; enable both routines.

<!-- constellation-standards:begin -->
<!-- Managed by the constellation's operations/scripts/sync-standards.sh; edits inside this block are overwritten. -->
## Constellation standards

This repository adopts these constellation standards, vendored read-only in `docs/standards/` (each a directory: `STD-nn.md` holds the rules, `why.md` and `checks.md` the reasons and gates):

- `STD-03@2` — [docs/standards/STD-03-concurrency-and-process-safety/STD-03.md](docs/standards/STD-03-concurrency-and-process-safety/STD-03.md)
- `STD-04@2` — [docs/standards/STD-04-testing-and-verification/STD-04.md](docs/standards/STD-04-testing-and-verification/STD-04.md)
- `STD-05@1` — [docs/standards/STD-05-security-boundaries/STD-05.md](docs/standards/STD-05-security-boundaries/STD-05.md)

Follow them; they are normative. To deviate from a rule, record a decision in `docs/design/<feature>/4_decisions.md` citing `STD-nn@<version> §Rn`; never edit `docs/standards/` (`sh docs/standards/check.sh` enforces this).
Reviewers check every change against the adopted standards and report violations as `STD-nn §Rn` with file:line evidence.
<!-- constellation-standards:end -->
