# Deployment

The orchestrator gets installed to a system location because NUT's `nut` user
runs the event path and can't read a `0700` home directory. The repo can live
in your home for development; the deployed copy lives under `/opt`, `/etc`, and
`/var/lib`.

## 1. System install (root)

```bash
sudo deploy/install.sh
sudo "$EDITOR" /etc/ups-orchestrator.env   # set your real webhook
```

This creates a venv at `/opt/ups-orchestrator/venv` (symlinked to
`/usr/local/bin/ups-orchestrator`), installs the config/env/state under `/etc`
and `/var/lib`, drops the `upssched-cmd.sh` dispatcher in `/usr/local/bin`, sets
ACLs so the run user can read the config and write state, and adds a `sudoers.d`
rule for local shutdowns.

## 2. Wire up NUT

Apply the snippets in `deploy/nut/` (review them first; set your UPS names and
USB ids):

- `ups.conf`: one section per UPS.
- `upssched.conf`: points `CMDSCRIPT` at `/usr/local/bin/upssched-cmd.sh` and
  maps the events.
- `upsmon.conf`: a `MONITOR` line per UPS plus the `NOTIFYFLAG`/`NOTIFYCMD`
  wiring.

```bash
sudo systemctl restart nut-driver-enumerator nut-server nut-monitor
upsc -l   # your UPSes should be listed
```

## 3. The poll loop (no sudo)

```bash
loginctl enable-linger "$USER"
deploy/install-user-service.sh
```

This installs `ups-orchestrator-watch.service` as a `systemd --user` service so
it runs whether or not you're logged in.

## Where things land

| Path | What |
|------|------|
| `/usr/local/bin/ups-orchestrator` | the orchestrator |
| `/usr/local/bin/upssched-cmd.sh` | NUT dispatcher |
| `/etc/ups-orchestrator/config.json` | per-UPS config (no secret) |
| `/etc/ups-orchestrator.env` | webhook + paths |
| `/var/lib/ups-orchestrator/state.json` | per-UPS state |
| `~/.config/systemd/user/ups-orchestrator-watch.service` | the poll loop |

Your real device ids, IPs, and the webhook stay on the machine under `/etc`;
none of that is in the repo.
