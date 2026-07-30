PROJECT := ups-orchestrator
BASE    := $(CURDIR)
PY      := $(BASE)/.venv/bin/python3
ORCH    := $(BASE)/.venv/bin/ups-orchestrator

NUT_DIR        := /etc/nut
SYSTEMD_DIR    := /etc/systemd/system
ENV_FILE       := /etc/ups-orchestrator.env
STATE_DIR      := /var/lib/$(PROJECT)

ACTION ?= beeper-mute

CONFIG ?=

.PHONY: help venv lint type test coverage check deploy deploy-code deploy-user-service nut-snippets nut-control-user control nut-repair-listen install-config nut-status

help:
	@echo "Targets:"
	@echo "  venv                 Create .venv and install package + dev deps"
	@echo "  lint type test       Run ruff / mypy / pytest"
	@echo "  coverage             Run pytest with branch coverage + term-missing"
	@echo "  mutation             Targeted mutation test of critical logic"
	@echo "  check                All three"
	@echo "  deploy               Full system install (needs root): sudo make deploy"
	@echo "  deploy-code          Redeploy just the code into /opt (NO sudo once owned)"
	@echo "  deploy-user-service  Enable the --user poll-loop service (NO sudo)"
	@echo "  nut-control-user     Create least-priv NUT control user (root): sudo make nut-control-user"
	@echo "  control ACTION=x     Run a control action on all UPSes (e.g. ACTION=beeper-mute)"
	@echo "  nut-snippets         Print the NUT config changes to apply by hand"
	@echo "  nut-status           Show NUT/orchestrator unit health + reachability (NO sudo)"
	@echo "  nut-repair-listen    Restore upsd.conf's loopback LISTEN + restart nut-server (root)"
	@echo "  install-config CONFIG=x  Validate then install a config.json into /etc (root)"

venv:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

# CI runs BOTH `ruff check` and `ruff format --check`. This target ran only the
# first, so a formatting-only regression passed `make check` locally and failed in
# CI — the gap the integration audit hit. Keep the two in step.
lint:
	$(BASE)/.venv/bin/ruff check .
	$(BASE)/.venv/bin/ruff format --check .

type:
	$(BASE)/.venv/bin/mypy

test:
	$(BASE)/.venv/bin/pytest -q

coverage:
	$(BASE)/.venv/bin/pytest -q --cov --cov-report=term-missing

# Targeted mutation test — patches critical logic and expects the suite to catch it.
mutation:
	$(PY) tools/mutation_test.py

check: lint type test

# System install (root): venv + /etc config+env + /var/lib state + dispatcher +
# ACLs + control user, and hands /opt ownership to the installing user.
deploy:
	@deploy/install.sh

# Redeploy just the code into the existing /opt venv. No sudo once `deploy` has
# handed /opt to you; falls back to a clear message if you don't own it yet.
deploy-code:
	@/opt/ups-orchestrator/venv/bin/pip install -q --force-reinstall --no-deps "$(BASE)" \
	  && echo "redeployed $(PROJECT) into /opt" \
	  || echo "cannot write /opt venv — run 'sudo make deploy' once to take ownership"

# Least-privilege NUT control user + env creds (root): sudo make nut-control-user
nut-control-user:
	@deploy/nut-control-user.sh

# Run a control action across every configured UPS (needs admin creds in env).
control:
	@$(ORCH) control $(ACTION)

# Poll-loop as a systemd --user service (NO sudo).
deploy-user-service:
	@deploy/install-user-service.sh

# Restore the loopback LISTEN upsd needs to survive a boot before DHCP, then
# restart nut-server and prove a bare `upsc` works: sudo make nut-repair-listen
nut-repair-listen:
	@deploy/repair-upsd-listen.sh

# Read-only: which NUT driver is burning CPU, sampled over 5s of wall clock.
# %CPU from ps is a LIFETIME average and hid this for two days -- a driver that
# spun up recently reads low, and one that has since calmed down reads high.
nut-driver-cpu:
	@echo "sampling usbhid-ups CPU over 5s (jiffies/100 = seconds of CPU)..."
	@for p in $$(pgrep -x usbhid-ups); do \
	  a=$$(awk '{print $$14+$$15}' /proc/$$p/stat 2>/dev/null); echo "$$p $$a"; \
	 done > /tmp/.nutcpu.$$$$; sleep 5; \
	 while read p a; do \
	   b=$$(awk '{print $$14+$$15}' /proc/$$p/stat 2>/dev/null); \
	   [ -n "$$b" ] || continue; \
	   name=$$(tr '\0' ' ' < /proc/$$p/cmdline 2>/dev/null | awk '{print $$NF}'); \
	   printf '  %-14s pid %-8s %s%% of one core\n' "$$name" "$$p" "$$(( (b-a) * 100 / 500 ))"; \
	 done < /tmp/.nutcpu.$$$$; rm -f /tmp/.nutcpu.$$$$
	@echo "a healthy idle driver is ~0%. If one is pegged, see: make nut-repair-driver-spin UPS=<name>"

# Root: add NUT's `pollonly` to one misbehaving driver and restart just it.
nut-repair-driver-spin:
	@deploy/repair-driver-spin.sh $(UPS)

# Validate a config.json, then install it into /etc with the canonical
# 0640 root:nut + u:$(USER):r ACL: sudo make install-config CONFIG=/path/to.json
install-config:
	@test -n "$(CONFIG)" || { echo "usage: sudo make install-config CONFIG=/path/to/config.json" >&2; exit 1; }
	@deploy/install-config.sh "$(CONFIG)"

# Read-only health check — what is running, and can anything actually be read.
nut-status:
	@echo "--- units ---"
	@for u in nut-server nut-monitor nut-driver.target; do \
	  printf '  %-34s %s\n' "$$u" "$$(systemctl is-active $$u 2>&1)"; done
	@for u in ups-orchestrator-recorder ups-orchestrator-watch; do \
	  printf '  %-34s %s\n' "$$u (--user)" "$$(systemctl --user is-active $$u 2>&1)"; done
	@echo "--- active LISTEN lines in $(NUT_DIR)/upsd.conf ---"
	@grep -E '^\s*LISTEN' $(NUT_DIR)/upsd.conf 2>/dev/null || echo "(none readable)"
	@echo "--- upsc reachability ---"
	@upsc -l >/dev/null 2>&1 && echo "localhost: OK" || echo "localhost: REFUSED (run: sudo make nut-repair-listen)"
	@echo "--- latest recorder sample ---"
	@tail -n1 $(STATE_DIR)/samples.jsonl 2>/dev/null | $(PY) -c 'import json,sys;r=json.load(sys.stdin);print(r.get("time"), {k:v.get("status") for k,v in r.get("upses",{}).items()})' 2>/dev/null || echo "(no readable samples)"

nut-snippets:
	@echo "Apply these to $(NUT_DIR) (back up first), then: systemctl restart nut-driver-enumerator nut-server nut-monitor"
	@echo "  - ups.conf       : append deploy/nut/ups.conf.snippet (one section per UPS)"
	@echo "  - upssched.conf  : replace with deploy/nut/upssched.conf.snippet (fixes CMDSCRIPT path)"
	@echo "  - upsmon.conf    : merge deploy/nut/upsmon.conf.snippet (one MONITOR per UPS, NOTIFYFLAGs)"
	@echo ""
	@echo "Primary-side artifacts for NUT-secondary enrollment (operator reference / rollback;"
	@echo "the live path is 'ups-orchestrator monitor add'):"
	@echo "  - upsd.conf      : merge deploy/nut/upsd.conf.snippet (LAN LISTEN — restart nut-server, NOT reload)"
	@echo "  - upsd.users     : merge deploy/nut/upsd.users.snippet ([upsmon_secondary]; set real pw from UPS_NUT_SECONDARY_PASSWORD)"
	@echo "  - crowdsec guard : deploy/nftables/crowdsec-partof.conf via 'systemctl edit crowdsec-firewall-bouncer' (PartOf=nftables.service)"
	@echo "  - nut-monitor    : deploy/nut/nut-monitor-network-online.conf drop-in on secondaries (After/Wants=network-online.target)"
