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

**Status: scaffold.** Only the `status` tool exists. Mail sending, polling, the digest and
hardening are decision channel slices 5–7.

The design is Orbit's
[`docs/design/human-decision-channel/2_design.md`](https://github.com/constellation-works/orbit/blob/agent-main/docs/design/human-decision-channel/2_design.md);
§10 lists the slices.

## Layout

- `.orbit-plugin/` is the installable plugin and the only copy of the code: `plugin.yaml`,
  the `bin/decision-mail` launcher, `schemas/`, `src/decision_mail/` and the conformance
  goldens in `tests/conformance/`.
- `tests/` holds the pytest suite; the root `pyproject.toml` carries dev tools only.

The runtime is the Python standard library. The launcher uses `uv` to find (or install into
plugin state) Python 3.12 or newer and runs the package from the plugin root.

## Develop

```bash
make check                       # uv lock --check, ruff, basedpyright strict, pytest
out=$(make -s export)            # fresh export of HEAD's plugin tree
orbit plugin validate "$out"
orbit plugin test "$out" --grant fs,network
```

## Install (operator)

On dk-server-2, from a fresh export:

```bash
orbit plugin add /tmp/decision-mail-plugin-<sha>
orbit plugin enable decision-mail --grant 'fs={{plugin_state}}' --grant network
```

The mailbox credentials are plugin secrets the operator sets with
`orbit plugin secret set decision-mail <name>`; `orbit plugin secret list decision-mail` shows
which are set.

## License

MIT; see [LICENSE.md](LICENSE.md).
