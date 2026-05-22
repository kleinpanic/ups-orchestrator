# Contributing

Thanks for your interest! This is a small, dependency-free tool — the bar is
"clean, typed, tested."

## Dev setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Before you push — the gate (CI runs the same)

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy            # strict
.venv/bin/pytest -q
```

All three must pass. CI runs them on Python 3.11–3.13.

## Conventions

- **Zero runtime dependencies.** The package uses only the standard library
  (`urllib` for Discord, the `upsc` CLI for UPS data). Dev tools live in the
  `dev` extra. Don't add runtime deps without a strong reason.
- **Typed.** `mypy --strict` over `src/`. New code must type-clean.
- **Tested.** Side effects (UPS reads, shutdowns, clock, network) are injected
  via `events.Deps`, so handlers test without real hardware — follow that pattern.
- **Notifications stay NUT-event-driven**; the poll loop is for shutdown
  decisions. Keep those concerns separate.
- **Never commit secrets or host specifics.** The webhook comes from an env
  file; `config.json` and `*.env` are gitignored. Only `config.example.json`
  (generic placeholders) is tracked.
- Conventional-ish commit subjects (`feat:`, `fix:`, `docs:`, `refactor:`).

## Releases

Maintainers tag `vX.Y.Z` (matching `pyproject.toml`); the release workflow
builds the wheel/sdist and publishes a GitHub Release. Update
[`CHANGELOG.md`](CHANGELOG.md) in the same change.
