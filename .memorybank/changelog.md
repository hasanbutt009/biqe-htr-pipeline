# BIQE HTR Pipeline - Changelog

---

## v5.1 (February 19, 2026) - Current

**True Async Parallelism**

### Changes
- Replaced `BackgroundTasks.add_task()` with `asyncio.create_task()` for true parallel processing
- Added global semaphore to limit concurrent Gemini API calls (default: 10)
- Added `GEMINI_CONCURRENT_LIMIT` environment variable
- Updated Cloud Run configuration:
  - min-instances: 0 (cost optimization)
  - max-instances: 100 (handle burst loads)
  - concurrency: 20 per instance
  - cpu-boost: enabled (fast cold start)

### Performance Improvement
| Scenario | Before (v5.0 Sequential) | After (v5.1 Parallel) |
|----------|--------------------------|----------------------|
| 10 images | ~60 seconds | ~5-10 seconds |
| 50 images | ~300 seconds | ~25-50 seconds |
| 100 images | ~600 seconds | ~50-100 seconds |

### Files Modified
- `src/api/v3_router.py` - Added semaphore and asyncio.create_task()
- `deploy/deploy.sh` - Updated Cloud Run configuration

---

## v5.0 (February 18, 2026)

**Major Simplification - No GCS, No Pub/Sub**

### Changes
- Removed GCS storage - images processed in Cloud Run memory
- Removed Pub/Sub - using FastAPI BackgroundTasks
- Single endpoint: `/gcs-batch/process-image`
- Unified `test_api.py` script (removed --api-version flag)
- Changed "Uploading" terminology to "Submitting"

### Deleted Files
- `src/api/gcs_batch_router.py` - Old V2 with GCS
- `src/api/v2_router.py` - Duplicate router
- `src/api/trigger.py` - Cloud Run Jobs trigger
- `src/workers/ocr_worker.py` - Pub/Sub worker
- `src/core/pubsub_client.py` - Pub/Sub client
- `.memorybank/v3.1-*.md`, `v4.0-*.md`, `v5.0-*.md` - Old version docs

### Performance
- 200 images: 640s single-step, 909s two-step
- 100% success rate
- ~3.2s per image (single-step)
- ~4.5s per image (two-step)

---

## v4.0 (February 2026)

**BackgroundTasks Implementation**

- Added V2 API with FastAPI BackgroundTasks
- Eliminated Pub/Sub timeout issues
- Increased Cloud Run timeout to 1100s

---

## v3.1 (February 2026)

**Zero Failure Fixes**

- Fixed 0KB file uploads
- Added upload verification
- Improved retry logic

---

## v3.0 (February 2026)

**Single Image API**

- New `/gcs-batch/process-image` endpoint
- Designed for BIQE C# client integration
- Per-image processing with polling
- Two-step OCR (Flash + Pro)

---

## v2.0 (January 2026)

**GCS Batch Processing**

- Added GCS-based batch flow
- Signed URL uploads
- Large file support (>32MB)
- Parallel processing

---

## v1.0 (January 2026)

**Initial Release**

- Pub/Sub-based processing
- Gemini Flash/Pro models
- Firestore job tracking
- Basic batch API

---

## Migration Notes

### From v5.0 to v5.1
No API changes. Just redeploy:
```bash
./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

### From Earlier Versions
The API endpoint remains `/gcs-batch/process-image` - no client changes needed.
