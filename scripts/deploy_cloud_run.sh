#!/usr/bin/env bash
# Deploy the PUBLIC Candor web app (chat UI + Gemini agent) to Cloud Run.
# The Candor engine is a separate, private service that is NOT part of this repository: provide its URL
# (CANDOR_ENGINE_URL) or deploy it as a Cloud Run service named "candor-engine" in the same region.
#
#   export PROJECT_ID=<your-project>
#   export CHECKPOINT_PATH=<identifier of the model bundle your private engine should load>
#   export GEMINI_MODEL=gemini-2.5-flash            # optional
#   # optional: CANDOR_ENGINE_URL, REGION, DEMO_ACCESS_CODE, CANDOR_ENGINE_KEY_HEADER
#   bash scripts/deploy_cloud_run.sh
#
# Prerequisite: a Secret Manager secret named CANDOR_ENGINE_KEY holding the shared key your engine expects.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${CHECKPOINT_PATH:?set CHECKPOINT_PATH}"
REGION="${REGION:-asia-southeast1}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
GEMINI_LOCATION="${GEMINI_LOCATION:-global}"
ENGINE_SERVICE="${ENGINE_SERVICE:-candor-engine}"
WEB_SERVICE="${WEB_SERVICE:-candor}"
REPO="${REPO:-candor}"
WEB_SA="candor-web@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${WEB_SERVICE}:latest"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WEB_DIR="${HERE}/app"

gcloud config set project "$PROJECT_ID" >/dev/null
[ "$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)')" = "True" ] || {
  echo "Billing is not linked to $PROJECT_ID. Link it first, then rerun."; exit 1; }

echo "== Safety check: the public image must not contain model files, credentials or private keys"
LEAKS=$(find "$WEB_DIR" \( -name '*.zip' -o -name '*.pkl' -o -name '*.joblib' -o -name '*.pt' -o -name '*.onnx' \
  -o -name '*.npz' -o -name '*.safetensors' -o -name '.env' \) -print)
if [ -n "$LEAKS" ]; then echo "STOP - forbidden files in app/:"; echo "$LEAKS"; exit 1; fi
if grep -rEq 'AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----' "$WEB_DIR"; then
  echo "STOP - credential-like string in app/"; exit 1; fi
echo "   clean."

CANDOR_ENGINE_URL="${CANDOR_ENGINE_URL:-$(gcloud run services describe "$ENGINE_SERVICE" --region "$REGION" --format 'value(status.url)' 2>/dev/null || true)}"
[ -n "$CANDOR_ENGINE_URL" ] || { echo "No Candor engine found. The engine is private and not part of this repository: set CANDOR_ENGINE_URL, or deploy your own Cloud Run service named '$ENGINE_SERVICE' in $REGION."; exit 1; }
echo "   Candor engine (private) URL: $CANDOR_ENGINE_URL"

echo "== Enabling APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com aiplatform.googleapis.com compute.googleapis.com
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION"

echo "== Service account + permissions (least privilege)"
gcloud iam service-accounts describe "$WEB_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create candor-web --display-name "Candor public demo"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${WEB_SA}" \
  --role roles/aiplatform.user --condition=None >/dev/null
gcloud run services add-iam-policy-binding "$ENGINE_SERVICE" --region "$REGION" \
  --member "serviceAccount:${WEB_SA}" --role roles/run.invoker >/dev/null

echo "== Demo access code (shared with judges)"
gcloud secrets describe CANDOR_ENGINE_KEY >/dev/null 2>&1 || { echo "Create the Secret Manager secret CANDOR_ENGINE_KEY first."; exit 1; }
if ! gcloud secrets describe DEMO_ACCESS_CODE >/dev/null 2>&1; then
  CODE="${DEMO_ACCESS_CODE:-candor-$(openssl rand -hex 3)}"
  printf %s "$CODE" | gcloud secrets create DEMO_ACCESS_CODE --data-file=-
elif [ -n "${DEMO_ACCESS_CODE:-}" ]; then
  printf %s "$DEMO_ACCESS_CODE" | gcloud secrets versions add DEMO_ACCESS_CODE --data-file=-
fi
for s in CANDOR_ENGINE_KEY DEMO_ACCESS_CODE; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:${WEB_SA}" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

echo "== Build"
gcloud builds submit --tag "$IMAGE" "$WEB_DIR"

echo "== Deploy"
# --no-cpu-throttling : jobs run in a background thread after the request returns
# min/max instances 1 : chat sessions live in memory; min 1 avoids a cold start in front of a judge
ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GEMINI_LOCATION},GEMINI_MODEL=${GEMINI_MODEL},CANDOR_ENGINE_URL=${CANDOR_ENGINE_URL},CHECKPOINT_PATH=${CHECKPOINT_PATH},PUBLIC_MODE=1,MAX_TABLE_ROWS=500"
[ -z "${CANDOR_ENGINE_KEY_HEADER:-}" ] || ENV_VARS="${ENV_VARS},CANDOR_ENGINE_KEY_HEADER=${CANDOR_ENGINE_KEY_HEADER}"
gcloud run deploy "$WEB_SERVICE" \
  --image "$IMAGE" --region "$REGION" --service-account "$WEB_SA" \
  --allow-unauthenticated \
  --cpu 1 --memory 1Gi --timeout 900 \
  --concurrency 40 --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --set-secrets "CANDOR_ENGINE_KEY=CANDOR_ENGINE_KEY:latest,DEMO_ACCESS_CODE=DEMO_ACCESS_CODE:latest" \
  --set-env-vars "$ENV_VARS"

URL=$(gcloud run services describe "$WEB_SERVICE" --region "$REGION" --format 'value(status.url)')
CODE_NOW=$(gcloud secrets versions access latest --secret DEMO_ACCESS_CODE)
echo
echo "Demo URL:     $URL"
echo "Access code:  $CODE_NOW"
echo "JUDGE LINK (share privately, never commit it): $URL/?k=$CODE_NOW"
echo "Health check: curl $URL/healthz"
echo "The engine stays private: curl -s -o /dev/null -w '%{http_code}' $CANDOR_ENGINE_URL/health   ->  403"
