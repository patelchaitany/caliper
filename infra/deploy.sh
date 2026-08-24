#!/usr/bin/env bash
# Deploy Caliper to Cloud Run with a service account scoped to Vertex AI.
#
# Usage: ./infra/deploy.sh PROJECT_ID [REGION]
#
# This script does not authenticate for you. Run `gcloud auth login` first.

set -euo pipefail

PROJECT="${1:?usage: deploy.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"
SERVICE="caliper"
REPO="caliper"
SA="caliper-runtime"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:latest"

echo "==> Project ${PROJECT}, region ${REGION}"
gcloud config set project "${PROJECT}" >/dev/null

echo "==> Enabling required services"
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

echo "==> Ensuring Artifact Registry repository"
gcloud artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Caliper container images"

echo "==> Ensuring runtime service account"
SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SA}" \
    --display-name="Caliper Cloud Run runtime"

# Vertex AI access only. Caliper needs nothing else.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user" \
  --condition=None >/dev/null

echo "==> Building ${IMAGE}"
gcloud builds submit --tag "${IMAGE}" --file infra/Dockerfile .

echo "==> Deploying"
# --no-allow-unauthenticated: Caliper has no authentication of its own and must
# not be reachable without an identity token.
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --no-allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 900 \
  --concurrency 4 \
  --set-env-vars "CALIPER_BACKEND=vertex,CALIPER_GCP_PROJECT=${PROJECT},CALIPER_GCP_REGION=${REGION},CALIPER_MODEL=claude-opus-5,CALIPER_EFFORT=high"

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
cat <<MSG

Deployed: ${URL}

Try it:
  curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" \\
       -H "Content-Type: application/json" \\
       -d '{"author":"you@team.dev","files":{"a.py":"x = eval(input())\\n"}}' \\
       "${URL}/v1/reviews"

Note: the SQLite ledger is on the container filesystem and resets with each
revision. See docs/GCP.md for persisting it.
MSG
