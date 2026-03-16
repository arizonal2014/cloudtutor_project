#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloudtutor-490215}}"
REGION="${REGION:-${GCP_REGION:-us-central1}}"
SERVICE_NAME="${SERVICE_NAME:-cloudtutor-backend}"
ARTIFACT_REPO="${ARTIFACT_REPO:-cloudtutor-images}"
IMAGE_NAME="${IMAGE_NAME:-cloudtutor-backend}"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d-%H%M%S)}"
ENV_FILE="${ENV_FILE:-infra/env/cloudrun.env.yaml}"
USE_TERRAFORM="${USE_TERRAFORM:-1}"
SKIP_BUILD="${SKIP_BUILD:-0}"
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-1}"
RUNTIME_SERVICE_ACCOUNT_ID="${RUNTIME_SERVICE_ACCOUNT_ID:-cloudtutor-backend-sa}"
RUNTIME_SERVICE_ACCOUNT_EMAIL="${RUNTIME_SERVICE_ACCOUNT_EMAIL:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --project <id>          GCP project id (default: ${PROJECT_ID})
  --region <region>       GCP region (default: ${REGION})
  --service <name>        Cloud Run service name (default: ${SERVICE_NAME})
  --repo <name>           Artifact Registry repo (default: ${ARTIFACT_REPO})
  --image-name <name>     Image name (default: ${IMAGE_NAME})
  --tag <tag>             Image tag (default: UTC timestamp)
  --env-file <path>       Cloud Run env vars yaml (default: ${ENV_FILE})
  --runtime-sa-id <id>    Runtime service account id (default: ${RUNTIME_SERVICE_ACCOUNT_ID})
  --runtime-sa-email <e>  Runtime service account email (overrides --runtime-sa-id)
  --no-terraform          Deploy via gcloud only
  --use-terraform         Deploy via Terraform (default)
  --skip-build            Reuse existing image tag (skip cloud build)
  --private               Disable unauthenticated access
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="$2"
      shift 2
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    --service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --repo)
      ARTIFACT_REPO="$2"
      shift 2
      ;;
    --image-name)
      IMAGE_NAME="$2"
      shift 2
      ;;
    --tag)
      IMAGE_TAG="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --runtime-sa-id)
      RUNTIME_SERVICE_ACCOUNT_ID="$2"
      shift 2
      ;;
    --runtime-sa-email)
      RUNTIME_SERVICE_ACCOUNT_EMAIL="$2"
      shift 2
      ;;
    --no-terraform)
      USE_TERRAFORM="0"
      shift
      ;;
    --use-terraform)
      USE_TERRAFORM="1"
      shift
      ;;
    --skip-build)
      SKIP_BUILD="1"
      shift
      ;;
    --private)
      ALLOW_UNAUTHENTICATED="0"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

need_cmd gcloud

ensure_project_role() {
  local member="$1"
  local role="$2"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "$member" \
    --role "$role" \
    --quiet >/dev/null
}

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
if [[ -z "$ACTIVE_ACCOUNT" ]]; then
  echo "No active gcloud account found. Run: gcloud auth login --update-adc"
  exit 1
fi

echo "[1/7] Using account: ${ACTIVE_ACCOUNT}"
echo "[2/7] Setting active project: ${PROJECT_ID}"
gcloud config set project "$PROJECT_ID" >/dev/null

PROJECT_NUMBER="$(
  gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)'
)"
CLOUD_BUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
COMPUTE_DEFAULT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
if [[ -z "$RUNTIME_SERVICE_ACCOUNT_EMAIL" ]]; then
  RUNTIME_SERVICE_ACCOUNT_EMAIL="${RUNTIME_SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
else
  RUNTIME_SERVICE_ACCOUNT_ID="${RUNTIME_SERVICE_ACCOUNT_EMAIL%@*}"
fi

echo "[3/7] Enabling required APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project "$PROJECT_ID" >/dev/null

echo "[3.1/7] Ensuring runtime service account exists..."
if ! gcloud iam service-accounts describe "$RUNTIME_SERVICE_ACCOUNT_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SERVICE_ACCOUNT_ID" \
    --display-name="CloudTutor Backend Runtime" \
    --project "$PROJECT_ID" >/dev/null
fi

echo "[3.2/7] Granting runtime IAM roles..."
for role in \
  roles/logging.logWriter \
  roles/monitoring.metricWriter \
  roles/storage.objectAdmin \
  roles/datastore.user \
  roles/aiplatform.user \
  roles/secretmanager.secretAccessor; do
  ensure_project_role "serviceAccount:${RUNTIME_SERVICE_ACCOUNT_EMAIL}" "$role"
done

echo "[3.3/7] Granting build IAM roles..."
for build_sa in "$CLOUD_BUILD_SA" "$COMPUTE_DEFAULT_SA"; do
  ensure_project_role "serviceAccount:${build_sa}" "roles/artifactregistry.writer"
  ensure_project_role "serviceAccount:${build_sa}" "roles/storage.objectAdmin"
  ensure_project_role "serviceAccount:${build_sa}" "roles/logging.logWriter"
done

echo "[4/7] Ensuring Artifact Registry repository exists..."
if ! gcloud artifacts repositories describe "$ARTIFACT_REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$ARTIFACT_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="CloudTutor backend images" \
    --project "$PROJECT_ID"
fi

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"

if [[ "$SKIP_BUILD" != "1" ]]; then
  echo "[5/7] Building and pushing image: ${IMAGE_URI}"
  gcloud builds submit \
    backend \
    --project "$PROJECT_ID" \
    --tag "$IMAGE_URI"
else
  echo "[5/7] Skipping build. Reusing image: ${IMAGE_URI}"
fi

ALLOW_UNAUTHENTICATED_BOOL="true"
if [[ "$ALLOW_UNAUTHENTICATED" != "1" ]]; then
  ALLOW_UNAUTHENTICATED_BOOL="false"
fi

if [[ "$USE_TERRAFORM" == "1" ]]; then
  need_cmd terraform
  echo "[6/7] Applying Terraform infrastructure and Cloud Run service..."
  terraform -chdir=infra/terraform init

  TF_ARGS=(
    "-var=project_id=${PROJECT_ID}"
    "-var=region=${REGION}"
    "-var=service_name=${SERVICE_NAME}"
    "-var=artifact_repo=${ARTIFACT_REPO}"
    "-var=image_name=${IMAGE_NAME}"
    "-var=image_tag=${IMAGE_TAG}"
    "-var=container_image=${IMAGE_URI}"
    "-var=allow_unauthenticated=${ALLOW_UNAUTHENTICATED_BOOL}"
    "-var=create_artifact_registry=false"
  )

  if [[ -f "infra/terraform/terraform.tfvars" ]]; then
    TF_ARGS+=("-var-file=terraform.tfvars")
  fi

  if [[ -f "$ENV_FILE" ]] && [[ -x ".venv/bin/python" ]]; then
    ENV_JSON="$(
      .venv/bin/python - <<'PY' "$ENV_FILE"
import json
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    print("{}")
    raise SystemExit(0)

path = Path(sys.argv[1])
if not path.exists():
    print("{}")
    raise SystemExit(0)

data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
if not isinstance(data, dict):
    print("{}")
    raise SystemExit(0)

normalized = {str(k): str(v) for k, v in data.items()}
print(json.dumps(normalized, separators=(",", ":")))
PY
    )"
    if [[ "$ENV_JSON" != "{}" ]]; then
      TF_ARGS+=("-var=env_vars=${ENV_JSON}")
    fi
  fi

  terraform -chdir=infra/terraform apply -auto-approve "${TF_ARGS[@]}"
  SERVICE_URL="$(terraform -chdir=infra/terraform output -raw service_url 2>/dev/null || true)"
else
  echo "[6/7] Deploying Cloud Run service with gcloud..."
  DEPLOY_ARGS=(
    run deploy "$SERVICE_NAME"
    --project "$PROJECT_ID"
    --region "$REGION"
    --platform managed
    --image "$IMAGE_URI"
    --port 8080
    --service-account "$RUNTIME_SERVICE_ACCOUNT_EMAIL"
    --quiet
  )
  if [[ -f "$ENV_FILE" ]]; then
    DEPLOY_ARGS+=(--env-vars-file "$ENV_FILE")
  fi
  if [[ "$ALLOW_UNAUTHENTICATED" == "1" ]]; then
    DEPLOY_ARGS+=(--allow-unauthenticated)
  else
    DEPLOY_ARGS+=(--no-allow-unauthenticated)
  fi
  gcloud "${DEPLOY_ARGS[@]}"
  SERVICE_URL="$(
    gcloud run services describe "$SERVICE_NAME" \
      --project "$PROJECT_ID" \
      --region "$REGION" \
      --format='value(status.url)'
  )"
fi

echo "[7/7] Deployment complete."
echo "Service: ${SERVICE_NAME}"
echo "Region:  ${REGION}"
echo "Image:   ${IMAGE_URI}"
echo "Runtime SA: ${RUNTIME_SERVICE_ACCOUNT_EMAIL}"
echo "URL:     ${SERVICE_URL}"

if [[ "$ALLOW_UNAUTHENTICATED" == "1" ]] && [[ -n "$SERVICE_URL" ]]; then
  echo "Health check:"
  curl -fsS "${SERVICE_URL}/health" || true
fi
