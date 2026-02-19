#!/bin/bash
# =============================================================================
# BIQE HTR Pipeline - Complete Infrastructure Setup
# =============================================================================
# This script sets up ALL GCP resources required for the HTR pipeline.
# Single source of truth for infrastructure.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Project owner or editor permissions
#
# Usage:
#   ./setup-infra.sh <PROJECT_ID> <REGION>
#
# Example:
#   ./setup-infra.sh gen-lang-client-0609361296 europe-west4
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
PROJECT_ID="${1:-gen-lang-client-0609361296}"
REGION="${2:-europe-west4}"

# Resource names
INPUT_BUCKET="${PROJECT_ID}-htr-input"
OUTPUT_BUCKET="${PROJECT_ID}-htr-output"
OCR_JOBS_TOPIC="ocr-jobs-topic"
OCR_JOBS_SUBSCRIPTION="ocr-jobs-subscription"
WORKER_SA="biqe-htr-worker"
CLIENT_SA="biqe-htr-client"
ARTIFACT_REPO="biqe-htr"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} BIQE HTR Pipeline - Infrastructure Setup${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Project ID: $PROJECT_ID"
echo "Region: $REGION"
echo ""

# -----------------------------------------------------------------------------
# Step 1: Set project and enable APIs
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[1/10] Setting project and enabling APIs...${NC}"

gcloud config set project "$PROJECT_ID"

gcloud services enable \
    run.googleapis.com \
    storage.googleapis.com \
    pubsub.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    firestore.googleapis.com \
    generativelanguage.googleapis.com \
    --quiet

echo -e "${GREEN}✓ APIs enabled${NC}"

# -----------------------------------------------------------------------------
# Step 2: Create Cloud Storage buckets
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[2/10] Creating Cloud Storage buckets...${NC}"

# Input bucket
if ! gsutil ls "gs://$INPUT_BUCKET" &>/dev/null; then
    gsutil mb -l "$REGION" "gs://$INPUT_BUCKET"
    gsutil lifecycle set /dev/stdin "gs://$INPUT_BUCKET" <<EOF
{
  "rule": [{
    "action": {"type": "Delete"},
    "condition": {"age": 7}
  }]
}
EOF
    echo "Created: gs://$INPUT_BUCKET (7-day lifecycle)"
else
    echo "Exists: gs://$INPUT_BUCKET"
fi

# Output bucket
if ! gsutil ls "gs://$OUTPUT_BUCKET" &>/dev/null; then
    gsutil mb -l "$REGION" "gs://$OUTPUT_BUCKET"
    gsutil lifecycle set /dev/stdin "gs://$OUTPUT_BUCKET" <<EOF
{
  "rule": [{
    "action": {"type": "Delete"},
    "condition": {"age": 30}
  }]
}
EOF
    echo "Created: gs://$OUTPUT_BUCKET (30-day lifecycle)"
else
    echo "Exists: gs://$OUTPUT_BUCKET"
fi

echo -e "${GREEN}✓ Buckets created${NC}"

# -----------------------------------------------------------------------------
# Step 3: Create Pub/Sub topics and subscriptions
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[3/10] Creating Pub/Sub topic and subscription...${NC}"

# OCR Jobs topic (for batch processing)
if ! gcloud pubsub topics describe "$OCR_JOBS_TOPIC" &>/dev/null 2>&1; then
    gcloud pubsub topics create "$OCR_JOBS_TOPIC"
    echo "Created topic: $OCR_JOBS_TOPIC"
else
    echo "Exists: $OCR_JOBS_TOPIC"
fi

# OCR Jobs subscription (600s ack deadline for long processing)
if ! gcloud pubsub subscriptions describe "$OCR_JOBS_SUBSCRIPTION" &>/dev/null 2>&1; then
    gcloud pubsub subscriptions create "$OCR_JOBS_SUBSCRIPTION" \
        --topic="$OCR_JOBS_TOPIC" \
        --ack-deadline=600 \
        --message-retention-duration=7d
    echo "Created subscription: $OCR_JOBS_SUBSCRIPTION"
else
    echo "Exists: $OCR_JOBS_SUBSCRIPTION"
fi

echo -e "${GREEN}✓ Pub/Sub configured${NC}"

# -----------------------------------------------------------------------------
# Step 4: Create Firestore Database
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[4/10] Creating Firestore database...${NC}"

if gcloud firestore databases describe --project="$PROJECT_ID" &>/dev/null 2>&1; then
    echo "Firestore database already exists"
else
    gcloud firestore databases create \
        --location="$REGION" \
        --project="$PROJECT_ID" || echo "Firestore may need IAM permission - check with project owner"
fi

echo -e "${GREEN}✓ Firestore configured${NC}"

# -----------------------------------------------------------------------------
# Step 5: Create Artifact Registry repository
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[5/10] Creating Artifact Registry repository...${NC}"

if ! gcloud artifacts repositories describe "$ARTIFACT_REPO" --location="$REGION" &>/dev/null 2>&1; then
    gcloud artifacts repositories create "$ARTIFACT_REPO" \
        --repository-format=docker \
        --location="$REGION" \
        --description="BIQE HTR Pipeline container images"
    echo "Created repository: $ARTIFACT_REPO"
else
    echo "Exists: $ARTIFACT_REPO"
fi

echo -e "${GREEN}✓ Artifact Registry configured${NC}"

# -----------------------------------------------------------------------------
# Step 6: Create Service Accounts
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[6/10] Creating Service Accounts...${NC}"

# Worker service account (used by Cloud Run)
WORKER_EMAIL="${WORKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$WORKER_EMAIL" &>/dev/null 2>&1; then
    gcloud iam service-accounts create "$WORKER_SA" \
        --display-name="BIQE HTR Worker"
    echo "Created: $WORKER_EMAIL"
else
    echo "Exists: $WORKER_EMAIL"
fi

# Client service account (used by C# application)
CLIENT_EMAIL="${CLIENT_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$CLIENT_EMAIL" &>/dev/null 2>&1; then
    gcloud iam service-accounts create "$CLIENT_SA" \
        --display-name="BIQE HTR Client (C# App)"
    echo "Created: $CLIENT_EMAIL"
else
    echo "Exists: $CLIENT_EMAIL"
fi

echo -e "${GREEN}✓ Service Accounts created${NC}"

# -----------------------------------------------------------------------------
# Step 7: Assign IAM roles to Worker SA
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[7/10] Assigning IAM roles to Worker SA...${NC}"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$WORKER_EMAIL" \
    --role="roles/storage.admin" \
    --condition=None --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$WORKER_EMAIL" \
    --role="roles/pubsub.publisher" \
    --condition=None --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$WORKER_EMAIL" \
    --role="roles/pubsub.subscriber" \
    --condition=None --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$WORKER_EMAIL" \
    --role="roles/aiplatform.user" \
    --condition=None --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$WORKER_EMAIL" \
    --role="roles/datastore.user" \
    --condition=None --quiet

# Service Account Token Creator - required for generating signed URLs on Cloud Run
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$WORKER_EMAIL" \
    --role="roles/iam.serviceAccountTokenCreator" \
    --condition=None --quiet

echo "Worker SA: storage.admin, pubsub.publisher, pubsub.subscriber, aiplatform.user, datastore.user, iam.serviceAccountTokenCreator"

echo -e "${GREEN}✓ Worker IAM roles assigned${NC}"

# -----------------------------------------------------------------------------
# Step 8: Assign IAM roles to Client SA
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[8/10] Assigning IAM roles to Client SA...${NC}"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$CLIENT_EMAIL" \
    --role="roles/storage.objectCreator" \
    --condition=None --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$CLIENT_EMAIL" \
    --role="roles/run.invoker" \
    --condition=None --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$CLIENT_EMAIL" \
    --role="roles/pubsub.subscriber" \
    --condition=None --quiet

echo "Client SA: storage.objectCreator, run.invoker, pubsub.subscriber"

echo -e "${GREEN}✓ Client IAM roles assigned${NC}"

# -----------------------------------------------------------------------------
# Step 9: Create Cloud Run Job placeholder
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[9/10] Cloud Run Job placeholder...${NC}"

JOB_NAME="biqe-htr-job"
if ! gcloud run jobs describe "$JOB_NAME" --region="$REGION" &>/dev/null 2>&1; then
    echo "Job will be created on first deployment via Cloud Build"
else
    echo "Exists: $JOB_NAME"
fi

echo -e "${GREEN}✓ Cloud Run Job configured${NC}"

# -----------------------------------------------------------------------------
# Step 10: Generate client service account key
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[10/10] Generating client service account key...${NC}"

KEY_FILE="client-sa-key.json"
if [ ! -f "$KEY_FILE" ]; then
    gcloud iam service-accounts keys create "$KEY_FILE" \
        --iam-account="$CLIENT_EMAIL"
    echo "Created: $KEY_FILE (KEEP SECURE!)"
else
    echo "Key file already exists: $KEY_FILE"
fi

echo -e "${GREEN}✓ Client key generated${NC}"

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Resources created:"
echo "  • Input Bucket:       gs://$INPUT_BUCKET"
echo "  • Output Bucket:      gs://$OUTPUT_BUCKET"
echo "  • OCR Jobs Topic:     $OCR_JOBS_TOPIC"
echo "  • OCR Subscription:   $OCR_JOBS_SUBSCRIPTION"
echo "  • Firestore:          $REGION"
echo "  • Artifact Registry:  $ARTIFACT_REPO"
echo "  • Worker SA:          $WORKER_EMAIL"
echo "  • Client SA:          $CLIENT_EMAIL"
echo ""
echo "Next steps:"
echo "  1. Deploy: ./deploy/deploy.sh $PROJECT_ID $REGION"
echo "  2. Share $KEY_FILE with C# developer (securely!)"
echo ""
echo "Environment variables for .env:"
echo "  GCP_PROJECT_ID=$PROJECT_ID"
echo "  GCP_REGION=$REGION"
echo "  GCS_INPUT_BUCKET=$INPUT_BUCKET"
echo "  GCS_OUTPUT_BUCKET=$OUTPUT_BUCKET"
echo ""
