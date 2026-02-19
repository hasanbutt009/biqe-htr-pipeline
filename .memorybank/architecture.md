# BIQE HTR Pipeline - Architecture

> **Last Updated:** February 19, 2026  
> **Version:** 5.1 (True Async Parallelism)

---

## Overview

Cloud-based OCR service using Google Gemini AI for historical document transcription. The API uses a simple submit-poll pattern with true async parallelism for high throughput.

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLIENT (C# Desktop App / Test Script)                              │
│  ├── POST image bytes to API                                        │
│  └── Poll for results (2-3 second intervals)                        │
└─────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│  CLOUD RUN (biqe-ocr-service)                                       │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  FastAPI Application                                         │   │
│  │  ├── POST /gcs-batch/process-image  →  Submit image         │   │
│  │  ├── GET /gcs-batch/jobs/{id}/status  →  Get results        │   │
│  │  └── GET /health  →  Health check                           │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  True Async Processing (asyncio.create_task)                 │   │
│  │  ├── Multiple images process IN PARALLEL                     │   │
│  │  ├── Semaphore limits concurrent Gemini calls (10)           │   │
│  │  ├── Image bytes in memory (no GCS)                          │   │
│  │  ├── Call Gemini API (Flash or Pro)                          │   │
│  │  ├── Save text to Firestore                                  │   │
│  │  └── gc.collect() to free memory                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
            │                                           │
            ▼                                           ▼
┌───────────────────────┐                 ┌───────────────────────┐
│  FIRESTORE            │                 │  VERTEX AI            │
│  ├── jobs collection  │                 │  ├── Gemini 2.5 Flash │
│  │   ├── status       │                 │  └── Gemini 2.5 Pro   │
│  │   ├── text_content │                 │                       │
│  │   └── metadata     │                 │  (Or OpenRouter)      │
│  └────────────────────│                 │  ├── Gemini 3 Flash   │
└───────────────────────┘                 │  └── Gemini 3 Pro     │
                                          └───────────────────────┘
```

---

## True Async Parallelism (v5.1)

### Before (v5.0 - Sequential BackgroundTasks)

```
Request 1 arrives → Background task queued → Waits
Request 2 arrives → Background task queued → Waits
Request 3 arrives → Background task queued → Waits

Processing: Job1 (6s) → Job2 (6s) → Job3 (6s) → ...
Total for 10 images: ~60 seconds
```

### After (v5.1 - True Async Parallelism)

```
Request 1 arrives → asyncio.create_task() → Runs immediately
Request 2 arrives → asyncio.create_task() → Runs immediately  
Request 3 arrives → asyncio.create_task() → Runs immediately

Processing: [Job1, Job2, Job3...] all in parallel
            Semaphore limits to 10 concurrent Gemini calls
Total for 10 images: ~5-10 seconds
```

---

## Processing Flow

### 1. Submit Image (POST /gcs-batch/process-image)

```
Client                    API                         Firestore
   │                        │                             │
   │── POST image bytes ───▶│                             │
   │                        │── Create job ──────────────▶│
   │                        │    (status: processing)     │
   │                        │                             │
   │                        │── asyncio.create_task()     │
   │                        │   (parallel processing)     │
   │                        │                             │
   │◀── Return job_id ──────│   (~100ms response time)   │
```

### 2. Background Processing (Parallel)

```
Background Task           Semaphore               Gemini API            Firestore
   │                          │                      │                       │
   │── Acquire semaphore ────▶│                      │                       │
   │   (wait if >10 active)   │                      │                       │
   │                          │                      │                       │
   │── Send image bytes ─────────────────────────────▶│                       │
   │                          │                      │── Process OCR         │
   │◀── Return text ───────────────────────────────────│   (5-90 seconds)     │
   │                          │                      │                       │
   │── Release semaphore ────▶│                      │                       │
   │                          │                      │                       │
   │── Update job ──────────────────────────────────────────────────────────▶│
   │    (status: completed, text_content: "...")     │                       │
   │                          │                      │                       │
   │── gc.collect()           │                      │                       │
```

### 3. Poll Results (GET /gcs-batch/jobs/{id}/status)

```
Client                    API                         Firestore
   │                        │                             │
   │── GET status ─────────▶│                             │
   │                        │── Query job ───────────────▶│
   │                        │◀── Return job data ─────────│
   │◀── Return response ────│                             │
   │    (status, text_content, confidence, etc.)         │
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Compute | Cloud Run (auto-scaling) | Serverless, 0-100 instances |
| Parallelism | asyncio.create_task() + semaphore | True parallel processing |
| Queue | ~~Pub/Sub~~ ~~BackgroundTasks~~ asyncio | Fastest, no timeout issues |
| Storage | ~~GCS~~ In-memory | Faster, simpler |
| State | Firestore | Real-time job tracking |
| Rate Limit | Semaphore (10 concurrent) | Prevent Gemini API limits |
| Models | Gemini 2.5 Flash + Pro | Flash for speed, Pro for accuracy |
| Region | europe-west4 | GDPR compliance |

---

## Cloud Run Configuration (v5.1)

```yaml
service: biqe-ocr-service
region: europe-west4

resources:
  memory: 4Gi
  cpu: 4

scaling:
  min_instances: 0        # Cost optimization
  max_instances: 100      # Handle burst loads
  concurrency: 20         # Requests per instance

timeout: 1100s
cpu_boost: true           # Fast cold start

environment:
  GEMINI_CONCURRENT_LIMIT: 10  # Parallel Gemini calls per instance
```

---

## Two-Step OCR Flow

When `two_step_ocr=true`:

```
┌─────────────────────────────────────────────────────────────────┐
│  Step 1: Flash Model (fast, ~5-15s)                              │
│  ├── Process image                                               │
│  ├── Get confidence score                                        │
│  └── If confidence >= threshold → Done                           │
└─────────────────────────────────────────────────────────────────┘
                               │
                    If confidence < threshold
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 2: Pro Model (slower, ~60-90s)                             │
│  ├── Send Flash result as "ground truth hint"                    │
│  ├── Pro model corrects/improves                                 │
│  └── Return improved transcription                               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Firestore Schema

```javascript
// Collection: jobs
{
  job_id: "abc123-def456-...",
  filename: "document.tif",
  status: "processing" | "completed" | "failed",
  
  // Processing params
  params: {
    provider: "vertex" | "openrouter",
    two_step_ocr: true | false,
    confidence_threshold: 0.95,
    temperature: 0.0,
    top_p: 1.0,
    top_k: 40
  },
  
  // File info
  file_size_bytes: 1234567,
  
  // Results (when completed)
  confidence: 0.96,
  model_used: "gemini-2.5-flash",
  text_content: "Full transcription text...",
  
  // Timestamps
  created_at: Timestamp,
  completed_at: Timestamp,
  processing_time_ms: 25000,
  
  // Error (when failed)
  error: "Error message...",
  retry_count: 3
}
```

---

## Source Files

| File | Purpose |
|------|---------|
| [`src/api/main.py`](../src/api/main.py) | FastAPI app, health endpoint |
| [`src/api/v3_router.py`](../src/api/v3_router.py) | **Main API** - asyncio.create_task + semaphore |
| [`src/core/ocr_engine.py`](../src/core/ocr_engine.py) | Gemini API integration |
| [`src/core/firestore_client.py`](../src/core/firestore_client.py) | Job state management |
| [`test_api.py`](../test_api.py) | Test script |
| [`deploy/deploy.sh`](../deploy/deploy.sh) | Deployment script |
| [`deploy/Dockerfile`](../deploy/Dockerfile) | Container definition |

---

## Key Code: v3_router.py

```python
# Global semaphore for rate limiting
GEMINI_CONCURRENT_LIMIT = int(os.environ.get("GEMINI_CONCURRENT_LIMIT", "10"))
_gemini_semaphore: Optional[asyncio.Semaphore] = None

def get_gemini_semaphore() -> asyncio.Semaphore:
    global _gemini_semaphore
    if _gemini_semaphore is None:
        _gemini_semaphore = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)
    return _gemini_semaphore

# In endpoint:
@router.post("/process-image")
async def process_image_v3(...):
    # Create job in Firestore
    firestore.create_job(job_id, ...)
    
    # TRUE PARALLEL: asyncio.create_task (not BackgroundTasks)
    asyncio.create_task(
        process_image_background_v3(job_id, filename, image_data, ...)
    )
    
    return ProcessImageResponse(job_id=job_id, ...)

# In background task:
async def process_image_background_v3(...):
    semaphore = get_gemini_semaphore()
    
    # Rate limit Gemini API calls
    async with semaphore:
        result = ocr_engine.process_image_bytes(...)
    
    # Update Firestore
    firestore.update_job_result(job_id, text_content=result.text_content, ...)
```

---

## What's NOT Used (Kept for Future)

| Component | Status | Notes |
|-----------|--------|-------|
| GCS Storage | Not used | Images processed in memory |
| Pub/Sub | Not used | asyncio.create_task instead |
| gcs_client.py | Kept | May use for batch uploads later |
| storage.py | Kept | May use for file persistence |
| notifier.py | Kept | May use for webhooks |
