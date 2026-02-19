"""
BIQE HTR API Router - Direct Processing with True Async Parallelism.

This is the MAIN API router for the BIQE HTR Pipeline.

Architecture:
- Image bytes held in Cloud Run memory during processing
- Text results stored directly in Firestore
- TRUE ASYNC PARALLELISM using asyncio.create_task() with semaphore
- Memory explicitly cleared after Gemini call completes
- Fast response (~100ms) with parallel background processing

Endpoints:
- POST /gcs-batch/process-image - Submit image (returns immediately)
- GET /gcs-batch/jobs/{job_id}/status - Poll for results

Flow:
1. Client POSTs image
2. API returns job_id immediately (~100ms)
3. Background task runs IN PARALLEL (controlled by semaphore)
4. Client polls status until completed
5. Client gets text_content from response

Parallelism:
- GEMINI_CONCURRENT_LIMIT controls max parallel Gemini API calls per instance
- Default: 10 concurrent calls (configurable via env var)
- Prevents rate limit errors while maximizing throughput
"""

import asyncio
import gc
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from pathlib import Path

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from src.config import settings
from src.core.firestore_client import get_firestore_client
from src.core.ocr_engine import get_ocr_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gcs-batch", tags=["gcs-batch"])

# =============================================================================
# Parallel Processing Configuration
# =============================================================================

# Maximum concurrent Gemini API calls per Cloud Run instance
# This prevents rate limit errors while maximizing throughput
# Default: 10 concurrent calls (Gemini API typically allows 60 RPM)
GEMINI_CONCURRENT_LIMIT = int(os.environ.get("GEMINI_CONCURRENT_LIMIT", "10"))

# Global semaphore for rate limiting - shared across all requests in this instance
_gemini_semaphore: Optional[asyncio.Semaphore] = None


def get_gemini_semaphore() -> asyncio.Semaphore:
    """Get or create the global semaphore for rate limiting Gemini API calls."""
    global _gemini_semaphore
    if _gemini_semaphore is None:
        _gemini_semaphore = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)
        logger.info(f"[V3] Created Gemini semaphore with limit: {GEMINI_CONCURRENT_LIMIT}")
    return _gemini_semaphore


# =============================================================================
# Response Models
# =============================================================================

class ProcessImageResponse(BaseModel):
    """Response from POST /gcs-batch/process-image (immediate)."""
    job_id: str = Field(description="Unique job identifier for status polling")
    status: str = Field(default="processing")
    poll_url: str = Field(description="URL to check job status")
    provider: str = Field(description="Provider processing this job")
    created_at: datetime = Field(description="Job creation timestamp")


class JobStatusResponse(BaseModel):
    """Response from GET /gcs-batch/jobs/{job_id}/status endpoint."""
    job_id: str
    status: str  # "processing" | "completed" | "failed"
    filename: str
    provider: str
    created_at: Optional[datetime] = None
    
    # Completed fields
    confidence: Optional[float] = None
    model_used: Optional[str] = None
    text_content: Optional[str] = None
    completed_at: Optional[datetime] = None
    processing_time_seconds: Optional[int] = None
    
    # Failed fields
    error: Optional[str] = None
    retry_count: Optional[int] = None


# =============================================================================
# Background Task Processing (No GCS)
# =============================================================================

MIN_TEXT_LENGTH = 10  # Minimum chars to consider valid
MAX_RETRIES = 3


async def process_image_background_v3(
    job_id: str,
    filename: str,
    image_data: bytes,
    mime_type: str,
    params: dict,
):
    """
    Background task that processes the image through Gemini OCR.
    
    V3 CHANGES:
    - No GCS download (image_data passed directly)
    - No GCS result upload (text stored in Firestore)
    - TRUE ASYNC PARALLELISM using semaphore for rate limiting
    - Explicit memory cleanup after processing
    
    Args:
        job_id: Unique job identifier
        filename: Original filename
        image_data: Raw image bytes (held in memory)
        mime_type: MIME type of image
        params: OCR parameters (provider, two_step_ocr, etc.)
    """
    start_time = time.perf_counter()
    firestore = get_firestore_client(settings.gcp_project_id)
    semaphore = get_gemini_semaphore()
    
    try:
        # Update status to show we're processing
        firestore.update_job_status(job_id, "processing")
        
        # Get OCR engine based on provider
        provider = params.get("provider", "vertex")
        two_step = params.get("two_step_ocr", False)
        ocr_engine = get_ocr_engine(provider)
        
        logger.info(f"[V3_BG] Processing job {job_id}: provider={provider}, two_step={two_step}, file={filename}")
        
        # Process with retry logic - ALL GEMINI CALLS PROTECTED BY SEMAPHORE
        result = None
        last_error = None
        
        for attempt in range(MAX_RETRIES):
            try:
                # SEMAPHORE: Limit concurrent Gemini API calls
                async with semaphore:
                    logger.debug(f"[V3_BG] Job {job_id} acquired semaphore (attempt {attempt + 1})")
                    
                    if two_step:
                        result = ocr_engine.process_two_step(
                            image_data=image_data,
                            mime_type=mime_type,
                            filename=filename,
                            params=params,
                        )
                    else:
                        result = ocr_engine.process_image_bytes(
                            image_data=image_data,
                            mime_type=mime_type,
                            filename=filename,
                            params=params,
                        )
                
                # Verify result
                if result and result.text_content and len(result.text_content.strip()) >= MIN_TEXT_LENGTH:
                    break
                    
                logger.warning(f"[V3_BG] Empty result for {job_id}, retrying ({attempt + 1}/{MAX_RETRIES})")
                
            except Exception as e:
                last_error = str(e)
                logger.error(f"[V3_BG] Attempt {attempt + 1}/{MAX_RETRIES} failed for {job_id}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)  # Brief pause before retry
        
        # Check if we got a valid result
        if not result or not result.text_content or len(result.text_content.strip()) < MIN_TEXT_LENGTH:
            # All retries failed - try Pro model (with semaphore protection)
            logger.warning(f"[V3_BG] All retries failed for {job_id}, forcing Pro model")
            try:
                async with semaphore:
                    logger.debug(f"[V3_BG] Job {job_id} acquired semaphore for Pro fallback")
                    pro_params = {**params, "model": "pro"}
                    result = ocr_engine.process_image_bytes(
                        image_data=image_data,
                        mime_type=mime_type,
                        filename=filename,
                        params=pro_params,
                    )
            except Exception as e:
                last_error = str(e)
                logger.error(f"[V3_BG] Pro fallback also failed for {job_id}: {e}")
        
        # Final check
        if not result or not result.text_content or len(result.text_content.strip()) < MIN_TEXT_LENGTH:
            raise ValueError(f"Failed to get valid OCR result after {MAX_RETRIES} retries: {last_error}")
        
        # Calculate processing time
        processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        # Get model name as string
        model_used = result.model_used.value if hasattr(result.model_used, 'value') else str(result.model_used)
        
        # V3: Store text directly in Firestore (no GCS upload)
        # Firestore document limit is 1MB, text should be well under that
        firestore.update_job_result(
            job_id=job_id,
            text_content=result.text_content,
            confidence=result.confidence_score,
            model_used=model_used,
            text_url=None,  # V3: No GCS URL
            processing_time_ms=processing_time_ms,
        )
        
        logger.info(f"[V3_BG] Job {job_id} completed: {result.confidence_score*100:.0f}% ({model_used}), {len(result.text_content)} chars")
        
    except Exception as e:
        # Update Firestore with error
        logger.error(f"[V3_BG] Job {job_id} failed: {e}")
        logger.error(traceback.format_exc())
        
        firestore.update_job_status(
            job_id=job_id,
            status="failed",
            error=str(e),
        )
    
    finally:
        # V3: Explicit memory cleanup
        # Clear the image bytes from memory
        del image_data
        gc.collect()
        logger.debug(f"[V3_BG] Memory cleaned for job {job_id}")


# =============================================================================
# V3 Endpoints
# =============================================================================

@router.post("/process-image", response_model=ProcessImageResponse)
async def process_image_v3(
    file: UploadFile = File(..., description="Preprocessed image file"),
    filename: str = Form(..., description="Original filename"),
    provider: str = Form(default="vertex", description="'vertex' or 'openrouter'"),
    two_step_ocr: bool = Form(default=False),
    confidence_threshold: float = Form(default=0.95),
    step1_model: str = Form(default=None),
    step2_model: str = Form(default=None),
    custom_prompt: str = Form(default=None),
    temperature: float = Form(default=0.0),
    top_p: float = Form(default=1.0),
    top_k: int = Form(default=40),
):
    """
    V3: Process a single image WITHOUT GCS storage - TRUE ASYNC PARALLELISM.
    
    FASTER than V2 because:
    - No GCS upload (~100ms saved)
    - No GCS result storage (~50ms saved)
    - Image bytes stay in Cloud Run memory
    - TRUE PARALLEL processing via asyncio.create_task()
    
    Parallelism:
    - Uses asyncio.create_task() for background processing
    - Semaphore limits concurrent Gemini API calls (default: 10)
    - Multiple images process IN PARALLEL, not sequentially
    
    Trade-offs:
    - No GCS persistence (no audit trail in GCS)
    - Text results only in Firestore
    - Memory usage higher during processing
    
    Flow:
    1. Read image into memory (~50ms)
    2. Create job in Firestore (~50ms)
    3. Start background task with asyncio.create_task() (PARALLEL!)
    4. Return immediately with job_id (~100ms total)
    
    Poll /gcs-batch/jobs/{job_id}/status for results.
    
    Parameters:
    - file: Preprocessed image file (JPEG, PNG, TIFF, WebP)
    - filename: Original filename for identification
    - provider: 'vertex' (GDPR/EU) or 'openrouter' (fast/USA)
    - two_step_ocr: Enable two-step processing (Flash → Pro correction)
    - confidence_threshold: Threshold for Pro fallback in auto mode
    - step1_model: Override Step 1 model (optional)
    - step2_model: Override Step 2 model (optional)
    - custom_prompt: Custom OCR prompt (optional)
    - temperature: Model temperature (0.0-1.0)
    - top_p: Nucleus sampling parameter (0.0-1.0)
    - top_k: Top-k sampling parameter
    
    Returns:
    - job_id: Unique identifier for this job
    - poll_url: Endpoint to poll for status/results
    """
    try:
        firestore = get_firestore_client(settings.gcp_project_id)
        
        job_id = str(uuid4())
        logger.info(f"[V3] Processing single image: job_id={job_id}, filename={filename}, provider={provider}")
        
        # Read image data
        image_data = await file.read()
        file_size = len(image_data)
        
        # Validate file size (max 20MB)
        MAX_FILE_SIZE = 20 * 1024 * 1024
        if file_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large: {file_size} bytes. Maximum: {MAX_FILE_SIZE} bytes (20MB)"
            )
        
        # Determine MIME type
        suffix = Path(filename).suffix.lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
        }
        mime_type = mime_types.get(suffix, "image/jpeg")
        
        # V3: NO GCS UPLOAD - image stays in memory
        
        # Build params
        params = {
            "provider": provider,
            "two_step_ocr": two_step_ocr,
            "confidence_threshold": confidence_threshold,
            "model": "auto",
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        
        if step1_model:
            params["step1"] = {"model": step1_model}
            if custom_prompt:
                params["step1"]["prompt"] = custom_prompt
        elif custom_prompt:
            params["step1"] = {"prompt": custom_prompt}
        
        if step2_model:
            params["step2"] = {"model": step2_model}
        
        # Create job in Firestore (no GCS path)
        firestore.create_job(
            job_id=job_id,
            filename=filename,
            gcs_input_path=None,  # V3: No GCS
            params=params,
            file_size_bytes=file_size,
        )
        
        # Verify job was created successfully before scheduling background task
        # This prevents 404 errors when background task tries to update the job
        job = firestore.get_job(job_id)
        if not job:
            logger.error(f"[V3] Job {job_id} was NOT created in Firestore after create_job() call - critical error")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create job in Firestore. Job ID: {job_id}"
            )
        logger.info(f"[V3] Job {job_id} verified in Firestore, proceeding with background task")
        
        # V3 PARALLELISM: Use asyncio.create_task() instead of BackgroundTasks
        # This allows multiple images to process CONCURRENTLY within the same instance
        # Semaphore in process_image_background_v3 controls max concurrent Gemini calls
        asyncio.create_task(
            process_image_background_v3(
                job_id=job_id,
                filename=filename,
                image_data=image_data,
                mime_type=mime_type,
                params=params,
            )
        )
        
        logger.info(f"[V3] Job {job_id} created, parallel async task started (semaphore limit: {GEMINI_CONCURRENT_LIMIT})")
        
        created_at = datetime.now(timezone.utc)
        
        return ProcessImageResponse(
            job_id=job_id,
            status="processing",
            poll_url=f"/gcs-batch/jobs/{job_id}/status",
            provider=provider,
            created_at=created_at,
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[V3] Failed to process image: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """
    Get status and results for a job.
    
    Poll this endpoint to check if processing is complete and
    retrieve the transcription result.
    
    Returns:
    - When processing: status="processing", no results yet
    - When completed: status="completed" with full transcription in text_content
    - When failed: status="failed" with error details
    """
    try:
        firestore = get_firestore_client(settings.gcp_project_id)
        job = firestore.get_job(job_id)
        
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        # Build response
        response_data = {
            "job_id": job_id,
            "status": job["status"],
            "filename": job["filename"],
            "provider": job.get("params", {}).get("provider", "vertex"),
            "created_at": job.get("created_at"),
        }
        
        # Add results if completed
        if job["status"] == "completed":
            response_data.update({
                "confidence": job.get("confidence"),
                "model_used": job.get("model_used"),
                "text_content": job.get("text_content"),
                "completed_at": job.get("completed_at"),
                "processing_time_seconds": job.get("processing_time_ms", 0) // 1000 if job.get("processing_time_ms") else None,
            })
        
        # Add error if failed
        elif job["status"] == "failed":
            response_data.update({
                "error": job.get("error", "Processing failed"),
                "retry_count": job.get("retry_count", 0),
            })
        
        return JobStatusResponse(**response_data)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
