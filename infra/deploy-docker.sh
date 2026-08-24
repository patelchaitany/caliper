#!/usr/bin/env bash
# Deploy Caliper to any Docker host (VM, homelab, bare metal).
#
# Usage: ./infra/deploy-docker.sh [PORT]
#
# Defaults are deliberately conservative because this is often run on a box
# that is already doing something else:
#
#   * bound to 127.0.0.1 — Caliper has no authentication of its own and must
#     not be reachable from the internet. Put it behind your existing reverse
#     proxy / SSO if you need remote access.
#   * memory and CPU capped — it cannot starve whatever else lives on the host.
#   * the ledger lives in a named volume, so reviews, score history and
#     conventions survive `docker rm`.

set -euo pipefail

PORT="${1:-8090}"
NAME="caliper"
IMAGE="caliper:latest"

cd "$(dirname "$0")/.."

echo "==> Building ${IMAGE}"
# nice'd: on a small shared box, an unthrottled build can push other services
# into swap.
nice -n 19 docker build -q -f infra/Dockerfile -t "${IMAGE}" .

echo "==> Replacing container"
docker rm -f "${NAME}" >/dev/null 2>&1 || true
docker volume create caliper-data >/dev/null

# Backend selection. With no credentials present, Caliper starts in `replay`
# mode: a local pattern matcher that exercises the whole pipeline but is NOT a
# real reviewer. Export ANTHROPIC_API_KEY (or set CALIPER_BACKEND=vertex with
# GCP ADC mounted) to get real reviews.
BACKEND_ENV=(-e "CALIPER_BACKEND=${CALIPER_BACKEND:-replay}")
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  BACKEND_ENV=(-e "CALIPER_BACKEND=${CALIPER_BACKEND:-anthropic}" -e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}")
elif [ "${CALIPER_BACKEND:-}" = "vertex" ]; then
  BACKEND_ENV+=(
    -e "CALIPER_GCP_PROJECT=${CALIPER_GCP_PROJECT:?vertex backend needs CALIPER_GCP_PROJECT}"
    -e "CALIPER_GCP_REGION=${CALIPER_GCP_REGION:-us-central1}"
    -e "GOOGLE_APPLICATION_CREDENTIALS=/adc.json"
    -v "${HOME}/.config/gcloud/application_default_credentials.json:/adc.json:ro"
  )
fi

docker run -d --name "${NAME}" \
  --restart unless-stopped \
  --memory 256m --memory-swap 512m \
  --cpus 0.5 \
  -p "127.0.0.1:${PORT}:8080" \
  -v caliper-data:/data \
  -e CALIPER_LEDGER=/data/ledger.db \
  -e "CALIPER_MODEL=${CALIPER_MODEL:-claude-opus-5}" \
  -e "CALIPER_EFFORT=${CALIPER_EFFORT:-high}" \
  -e "CALIPER_OUTPUT_MODE=${CALIPER_OUTPUT_MODE:-auto}" \
  "${BACKEND_ENV[@]}" \
  "${IMAGE}" >/dev/null

sleep 5
echo "==> Health"
curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/healthz" && echo

cat <<MSG

Caliper is on http://127.0.0.1:${PORT} (localhost only).

  health   curl http://127.0.0.1:${PORT}/healthz
  rubric   curl http://127.0.0.1:${PORT}/v1/rubric
  review   curl -X POST http://127.0.0.1:${PORT}/v1/reviews \\
             -H 'Content-Type: application/json' \\
             -d '{"author":"you@team.dev","files":{"a.py":"x = eval(input())\\n"}}'

If /healthz reports "backend":"replay", it is pattern-matching locally, not
reviewing. Re-run with ANTHROPIC_API_KEY exported to get real reviews.
MSG
