#!/usr/bin/env bash
set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-cloudtutor-490215}"
FIREBASE_PROJECT="${FIREBASE_PROJECT:-cloudtutor-challenge}"

REQUIRED_APIS=(
  "run.googleapis.com"
  "artifactregistry.googleapis.com"
  "cloudbuild.googleapis.com"
  "firestore.googleapis.com"
  "storage.googleapis.com"
)

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

need_cmd gcloud
need_cmd firebase

echo "[1/7] CLI versions"
gcloud --version | head -n 1
firebase --version

echo "[2/7] Active gcloud account"
GCLOUD_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
if [[ -z "${GCLOUD_ACCOUNT}" ]]; then
  echo "No active gcloud account. Run: gcloud auth login --update-adc"
  exit 1
fi
echo "gcloud account: ${GCLOUD_ACCOUNT}"

echo "[3/7] Project access check (${GCP_PROJECT})"
if ! gcloud projects describe "${GCP_PROJECT}" --format='value(projectId)' >/dev/null 2>&1; then
  echo "No access to GCP project '${GCP_PROJECT}' with account '${GCLOUD_ACCOUNT}'."
  echo "Grant this account at least Editor (or equivalent scoped roles) on '${GCP_PROJECT}', then rerun."
  exit 1
fi
echo "project access: ok"

echo "[4/7] Set active gcloud project"
gcloud config set project "${GCP_PROJECT}" >/dev/null
echo "active project set: ${GCP_PROJECT}"

echo "[5/7] Application Default Credentials (ADC) check"
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "ADC is not ready. Run: gcloud auth application-default login"
  exit 1
fi
echo "adc: ok"

echo "[6/7] Required API status"
ENABLED_APIS="$(gcloud services list --enabled --format='value(config.name)' 2>/dev/null || true)"
MISSING_APIS=()
for api in "${REQUIRED_APIS[@]}"; do
  if ! printf '%s\n' "${ENABLED_APIS}" | rg -q "^${api}$"; then
    MISSING_APIS+=("${api}")
  fi
done

if [[ "${#MISSING_APIS[@]}" -gt 0 ]]; then
  echo "Missing enabled APIs:"
  printf ' - %s\n' "${MISSING_APIS[@]}"
  echo "Enable with:"
  echo "gcloud services enable ${MISSING_APIS[*]} --project ${GCP_PROJECT}"
  exit 1
fi
echo "required APIs: ok"

echo "[7/7] Firebase project access check (${FIREBASE_PROJECT})"
FIREBASE_PROJECTS_JSON="$(firebase projects:list --json 2>/dev/null || true)"
if [[ -z "${FIREBASE_PROJECTS_JSON}" ]]; then
  echo "Firebase CLI not authenticated. Run: firebase login"
  exit 1
fi

if ! printf '%s\n' "${FIREBASE_PROJECTS_JSON}" | rg -q "\"projectId\"\\s*:\\s*\"${FIREBASE_PROJECT}\""; then
  echo "Firebase project '${FIREBASE_PROJECT}' not visible to current firebase login."
  echo "Run: firebase login --reauth"
  echo "Then ensure the logged-in account has access to '${FIREBASE_PROJECT}'."
  exit 1
fi
echo "firebase access: ok"

echo "cloud_access_ok project=${GCP_PROJECT} firebase=${FIREBASE_PROJECT}"
