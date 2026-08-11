#!/usr/bin/env bash
# Create private autocaption style assets bucket (versioning off / suspended).
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-central-1}}"
SUFFIX="${USER:-dev}"
BUCKET_NAME="${AWS_S3_BUCKET:-autocaption-styles-${SUFFIX}-$(aws sts get-caller-identity --query Account --output text)}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK_NAME="${STACK_NAME:-autocaption-style-assets}"

echo "Region:  $REGION"
echo "Bucket:  $BUCKET_NAME"
echo "Stack:   $STACK_NAME"

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$ROOT/infra/style-assets-bucket.yaml" \
  --parameter-overrides "BucketName=$BUCKET_NAME" \
  --capabilities CAPABILITY_NAMED_IAM

echo ""
echo "Add to backend/.env:"
echo "AWS_REGION=$REGION"
echo "AWS_S3_BUCKET=$BUCKET_NAME"
echo "STYLE_STORAGE=s3"
