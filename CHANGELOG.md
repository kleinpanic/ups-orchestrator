# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Load-step drop detection** (`load_step` config block, on by default): a
  device abruptly losing power shows up as its UPS's output load collapsing
  while the UPS itself stays `OL` — NUT's only in-band signature for a
  downstream device dying. A drop of `drop_percent` points (default 15) below
  the peak of the last `window_polls` polls (default 4 — the window keeps a
  collapse that straddles a poll boundary from splitting into two sub-threshold
  steps) logs a `load_step_drop` event with the estimated watts delta and sends
  a rate-limited notification (`cooldown_seconds`, default 600). The alert
  embeds a 10-minute draw-history sparkline built from the recorder samples.
  Motivated by a real incident: a host's hard power-offs were visible in the
  recorder as −135 W / −216 W load steps on clean input voltage, but nothing
  alerted on them.
- **`shutdown_scope`** (global default + per-UPS override): choose whether a UPS
  shuts down only its remote/serial targets (`remote`, the default) or the local
  host too (`all`, fired last at its own threshold). Lets you configure "just the
  remote" vs "both" per UPS.

- **`power-dashboard`** command: renders a PNG — a card per UPS (status, battery,
  load, runtime) plus a draw-history line chart from the recorder samples — and
  posts it to Discord as an image attachment (`--post`, `--out`, `--hours`).
  Needs the optional `matplotlib` dep (`pip install ups-orchestrator[dashboard]`).
  The daily `report` posts it automatically once a week (Mondays), so it rides
  the existing report timer with no separate systemd unit; `report --dashboard`
  forces it any day.

### Changed
- NUT `upsmon.conf` snippet and Deployment docs now spell out the full `MONITOR`
  contract — connect-host (`@localhost` only on the `upsd` host), `powervalue`
  vs `MINSUPPLIES`, `primary`/`secondary`, and the credential source — instead
  of the earlier one-line sketch.
- Recorder retention deepened from 10 to 20 rotations (`DEFAULT_MAX_ROTATIONS`
  and the `recorder.service` `--max-rotations`), roughly two weeks of one-second
  forensic history at the live three-UPS size. Worst case ≈ 1 GB
  (20 × 50 MB) for the samples log; confirm disk headroom before redeploying.

## [0.4.0] — 2026-05-21

### Added
- **`serial` shutdown target**: shut a UPS-powered machine down over a serial
  console (`device` + `baud`) into a passwordless/auto-login getty.
  Network-independent, so it works during an outage when SSH can't reach the box.
- SSH remote targets accept an `ssh_config` Host alias (omit `user` → `ssh <alias>`).
- Release-on-tag workflow and a `docs/` → GitHub wiki sync.

## [0.3.0] — 2026-05-21

### Added
- `status` command: per-UPS terminal table (alive/dead, battery gauge, runtime,
  load, input) with a live `--watch` refresh.
- Unified `shutdown_targets`: each `local` or `remote`, fired by a battery
  charge-% **or** runtime-seconds threshold; `local` targets always run
  after every enabled remote (the watcher host dies last).
- `watch` poll loop as a `systemd --user` service, polling every `poll_seconds`;
  configurable on-battery countdown (`countdown_every_seconds`, 0 = off).

### Removed
- Dead `shutdown_pi_on_lowbatt` / `min_runtime_seconds_shutdown_pi` config.
- The pre-0.3 `tick` systemd timer (replaced by the `watch` service).

## [0.2.0] — 2026-05-21

### Changed
- Rewrote the single-file NUT power-event script into a typed, tested package
  (`ups_orchestrator`): zero runtime deps, Discord embeds, multi-UPS,
  ruff + mypy(strict) + pytest, GitHub Actions CI, system deployment scripts.

[Unreleased]: https://github.com/kleinpanic/ups-orchestrator/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kleinpanic/ups-orchestrator/releases/tag/v0.4.0
[0.3.0]: https://github.com/kleinpanic/ups-orchestrator/releases/tag/v0.3.0
[0.2.0]: https://github.com/kleinpanic/ups-orchestrator/releases/tag/v0.2.0
