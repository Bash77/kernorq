#!/usr/bin/env bash
# Kernorq -> Cloud Run deployment (bash variant of deploy-cloud-run.ps1)
set -euo pipefail

PROJECT="all-things-agentic-bash77"
REGION="us-central1"
SERVICE="kernorq"
REPO="kernorq"
SECRET="kernorq-google-api-key"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:latest"

if [[ -z "${GOOGLE_API_KEY:-}" ]]; then
  echo "GOOGLE_API_KEY env var is required (never stored in this repo)." >&2
  exit 1
fi

gcloud config set project "$PROJECT"
gcloud config set run/region "$REGION"

# Secret Manager: create if missing, always add a fresh version from env
if ! gcloud secrets describe "$SECRET" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET" --replication-policy=automatic --project="$PROJECT"
fi
printf '%s' "$GOOGLE_API_KEY" | gcloud secrets versions add "$SECRET" --data-file=-

# Runtime SA can read the secret
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding "$SECRET" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Build server-side (no local Docker required)
gcloud builds submit --tag "$IMAGE" .

# Deploy — scale-to-zero, secret injected at runtime
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --set-secrets="GOOGLE_API_KEY=${SECRET}:latest"

gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)'
