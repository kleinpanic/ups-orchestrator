# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-05-21

### Added
- **`serial` shutdown target** — shut a UPS-powered machine down over a serial
  console (`device` + `baud`) into a passwordless/auto-login getty.
  Network-independent, so it works during an outage when SSH can't reach the box.
- SSH remote targets accept an `ssh_config` Host alias (omit `user` → `ssh <alias>`).
- Release-on-tag workflow and a `docs/` → GitHub wiki sync.

## [0.3.0] — 2026-05-21

### Added
- `status` command: per-UPS terminal table (alive/dead, battery gauge, runtime,
  load, input) with a live `--watch` refresh.
- Unified `shutdown_targets`: each `local` or `remote`, firing on a battery
  charge-% **or** runtime-seconds threshold; `local` targets always sequenced
  after every enabled remote (the watcher host dies last).
- `watch` poll loop as a `systemd --user` service, polling every `poll_seconds`;
  configurable on-battery countdown (`countdown_every_seconds`, 0 = off).

### Removed
- Dead `shutdown_pi_on_lowbatt` / `min_runtime_seconds_shutdown_pi` config.
- The pre-0.3 `tick` systemd timer (replaced by the `watch` service).

## [0.2.0] — 2026-05-21

### Changed
- Rewrote the single-file NUT power-event script into a typed, tested package
  (`ups_orchestrator`): zero runtime deps, beautiful Discord embeds, multi-UPS,
  ruff + mypy(strict) + pytest, GitHub Actions CI, system deployment scripts.

[Unreleased]: https://github.com/kleinpanic/ups-orchestrator/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kleinpanic/ups-orchestrator/releases/tag/v0.4.0
[0.3.0]: https://github.com/kleinpanic/ups-orchestrator/releases/tag/v0.3.0
[0.2.0]: https://github.com/kleinpanic/ups-orchestrator/releases/tag/v0.2.0
