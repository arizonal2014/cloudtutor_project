SHELL := /bin/bash

VENV_PY := .venv/bin/python
VENV_UVICORN := .venv/bin/uvicorn
BACKEND_HOST ?= 127.0.0.1
BACKEND_PORT ?= 8080
FRONTEND_PORT ?= 4173
FRONTEND_NEXT_PORT ?= 4174

.PHONY: help backend frontend frontend-next dev dev-next smoke smoke-live smoke-flow smoke-flow-sweep smoke-grounding smoke-computer-use smoke-show-more smoke-artifact smoke-persistence smoke-deploy smoke-hardening smoke-demo smoke-submission purge-persistence smoke-next smoke-cloud-access verify-session01 verify-session02 verify-session03 verify-session03-sweep verify-session04 verify-session05 verify-session07 verify-session08 verify-session09 verify-session10 verify-session11 verify-session12 verify-session13 verify-next verify-cloud deploy-cloud-run

help:
	@echo "Targets:"
	@echo "  make backend           Run backend API server"
	@echo "  make frontend          Run static frontend server"
	@echo "  make frontend-next     Run Next.js frontend server"
	@echo "  make dev               Run backend + frontend together"
	@echo "  make dev-next          Run backend + frontend-next together"
	@echo "  make smoke             Run Session 01 smoke verification"
	@echo "  make smoke-live        Run Session 02 live roundtrip verification (if creds configured)"
	@echo "  make smoke-flow        Run Session 03 flow-gate verification (if creds configured)"
	@echo "  make smoke-flow-sweep  Run Session 03 10-prompt behavior sweep (if creds configured)"
	@echo "  make smoke-grounding   Run Session 04 grounding benchmark (>=8/10 with source links)"
	@echo "  make smoke-computer-use Run Session 05 Computer Use worker foundation checks"
	@echo "  make smoke-show-more   Run Session 07 narrated show-more navigation checks"
	@echo "  make smoke-artifact    Run Session 08 tutorial artifact generation checks"
	@echo "  make smoke-persistence Run Session 09 persistence + restart checks"
	@echo "  make smoke-deploy      Run Session 10 deploy/infra verification checks"
	@echo "  make smoke-hardening   Run Session 11 reliability/hardening verification"
	@echo "  make smoke-demo        Run Session 12 demo-readiness verification"
	@echo "  make smoke-submission  Run Session 13 final submission gate"
	@echo "  make deploy-cloud-run  Build and deploy backend to Cloud Run"
	@echo "  make purge-persistence Purge old local session/artifact data (14d default)"
	@echo "  make smoke-next        Run frontend-next smoke verification"
	@echo "  make smoke-cloud-access Verify gcloud/firebase access for CloudTutor projects"
	@echo "  make verify-session01  Alias for smoke"
	@echo "  make verify-session02  Alias for smoke-live"
	@echo "  make verify-session03  Alias for smoke-flow"
	@echo "  make verify-session03-sweep  Alias for smoke-flow-sweep"
	@echo "  make verify-session04  Alias for smoke-grounding"
	@echo "  make verify-session05  Alias for smoke-computer-use"
	@echo "  make verify-session07  Alias for smoke-show-more"
	@echo "  make verify-session08  Alias for smoke-artifact"
	@echo "  make verify-session09  Alias for smoke-persistence"
	@echo "  make verify-session10  Alias for smoke-deploy"
	@echo "  make verify-session11  Alias for smoke-hardening"
	@echo "  make verify-session12  Alias for smoke-demo"
	@echo "  make verify-session13  Alias for smoke-submission"
	@echo "  make verify-next       Alias for smoke-next"
	@echo "  make verify-cloud      Alias for smoke-cloud-access"

backend:
	$(VENV_UVICORN) backend.app.main:app --reload --host $(BACKEND_HOST) --port $(BACKEND_PORT)

frontend:
	$(VENV_PY) -m http.server $(FRONTEND_PORT) --bind 127.0.0.1 --directory frontend

frontend-next:
	npm --prefix frontend-next run dev -- --hostname 127.0.0.1 --port $(FRONTEND_NEXT_PORT)

dev:
	./scripts/dev.sh

dev-next:
	./scripts/dev_next.sh

smoke:
	./scripts/verify_session01.sh

verify-session01: smoke

smoke-live:
	./scripts/verify_session02_live.sh

verify-session02: smoke-live

smoke-flow:
	./scripts/verify_session03_flow.sh

verify-session03: smoke-flow

smoke-flow-sweep:
	./scripts/verify_session03_prompt_sweep.sh

verify-session03-sweep: smoke-flow-sweep

smoke-grounding:
	./scripts/verify_session04_grounding.sh

verify-session04: smoke-grounding

smoke-computer-use:
	./scripts/verify_session05_computer_use.sh

verify-session05: smoke-computer-use

smoke-show-more:
	./scripts/verify_session07_show_more.sh

verify-session07: smoke-show-more

smoke-artifact:
	./scripts/verify_session08_artifact.sh

verify-session08: smoke-artifact

smoke-persistence:
	./scripts/verify_session09_persistence.sh

verify-session09: smoke-persistence

smoke-deploy:
	./scripts/verify_session10_deploy.sh

verify-session10: smoke-deploy

smoke-hardening:
	./scripts/verify_session11_hardening.sh

verify-session11: smoke-hardening

smoke-demo:
	./scripts/verify_session12_demo.sh

verify-session12: smoke-demo

smoke-submission:
	./scripts/verify_session13_submission.sh

verify-session13: smoke-submission

deploy-cloud-run:
	./scripts/deploy_cloud_run.sh

purge-persistence:
	./scripts/purge_local_persistence.sh

smoke-next:
	./scripts/verify_frontend_next.sh

verify-next: smoke-next

smoke-cloud-access:
	./scripts/verify_cloud_access.sh

verify-cloud: smoke-cloud-access
