PROJECT := ups-orchestrator
BASE    := $(CURDIR)
PY      := $(BASE)/.venv/bin/python3
ORCH    := $(BASE)/.venv/bin/ups-orchestrator

NUT_DIR        := /etc/nut
SYSTEMD_DIR    := /etc/systemd/system
ENV_FILE       := /etc/ups-orchestrator.env
STATE_DIR      := /var/lib/$(PROJECT)

.PHONY: help venv lint type test check deploy deploy-user-timer nut-snippets

help:
	@echo "Targets:"
	@echo "  venv               Create .venv and install package + dev deps"
	@echo "  lint type test     Run ruff / mypy / pytest"
	@echo "  check              All three"
	@echo "  deploy             System install (needs root): sudo make deploy"
	@echo "  deploy-user-timer  Enable the --user on-battery tick timer (NO sudo)"
	@echo "  nut-snippets       Print the NUT config changes to apply by hand"

venv:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

lint:
	$(BASE)/.venv/bin/ruff check .

type:
	$(BASE)/.venv/bin/mypy

test:
	$(BASE)/.venv/bin/pytest -q

check: lint type test

# System install (root): venv + /etc config+env + /var/lib state + dispatcher + ACLs.
deploy:
	@deploy/install.sh

# On-battery countdown timer as a systemd --user service (NO sudo).
deploy-user-timer:
	@deploy/install-user-timer.sh

nut-snippets:
	@echo "Apply these to $(NUT_DIR) (back up first), then: systemctl restart nut-driver-enumerator nut-server nut-monitor"
	@echo "  - ups.conf       : append deploy/nut/ups.conf.snippet (adds cyberpower2)"
	@echo "  - upssched.conf  : replace with deploy/nut/upssched.conf.snippet (fixes CMDSCRIPT path)"
	@echo "  - upsmon.conf    : merge deploy/nut/upsmon.conf.snippet (adds 2nd MONITOR, NOTIFYFLAGs)"
