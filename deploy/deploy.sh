#!/bin/bash
# =============================================================================
# BIQE HTR Pipeline - Cloud Run SERVICE Deployment Script
# =============================================================================
# Deploys the OCR Middleware as a Cloud Run SERVICE (not Job)
# This is for synchronous request/response handling with quota management
#
# Usage:
#   ./deploy/deploy.sh <PROJECT_ID> [REGION]
#
# Example (basic - Vertex AI only):
#   ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
#
# Example (with OpenRouter API key):
#   OPENROUTER_API_KEY="sk-or-v1-xxx" ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
#
# Example (with all custom settings):
#   OPENROUTER_API_KEY="sk-or-v1-xxx" \
#   OCR_PARALLEL_WORKERS=40 \
#   WORKER_RATE_LIMIT_SECONDS=1.5 \
#   ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
#
# Environment Variables (optional):
#   OPENROUTER_API_KEY       - Required for provider=openrouter (default: empty)
#   OCR_PARALLEL_WORKERS     - Number of parallel workers (default: 5)
#   WORKER_RATE_LIMIT_SECONDS - Delay between Gemini API calls (default: 15.0)
#   API_RPM_LIMIT            - Rate limit for documentation (default: 5)
# =============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
PROJECT_ID="${1:-}"
REGION="${2:-europe-west4}"
SERVICE_NAME="biqe-ocr-service"
REPO_NAME="biqe-repo"

# Validate
if [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}Error: PROJECT_ID is required${NC}"
    echo "Usage: ./deploy/deploy.sh <PROJECT_ID> [REGION]"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} BIQE OCR Middleware - Deployment${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
echo "Service: $SERVICE_NAME"
echo ""

# -----------------------------------------------------------------------------
# Step 1: Set project
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[1/5] Setting project...${NC}"
gcloud config set project "$PROJECT_ID"

# -----------------------------------------------------------------------------
# Step 2: Enable APIs
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[2/5] Enabling APIs...${NC}"
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    storage.googleapis.com

echo -e "${GREEN}✓ APIs enabled${NC}"

# -----------------------------------------------------------------------------
# Step 3: Create Artifact Registry repository
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[3/5] Creating Artifact Registry...${NC}"

if ! gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" &>/dev/null; then
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --description="BIQE HTR containers"
    echo "Created: $REPO_NAME"
else
    echo "Exists: $REPO_NAME"
fi

# Configure Docker auth
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo -e "${GREEN}✓ Artifact Registry ready${NC}"

# -----------------------------------------------------------------------------
# Step 4: Build and Push Docker image
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[4/5] Building and pushing Docker image...${NC}"

IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${SERVICE_NAME}:latest"

# Build the image for linux/amd64 (Cloud Run requires x86_64)
docker build --platform linux/amd64 -f deploy/Dockerfile -t "$IMAGE_URL" .

# Push to Artifact Registry
docker push "$IMAGE_URL"

echo -e "${GREEN}✓ Image pushed: ${IMAGE_URL}${NC}"

# -----------------------------------------------------------------------------
# Step 5: Deploy to Cloud Run SERVICE
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[5/5] Deploying to Cloud Run SERVICE...${NC}"

# GCS bucket names (derived from project ID)
GCS_INPUT_BUCKET="${PROJECT_ID}-htr-input"
GCS_OUTPUT_BUCKET="${PROJECT_ID}-htr-output"

# ===========================================================================
# PARALLEL PROCESSING CONFIGURATION (v3.1 - True Async Parallelism)
# ===========================================================================
# V3.1 uses asyncio.create_task() with semaphore for TRUE parallel processing.
# Multiple Gemini API calls can run concurrently within a single instance.
#
# GEMINI_CONCURRENT_LIMIT - Max concurrent Gemini API calls per instance
#   Default: 10 (safe for most Gemini quotas)
#   Increase if you have higher quotas (e.g., 20-50 for enterprise)
#   Decrease if hitting rate limits (e.g., 5 for preview tier)
#
# OLD SETTINGS (deprecated but kept for compatibility):
#   WORKER_RATE_LIMIT_SECONDS - No longer used (semaphore handles this)
#   OCR_PARALLEL_WORKERS - No longer used (asyncio handles this)
#
# To change after deployment:
#   gcloud run services update biqe-ocr-service --region europe-west4 \
#     --update-env-vars GEMINI_CONCURRENT_LIMIT=15
#
# Throughput calculation:
#   10 images @ 5s each = 50s total (sequential)
#   10 images @ 5s each with GEMINI_CONCURRENT_LIMIT=10 = ~5s total (parallel)
# ===========================================================================
GEMINI_CONCURRENT_LIMIT="${GEMINI_CONCURRENT_LIMIT:-10}"
API_RPM_LIMIT="${API_RPM_LIMIT:-60}"  # Updated default to 60 RPM
WORKER_RATE_LIMIT_SECONDS="${WORKER_RATE_LIMIT_SECONDS:-0}"  # Deprecated - set to 0
OCR_PARALLEL_WORKERS="${OCR_PARALLEL_WORKERS:-5}"  # Deprecated
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"

echo ""
echo "Configuration (v3.1 - True Async Parallelism):"
echo "  Concurrent Gemini calls per instance: ${GEMINI_CONCURRENT_LIMIT}"
echo "  Expected throughput: ${GEMINI_CONCURRENT_LIMIT} images in ~5 seconds"
if [ -n "$OPENROUTER_API_KEY" ]; then
    echo "  OpenRouter API Key: ****${OPENROUTER_API_KEY: -4}"
else
    echo "  OpenRouter API Key: NOT SET (provider=openrouter will fail)"
fi
echo ""

# Build env vars string
ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID}"
ENV_VARS="${ENV_VARS},GCP_REGION=${REGION}"
ENV_VARS="${ENV_VARS},GCS_INPUT_BUCKET=${GCS_INPUT_BUCKET}"
ENV_VARS="${ENV_VARS},GCS_OUTPUT_BUCKET=${GCS_OUTPUT_BUCKET}"
ENV_VARS="${ENV_VARS},GEMINI_MODEL_FLASH=gemini-3-flash-preview"
ENV_VARS="${ENV_VARS},GEMINI_MODEL_PRO=gemini-3-pro-preview"
ENV_VARS="${ENV_VARS},CONFIDENCE_THRESHOLD=0.95"
ENV_VARS="${ENV_VARS},TEMPERATURE=0.0"
# V3.1 True Async Parallelism - main control
ENV_VARS="${ENV_VARS},GEMINI_CONCURRENT_LIMIT=${GEMINI_CONCURRENT_LIMIT}"
# Legacy settings (kept for compatibility but not used in v3.1)
ENV_VARS="${ENV_VARS},API_RPM_LIMIT=${API_RPM_LIMIT}"
ENV_VARS="${ENV_VARS},WORKER_RATE_LIMIT_SECONDS=${WORKER_RATE_LIMIT_SECONDS}"
ENV_VARS="${ENV_VARS},OCR_PARALLEL_WORKERS=${OCR_PARALLEL_WORKERS}"

# Add OpenRouter API key if provided
if [ -n "$OPENROUTER_API_KEY" ]; then
    ENV_VARS="${ENV_VARS},OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
fi

# v3.1: Optimized for True Async Parallelism
# 
# KEY CHANGES:
# - min-instances=0: Save cost when idle (auto-scale from 0)
# - max-instances=100: Handle high burst loads
# - concurrency=20: Each instance handles 20 concurrent requests
#   (with GEMINI_CONCURRENT_LIMIT=10, means ~10 parallel Gemini calls)
# - cpu-boost: Fast startup for auto-scaled instances
#
# Cost optimization:
# - Pay only when processing (min-instances=0)
# - Auto-scale up to 100 instances during high load
# - Each instance can process ~10 images in parallel
#
# Performance:
# - 10 images → 1 instance → ~5-10 seconds (parallel)
# - 100 images → auto-scales to ~10 instances → ~15-20 seconds total
gcloud run deploy "$SERVICE_NAME" \
    --image="$IMAGE_URL" \
    --region="$REGION" \
    --platform=managed \
    --allow-unauthenticated \
    --execution-environment=gen2 \
    --cpu-boost \
    --concurrency=20 \
    --timeout=1000s \
    --memory=4Gi \
    --cpu=4 \
    --min-instances=2 \
    --max-instances=50 \
    --set-env-vars="$ENV_VARS"

# Get the service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format="value(status.url)")

echo -e "${GREEN}✓ Deployed successfully!${NC}"

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Service URL: $SERVICE_URL"
echo ""
echo "Test endpoints:"
echo "  Health:  curl ${SERVICE_URL}/health"
echo "  OCR:     curl -X POST ${SERVICE_URL}/ocr -F 'file=@image.jpg'"
echo ""
echo "C# Integration:"
echo "  POST ${SERVICE_URL}/ocr"
echo "  Content-Type: multipart/form-data"
echo "  Body: file=<image bytes>"
echo ""
