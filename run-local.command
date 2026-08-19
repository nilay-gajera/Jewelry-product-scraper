#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
umask 077

if [[ -f "$SCRIPT_DIR/.env.local" ]]; then
  set -a
  source "$SCRIPT_DIR/.env.local"
  set +a
fi

RUNTIME_PATH="$SCRIPT_DIR/runtime/local-live"
DATABASE_PATH="$RUNTIME_PATH/export/catalog.sqlite"

if [[ ! -f "$DATABASE_PATH" ]]; then
  echo "Missing local catalog checkpoint: $DATABASE_PATH"
  echo "Download and decompress jewelry-product-scraper/checkpoints/catalog.sqlite.gz from S3 first (or use the legacy catalog.sqlite object)."
  read -r "?Press Return to close."
  exit 1
fi

export RUNTIME_DIR="$RUNTIME_PATH"
export CONTROL_TOKEN="${CONTROL_TOKEN:-$(openssl rand -hex 24)}"
export AWS_REGION="${AWS_REGION:-us-west-1}"
export S3_BUCKET="${S3_BUCKET:-nilay-jewelry-scraper-2026-406658519322-us-west-1-an}"
export S3_PREFIX="${S3_PREFIX:-jewelry-product-scraper}"
export S3_PUBLIC_BASE_URL="${S3_PUBLIC_BASE_URL:-https://nilay-jewelry-scraper-2026-406658519322-us-west-1.amazonaws.com/jewelry-product-scraper/media}"
export S3_ENRICH_UPLOAD_EVERY="${S3_ENRICH_UPLOAD_EVERY:-500}"
export S3_CHECKPOINT_COMPRESSION_LEVEL="${S3_CHECKPOINT_COMPRESSION_LEVEL:-6}"

echo "Starting Jewelry Product Scraper at http://127.0.0.1:8000"
echo "Control token: $CONTROL_TOKEN"
if [[ -z "${AWS_ACCESS_KEY_ID:-}" && ! -f "$HOME/.aws/credentials" ]]; then
  echo "Warning: no local AWS credentials were found. Add rotated credentials to .env.local before starting enrichment."
fi
echo "Press Control-C to stop it."
exec "$SCRIPT_DIR/.venv/bin/uvicorn" service:app --host 127.0.0.1 --port 8000
