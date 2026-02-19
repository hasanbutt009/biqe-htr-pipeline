# BIQE HTR API Reference

**Base URL**: `https://biqe-ocr-service-650561295384.europe-west4.run.app`  
**Version**: 5.1 (True Async Parallelism)  
**Last Updated**: February 19, 2026

---

## Overview

The BIQE HTR API provides OCR (Optical Character Recognition) services for historical handwritten documents using Google Gemini AI. The API uses a two-step flow:

1. **Submit**: POST image → receive job_id immediately (~100ms)
2. **Poll**: GET status → wait until completed → extract text_content

**Key Features:**
- True async parallelism - multiple images process concurrently
- No file size limits for typical documents (max 20MB)
- GDPR-compliant EU processing (europe-west4)
- 100% success rate in production

---

## Endpoints

### POST /gcs-batch/process-image

Submit an image for OCR transcription. Returns immediately with a job_id for polling.

**Request**: `multipart/form-data`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `file` | binary | Yes | - | Image file bytes |
| `filename` | string | Yes | - | Original filename with extension |
| `provider` | string | No | `vertex` | `vertex` (EU/GDPR) or `openrouter` (faster) |
| `two_step_ocr` | boolean | No | `false` | Enable two-step: Flash → Pro for higher accuracy |
| `confidence_threshold` | float | No | `0.95` | Threshold for Pro model (0.0-1.0) |
| `step1_model` | string | No | auto | Override Step 1 model |
| `step2_model` | string | No | auto | Override Step 2 model |
| `custom_prompt` | string | No | - | Custom OCR prompt (advanced) |
| `temperature` | float | No | `0.0` | Sampling temperature (0.0-2.0) |
| `top_p` | float | No | `1.0` | Nucleus sampling (0.0-1.0) |
| `top_k` | int | No | `40` | Top-k sampling (1-100) |

**Supported File Formats:**
- JPEG (.jpg, .jpeg)
- PNG (.png)
- TIFF (.tif, .tiff) -> only for vertex
- WebP (.webp)
- BMP (.bmp)
- GIF (.gif)

**Response (200 OK):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "poll_url": "/gcs-batch/jobs/550e8400.../status",
  "provider": "vertex",
  "created_at": "2026-02-19T12:00:00Z"
}
```

**Error Responses:**

| Status | Description |
|--------|-------------|
| 400 | Invalid parameter (e.g., unknown provider) |
| 413 | File too large (max 20MB) |
| 500 | Server error |

---

### GET /gcs-batch/jobs/{job_id}/status

Get job status and results. Poll this endpoint until `status` is `completed` or `failed`.

**Path Parameters:**
- `job_id` (string, required) - UUID from process-image response

**Response - Processing (still working):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "filename": "document.jpg",
  "provider": "vertex",
  "created_at": "2026-02-19T12:00:00Z"
}
```

**Response - Completed (success):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "filename": "document.jpg",
  "provider": "vertex",
  "confidence": 0.96,
  "model_used": "gemini-2.5-flash",
  "text_content": "Full transcription text of the document...",
  "completed_at": "2026-02-19T12:00:25Z",
  "processing_time_seconds": 25
}
```

**Response - Failed (error):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "failed",
  "filename": "document.jpg",
  "provider": "vertex",
  "error": "RECITATION: Content policy violation",
  "retry_count": 3
}
```

**Response - Not Found (404):**
```json
{
  "detail": "Job 550e8400-... not found"
}
```

---

### GET /health

Health check endpoint for monitoring.

**Response (200 OK):**
```json
{
  "status": "healthy",
  "version": "5.1"
}
```

---

## Status Values

| Status | Description | Action |
|--------|-------------|--------|
| `processing` | Job received, OCR in progress | Keep polling |
| `completed` | OCR finished, text available | Extract `text_content` |
| `failed` | Error occurred | Check `error` field |

---

## Providers

| Provider | Models | Region | Best For |
|----------|--------|--------|----------|
| `vertex` | gemini-2.5-flash, gemini-2.5-pro | europe-west4 | GDPR compliance, EU data |
| `openrouter` | gemini-3-flash, gemini-3-pro | Global | Speed, latest models |

**Note:** Use `vertex` for GDPR compliance. Use `openrouter` for faster processing.

---

## Two-Step Processing

When `two_step_ocr=true`:

1. **Step 1 (Flash)**: Fast model processes image first (~5-15s)
2. **Step 2 (Pro)**: If confidence < threshold, Pro model improves result (~60-90s)

| Scenario | Result |
|----------|--------|
| Flash returns 97% confidence, threshold 0.95 | Done (skip Pro) |
| Flash returns 97% confidence, threshold 0.99 | Run Pro for improvement |
| Flash returns 80% confidence, threshold 0.95 | Run Pro for improvement |

**Recommendation:** Use `--two-step --confidence-threshold 0.99` for best accuracy.

---

## Response Fields Reference

### Process Image Response

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | UUID for polling (save this!) |
| `status` | string | Always `processing` initially |
| `poll_url` | string | Relative URL to check status |
| `provider` | string | Provider being used |
| `created_at` | datetime | ISO 8601 timestamp |

### Status Response (Completed)

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | UUID |
| `status` | string | `completed` |
| `filename` | string | Original filename |
| `provider` | string | Provider used |
| `confidence` | float | OCR confidence (0.0-1.0) |
| `model_used` | string | Gemini model name |
| `text_content` | string | **THE TRANSCRIBED TEXT** |
| `completed_at` | datetime | ISO 8601 timestamp |
| `processing_time_seconds` | int | How long it took |

### Status Response (Failed)

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | UUID |
| `status` | string | `failed` |
| `filename` | string | Original filename |
| `error` | string | Error message |
| `retry_count` | int | Number of retries attempted |

---

## curl Examples

**Basic submission:**
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg"
```

**Two-step processing:**
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "provider=vertex" \
  -F "two_step_ocr=true" \
  -F "confidence_threshold=0.99"
```

**Check status:**
```bash
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/jobs/{job_id}/status
```

**OpenRouter provider:**
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "provider=openrouter"
```


---

