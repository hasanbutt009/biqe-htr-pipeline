# Technology Context

> **Version:** 5.1  
> **Last Updated:** February 19, 2026

---

## Core Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Runtime | Python 3.11+ | Main language |
| Framework | FastAPI | HTTP API |
| AI | Vertex AI | Gemini models |
| Compute | Cloud Run | Serverless hosting |
| State | Firestore | Job tracking |
| Container | Docker | Deployment |
| Parallelism | asyncio | True async processing |

---

## Cloud Run Configuration (v5.1)

| Setting | Value | Notes |
|---------|-------|-------|
| Service Name | biqe-ocr-service | - |
| Region | europe-west4 | GDPR/EU compliance |
| Generation | gen2 | Required for 4 CPU |
| Memory | 4GB | For image processing |
| CPU | 4 vCPU | Parallel processing |
| Timeout | 1100s | Max processing time |
| Concurrency | 20 | Requests per instance |
| Min Instances | 0 | Cost optimization |
| Max Instances | 100 | Handle burst loads |
| CPU Boost | enabled | Fast cold start |
| IAM | allUsers (public) | Open API |

### New in v5.1
| Setting | Value | Purpose |
|---------|-------|---------|
| GEMINI_CONCURRENT_LIMIT | 10 | Max parallel Gemini API calls per instance |
| cpu-boost | enabled | Reduce cold start latency |
| min-instances | 0 | Pay only when processing |

---

## Gemini Models

### Vertex AI (Default, GDPR-compliant)

| Model | Purpose | Speed | Region |
|-------|---------|-------|--------|
| gemini-2.5-flash | Primary OCR | Fast (~5-15s) | europe-west4 |
| gemini-2.5-pro | Fallback/accuracy | Slow (~60-90s) | europe-west4 |

### OpenRouter (Alternative, Global)

| Model | Purpose | Speed | Region |
|-------|---------|-------|--------|
| gemini-3-flash-preview | Primary OCR | Fast | Global |
| gemini-3-pro-preview | Fallback/accuracy | Slow | Global |

### Model Parameters (Defaults)

| Parameter | Value | Description |
|-----------|-------|-------------|
| Temperature | 0.0 | Deterministic (best for OCR) |
| top_p | 1.0 | All tokens considered |
| top_k | 40 | Balanced sampling |

---

## Parallelism Architecture (v5.1)

```python
# Global semaphore for rate limiting
GEMINI_CONCURRENT_LIMIT = int(os.environ.get("GEMINI_CONCURRENT_LIMIT", "10"))
_gemini_semaphore = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)

# In endpoint - TRUE PARALLEL processing
asyncio.create_task(process_image_background_v3(...))

# In background task - rate limited
async with semaphore:
    result = ocr_engine.process_image_bytes(...)
```

**Why asyncio.create_task() instead of BackgroundTasks?**
- BackgroundTasks runs tasks sequentially (one at a time)
- asyncio.create_task() runs tasks truly in parallel
- 10 images now take ~5-10s instead of ~60s

---

## Firestore Schema

```javascript
Collection: jobs
Document: {
  job_id: "uuid",
  filename: "document.jpg",
  status: "processing" | "completed" | "failed",
  
  params: {
    provider: "vertex",
    two_step_ocr: false,
    confidence_threshold: 0.95,
    temperature: 0.0,
    top_p: 1.0,
    top_k: 40
  },
  
  file_size_bytes: 1234567,
  
  // Results (when completed)
  confidence: 0.96,
  model_used: "gemini-2.5-flash",
  text_content: "...",
  
  // Timestamps
  created_at: Timestamp,
  completed_at: Timestamp,
  processing_time_ms: 25000,
  
  // Error (when failed)
  error: "...",
  retry_count: 3
}
```

---

## Removed Components

| Component | Reason | Notes |
|-----------|--------|-------|
| GCS Buckets | Images in memory, faster | Kept gcs_client.py for future |
| Pub/Sub | asyncio.create_task, simpler | Removed pubsub_client.py |
| Workers | Processing in-process | Removed ocr_worker.py |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| GCP_PROJECT_ID | Yes | - | Google Cloud project |
| GCP_REGION | No | europe-west4 | Deployment region |
| OPENROUTER_API_KEY | No | - | For OpenRouter provider |
| GEMINI_CONCURRENT_LIMIT | No | 10 | Max parallel Gemini calls |
| CONFIDENCE_THRESHOLD | No | 0.95 | Default threshold |
| TEMPERATURE | No | 0.0 | Default temperature |

---

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/gcs-batch/process-image` | POST | Submit image for OCR |
| `/gcs-batch/jobs/{job_id}/status` | GET | Get results |
| `/health` | GET | Health check |

---

## Deployment

### Standard Deployment
```bash
cd biqe-htr-pipeline
./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

### With Custom Parallelism
```bash
GEMINI_CONCURRENT_LIMIT=15 ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

### Manual Cloud Run Deploy
```bash
gcloud run deploy biqe-ocr-service \
  --region=europe-west4 \
  --cpu=4 \
  --memory=4Gi \
  --timeout=1100 \
  --concurrency=20 \
  --min-instances=0 \
  --max-instances=100 \
  --cpu-boost \
  --set-env-vars="GEMINI_CONCURRENT_LIMIT=10"
```

---

## Resource Limits

| Resource | Limit | Notes |
|----------|-------|-------|
| Max file size | 20MB | Per image |
| Firestore doc | 1MB | For text_content |
| Cloud Run memory | 4GB | Per instance |
| Cloud Run timeout | 1100s | Max processing time |
| Gemini concurrent | 10 | Per instance (configurable) |
| Cloud Run instances | 100 | Auto-scaling |

---

## Monitoring

### Cloud Run Logs
```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="biqe-ocr-service"' \
  --project=gen-lang-client-0609361296 --limit=50
```

### Check Parallelism
```bash
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"acquired semaphore"' \
  --project=gen-lang-client-0609361296 --limit=20
```

### Service Status
```bash
gcloud run services describe biqe-ocr-service \
  --region europe-west4 \
  --project gen-lang-client-0609361296
```
