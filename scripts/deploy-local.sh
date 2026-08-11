#!/usr/bin/env bash
# Local tests + frontend build + CDK deploy (Docker image built by CDK asset)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-central-1}}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"

echo "==> Region=$REGION Account=$ACCOUNT"

echo "==> Backend unit tests"
cd "$ROOT/backend"
# shellcheck disable=SC1091
source venv/bin/activate 2>/dev/null || true
pip install -q pytest httpx
pytest tests/ -q

echo "==> Frontend build (static export)"
cd "$ROOT"
npm ci --prefer-offline
# Placeholder URLs — after first deploy, re-run with real ApiBaseUrl / WarmupUrl
export NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000}"
export NEXT_PUBLIC_WARMUP_URL="${NEXT_PUBLIC_WARMUP_URL:-}"
npm run build

echo "==> CDK bootstrap + deploy"
cd "$ROOT/infra/cdk"
# shellcheck disable=SC1091
set -a
source "$ROOT/backend/.env" 2>/dev/null || true
set +a
export BACKEND_API_KEY="${BACKEND_API_KEY:-}"
npm ci --prefer-offline
npx cdk bootstrap "aws://${ACCOUNT}/${REGION}" || true
npx cdk deploy AutocaptionStack --require-approval never \
  -c "stylesBucketName=${AWS_S3_BUCKET:-autocaption-styles-deadzone-423623826655}"

echo "==> Done. Check CloudFormation outputs for ApiBaseUrl / CloudFrontUrl / WarmupUrl / ApiKeySecretArn."
echo "    Re-build frontend with NEXT_PUBLIC_API_URL=<ApiBaseUrl> NEXT_PUBLIC_WARMUP_URL=<WarmupUrl> and redeploy to bake URLs into the static UI."
