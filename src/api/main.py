"""
BIQE HTR Pipeline - FastAPI Middleware Entry Point.

This is a Cloud Run SERVICE that handles OCR processing:
- POST /gcs-batch/process-image - Submit image (returns immediately)
- GET /gcs-batch/jobs/{job_id}/status - Poll for results
- GET /health - Health check

Architecture:
- Image bytes held in Cloud Run memory
- Background task processes with Gemini
- Text results stored in Firestore
- No GCS storage (faster processing)
"""

import logging
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.config import settings


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="BIQE HTR Pipeline - OCR Middleware",
    description="Async OCR service for BIQE HTR Desktop Application. Upload images, get transcriptions.",
    version="3.0.0",
)

# CORS for browser testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include main router (gcs-batch endpoints)
try:
    from src.api.v3_router import router as gcs_batch_router
    app.include_router(gcs_batch_router)
    logger.info("BIQE HTR API router enabled at /gcs-batch")
except ImportError as e:
    logger.error(f"Failed to import router: {e}")
    raise


# =============================================================================
# Health Check
# =============================================================================

class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    timestamp: str
    version: str = "3.0.0"


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint for Cloud Run."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# =============================================================================
# Local Development
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
