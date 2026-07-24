PROJECT := ups-orchestrator
BASE    := $(CURDIR)
PY      := $(BASE)/.venv/bin/python3
ORCH    := $(BASE)/.venv/bin/ups-orchestrator

NUT_DIR        := /etc/nut
SYSTEMD_DIR    := /etc/systemd/system
ENV_FILE       := /etc/ups-orchestrator.env
STATE_DIR      := /var/lib/$(PROJECT)

.PHONY: help venv lint type test coverage check deploy deploy-user-service nut-snippets

help:
	@echo "Targets:"
	@echo "  venv                 Create .venv and install package + dev deps"
	@echo "  lint type test       Run ruff / mypy / pytest"
	@echo "  coverage             Run pytest with branch coverage + term-missing"
	@echo "  mutation             Targeted mutation test of critical logic"
	@echo "  check                All three"
	@echo "  deploy               System install (needs root): sudo make deploy"
	@echo "  deploy-user-service  Enable the --user poll-loop service (NO sudo)"
	@echo "  nut-snippets         Print the NUT config changes to apply by hand"

venv:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

lint:
	$(BASE)/.venv/bin/ruff check .

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

# System install (root): venv + /etc config+env + /var/lib state + dispatcher + ACLs.
deploy:
	@deploy/install.sh

# Poll-loop as a systemd --user service (NO sudo).
deploy-user-service:
	@deploy/install-user-service.sh

nut-snippets:
	@echo "Apply these to $(NUT_DIR) (back up first), then: systemctl restart nut-driver-enumerator nut-server nut-monitor"
	@echo "  - ups.conf       : append deploy/nut/ups.conf.snippet (one section per UPS)"
	@echo "  - upssched.conf  : replace with deploy/nut/upssched.conf.snippet (fixes CMDSCRIPT path)"
	@echo "  - upsmon.conf    : merge deploy/nut/upsmon.conf.snippet (one MONITOR per UPS, NOTIFYFLAGs)"
