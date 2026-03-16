#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/5] Verifying demo documentation assets..."
test -f docs/DEMO_RUNBOOK.md
test -f docs/JUDGE_RUBRIC_MAP.md
rg -q "Suggested Demo Timeline" docs/DEMO_RUNBOOK.md
rg -q "Innovation" docs/JUDGE_RUBRIC_MAP.md
echo "demo_docs_ok"

echo "[2/5] Verifying README references demo/deploy flow..."
rg -q "Session 10" README.md
rg -q "Cloud Access Bootstrap" README.md
echo "readme_demo_refs_ok"

echo "[3/5] Verifying required verification scripts exist..."
for script_name in \
  scripts/verify_session07_show_more.sh \
  scripts/verify_session08_artifact.sh \
  scripts/verify_session10_deploy.sh \
  scripts/verify_session11_hardening.sh; do
  test -f "$script_name"
done
echo "demo_verifier_refs_ok"

echo "[4/5] Running baseline hardening and artifact verifiers..."
./scripts/verify_session11_hardening.sh
./scripts/verify_session08_artifact.sh
echo "demo_baseline_verifiers_ok"

echo "[5/5] Session 12 demo readiness verification passed."
