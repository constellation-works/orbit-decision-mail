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
