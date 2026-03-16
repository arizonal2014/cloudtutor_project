#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/6] Verifying Session 10 deployment assets exist..."
REQUIRED_FILES=(
  "backend/Dockerfile"
  "backend/requirements.txt"
  "cloudbuild.backend.yaml"
  "infra/terraform/versions.tf"
  "infra/terraform/variables.tf"
  "infra/terraform/main.tf"
  "infra/terraform/outputs.tf"
  "infra/terraform/terraform.tfvars.example"
  "infra/env/cloudrun.env.yaml.example"
  "scripts/deploy_cloud_run.sh"
)

for file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "Missing required file: $file"
    exit 1
  fi
done
echo "asset_check=ok"

echo "[2/6] Validating shell scripts syntax..."
bash -n scripts/deploy_cloud_run.sh
bash -n scripts/verify_session10_deploy.sh
echo "shell_syntax=ok"

echo "[3/6] Compiling backend python modules..."
if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv python executable."
  exit 1
fi
.venv/bin/python -m compileall backend/app >/dev/null
echo "python_compile=ok"

echo "[4/6] Terraform static validation..."
if command -v terraform >/dev/null 2>&1; then
  terraform -chdir=infra/terraform fmt -check
  terraform -chdir=infra/terraform init -backend=false >/dev/null
  terraform -chdir=infra/terraform validate
  echo "terraform_validate=ok"
else
  echo "terraform not found; skipped terraform validate"
fi

echo "[5/6] Cloud access verification..."
if command -v gcloud >/dev/null 2>&1 && command -v firebase >/dev/null 2>&1; then
  ./scripts/verify_cloud_access.sh
else
  echo "gcloud/firebase not available; skipped cloud access verification"
fi

echo "[6/6] Session 10 deployment verification passed."
