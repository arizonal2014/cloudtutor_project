#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/6] Running core local flow verifiers..."
./scripts/verify_session03_flow.sh
./scripts/verify_session05_computer_use.sh
./scripts/verify_session07_show_more.sh
./scripts/verify_session08_artifact.sh
./scripts/verify_session09_persistence.sh

echo "[2/6] Running deployment and hardening verifiers..."
./scripts/verify_session10_deploy.sh
./scripts/verify_session11_hardening.sh

echo "[3/6] Running frontend-next smoke..."
./scripts/verify_frontend_next.sh

echo "[4/6] Running demo readiness verifier..."
./scripts/verify_session12_demo.sh

echo "[5/6] Running grounding benchmark (skips if creds unavailable)..."
./scripts/verify_session04_grounding.sh

echo "[6/6] Submission gate passed."
