# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/kleinpanic/ups-orchestrator/security/advisories/new)
rather than a public issue. You'll get a response as soon as practical.

## Secret & credential handling

- The Discord webhook is **never** stored in the repo. It is read at runtime
  from an environment variable (default `UPS_DISCORD_WEBHOOK`), supplied via
  `/etc/ups-orchestrator.env` (`root:nut 0640`, plus an ACL for the run user).
- `config.json`, `config.local.json`, `*.env`, and `state.json` are gitignored;
  only `config.example.json` (generic placeholders) is tracked.
- A `gitleaks` pre-commit/pre-push hook and CI guard against accidental leaks.

## Deployment notes that affect your attack surface

- **`local` shutdown targets** rely on a `sudoers.d` rule granting the run user
  passwordless `shutdown`/`poweroff` only. Review `deploy/install.sh`.
- **`serial` shutdown targets** assume a passwordless/auto-login getty on the
  target's serial console. Scope that auto-login to the **serial tty only** —
  it must not weaken SSH or the physical console. (SSH should remain key-only.)
- **`remote` (SSH) targets** use `BatchMode` key auth; prefer an `ssh_config`
  alias so host/port/key stay in `~/.ssh/config`, and a dedicated key with only
  the privileges needed to shut the box down.
