#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RETENTION_DAYS="${CLOUDTUTOR_RETENTION_DAYS:-14}"
SESSION_STORE_DIR="${CLOUDTUTOR_SESSION_STORE_DIR:-$ROOT_DIR/docs/sessions}"
ARTIFACT_DIR="${CLOUDTUTOR_ARTIFACT_DIR:-$ROOT_DIR/docs/artifacts}"

if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  echo "CLOUDTUTOR_RETENTION_DAYS must be an integer."
  exit 1
fi

echo "Purging local persistence older than ${RETENTION_DAYS} days..."
echo "Session store: ${SESSION_STORE_DIR}"
echo "Artifact dir:  ${ARTIFACT_DIR}"

if [[ -d "$SESSION_STORE_DIR/snapshots" ]]; then
  find "$SESSION_STORE_DIR/snapshots" -type f -name '*.json' -mtime +"$RETENTION_DAYS" -print -delete
fi

if [[ -d "$SESSION_STORE_DIR/events" ]]; then
  find "$SESSION_STORE_DIR/events" -type f -name '*.jsonl' -mtime +"$RETENTION_DAYS" -print -delete
fi

if [[ -d "$ARTIFACT_DIR" ]]; then
  find "$ARTIFACT_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -print -exec rm -rf {} +
fi

echo "Purge completed."
