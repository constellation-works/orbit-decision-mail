# Agent guidance

`orbit-decision-mail` is the email transport plugin (`decision-mail`) for Orbit's human decision channel: it mails decision requests and turns authenticated replies into decisions. The spec is Orbit's human-decision-channel design, linked from README.md; where this repo and that design differ, the design wins until it is amended. `CLAUDE.md` is a symlink to this file.

## Rules

- The plugin is a transport, not an authority: every answer goes through `orbit.decision.submit`, which re-checks the token and the authentication verdicts.
- Never add a path that moves a task directly.
- Credentials never cross the tool boundary (README.md § Tools). Mailbox credentials and the channel key arrive only as declared plugin secrets in `context.secrets`.
- Never log a secret value or put one in an error `detail`; STD-05 §R13 and §R14 govern redaction.
- Mail is untrusted data: message bodies and headers are parsed, never executed or followed, and only the closed answer grammar of design §6 is accepted.
- Plugin state never holds a secret, a token, a message body or a header dump.
- Daniel owns releases, `orbit plugin add/upgrade/enable` with grants, the mailbox and its secrets, and channel registration.

## Branching

- `agent-main` is the base branch, and changes land on it directly.

## Gates

- Run `make check` before handing off.

## Code

- `.orbit-plugin/` is the installable plugin and the only copy of the code. A tool change updates its schema, its goldens and `tests/test_plugin_tree.py` together.
- No symlinks in the plugin tree.
- Keep `dependencies = []` in `pyproject.toml`; a runtime dependency needs a design note first.
- A change to plugin state holds its lock (STD-03 §R6) and keeps every older state format readable (STD-03 §R24).

## Tests

- Tests are hermetic (STD-04 §R7): SMTP and IMAP are the fakes in `tests/fakes.py`, and no test touches the network or a real mailbox.
- Live mailbox checks are the operator's install check on dk-server-2, described in `docs/runbook.md`; no worker or test runs one.

## Tools, config and operator steps

- `README.md` holds the full tool, config and batching tables. Keep them in step with `.orbit-plugin/plugin.yaml` and its schemas.
- Tools and their kinds are in README.md § Tools. Config keys, defaults and secrets are in README.md § Config, and `.orbit-plugin/schemas/config.json` documents each key.
- Reply handling, batching, quiet hours and the digest are in README.md § How poll treats a reply and § Batching, quiet hours and the digest.
- Callbacks run through the `orbit_tools` grant listed in `.orbit-plugin/plugin.yaml`, as `orbit tool run … --input-file` on the inherited fd-3 session, and never with `--model` or `--agent`.
- The plugin cannot read the channel's key epoch. `key_epoch` is operator config, copied from `orbit decision channel list` after `register` and after each `rotate-key`.
- Operator steps (mailbox stamp, install and grants, secrets, registration, routines, key rotation, revocation, outage recovery) are in `docs/runbook.md`.

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
