# BIQE HTR Pipeline

> **API for Historical Document Transcription using Google Gemini AI**  
> Base URL: `https://biqe-ocr-service-650561295384.europe-west4.run.app`  
> Region: europe-west4 (GDPR-compliant EU)  
> Version: 5.1 (True Async Parallelism)

---

## Quick Start

### 1. Submit Image

```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "provider=vertex" \
  -F "two_step_ocr=true"
```

**Response (~100ms):**
```json
{
  "job_id": "abc123-def456-...",
  "status": "processing",
  "poll_url": "/gcs-batch/jobs/abc123.../status",
  "provider": "vertex",
  "created_at": "2026-02-19T12:00:00Z"
}
```

### 2. Poll for Results

```bash
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/jobs/{job_id}/status
```

**Response (completed):**
```json
{
  "job_id": "abc123-...",
  "status": "completed",
  "filename": "document.jpg",
  "provider": "vertex",
  "confidence": 0.96,
  "model_used": "gemini-2.5-flash",
  "text_content": "Full transcription text...",
  "completed_at": "2026-02-19T12:00:25Z",
  "processing_time_seconds": 25
}
```

---

## Test Script

```bash
cd biqe-htr-pipeline

# Test ALL images in folder
python test_api.py --folder ./images

# Limit to first 10 images
python test_api.py --folder ./images --limit 10

# Two-step processing (more accurate)
python test_api.py --folder ./images --two-step --confidence-threshold 0.99

# Single image
python test_api.py --image ./images/document.jpg

# Save results to files
python test_api.py --folder ./images --output ./results

# Fast parallel processing with OpenRouter
python test_api.py --folder ./images --provider openrouter --parallel-submit 10
```

---

## API Endpoints

### POST /gcs-batch/process-image

Submit an image for OCR processing.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file` | binary | **required** | Image file (JPEG, PNG, TIFF, WEBP, BMP, GIF) |
| `filename` | string | **required** | Original filename with extension |
| `provider` | string | `vertex` | `vertex` (EU/GDPR) or `openrouter` (faster) |
| `two_step_ocr` | bool | `false` | Enable two-step: Flash → Pro for higher accuracy |
| `confidence_threshold` | float | `0.95` | Threshold to trigger Pro model (0.0-1.0) |
| `step1_model` | string | auto | Override Step 1 model |
| `step2_model` | string | auto | Override Step 2 model |
| `custom_prompt` | string | - | Custom OCR prompt (advanced) |
| `temperature` | float | `0.0` | Sampling temperature (0.0-2.0) |
| `top_p` | float | `1.0` | Nucleus sampling (0.0-1.0) |
| `top_k` | int | `40` | Top-k sampling (1-100) |

### GET /gcs-batch/jobs/{job_id}/status

Poll for job status and results.

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | Unique job identifier |
| `status` | string | `processing`, `completed`, or `failed` |
| `filename` | string | Original filename |
| `provider` | string | Provider used |
| `confidence` | float | OCR confidence (0.0-1.0), only when completed |
| `model_used` | string | Gemini model used, only when completed |
| `text_content` | string | **Transcribed text**, only when completed |
| `processing_time_seconds` | int | Processing duration, only when completed |
| `error` | string | Error message, only when failed |

### GET /health

Health check endpoint. Returns `{"status": "healthy", "version": "5.1"}`.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Client (C# App / test_api.py)                                   │
│  └── POST image → GET status (poll) → Extract text_content       │
└──────────────────────────────────────────────────────────────────┘
                              ↓ HTTP
┌──────────────────────────────────────────────────────────────────┐
│  Cloud Run (FastAPI)                                             │
│  ├── Receive image bytes (in memory, no GCS)                     │
│  ├── Create job in Firestore                                     │
│  ├── Return job_id immediately (~100ms)                          │
│  │                                                               │
│  │  [True Async Parallelism - asyncio.create_task()]             │
│  ├── Semaphore limits concurrent Gemini calls (default: 10)      │
│  ├── Multiple images process IN PARALLEL                         │
│  └── Results saved to Firestore text_content field               │
└──────────────────────────────────────────────────────────────────┘
                    ↓                              ↓
          ┌─────────────────┐            ┌─────────────────┐
          │  Firestore      │            │  Vertex AI      │
          │  └── jobs       │            │  ├── Flash      │
          │      ├── status │            │  └── Pro        │
          │      └── text   │            │  (or OpenRouter)│
          └─────────────────┘            └─────────────────┘
```

**Key Features (v5.1):**
- ✅ True async parallelism via `asyncio.create_task()`
- ✅ Semaphore rate limiting (10 concurrent Gemini calls per instance)
- ✅ No GCS storage - images processed in memory
- ✅ Results stored directly in Firestore
- ✅ Auto-scaling Cloud Run (0-100 instances)
- ✅ 100% success rate in production tests

---

## Performance

| Mode | Time/Image | Parallelism | Notes |
|------|------------|-------------|-------|
| Single-step (flash) | ~3-5s | Up to 10 concurrent | Fast, good for most documents |
| Two-step (flash→pro) | ~4-17s | Up to 10 concurrent | Higher accuracy, uses Pro if flash <95% |

**10 images parallel processing:**
- Before (sequential): ~50-60 seconds
- After (parallel): ~5-10 seconds

---

## Documentation

See [`.memorybank/`](.memorybank/README.md) for full documentation:
- [Active Context](.memorybank/activeContext.md) - Current implementation
- [API Reference](.memorybank/api-reference.md) - Full API specification
- [API Parameters](.memorybank/api-parameters.md) - Detailed parameter docs
- [Architecture](.memorybank/architecture.md) - System design diagrams
- [Test Commands](.memorybank/test-commands.md) - Testing guide
- [Troubleshooting](.memorybank/troubleshooting.md) - Common issues
- [Changelog](.memorybank/changelog.md) - Version history

---

## Configuration

| Setting | Value |
|---------|-------|
| Project | gen-lang-client-0609361296 |
| Region | europe-west4 |
| Service | biqe-ocr-service |
| Memory | 4GB |
| CPU | 4 |
| Timeout | 1100s |
| Min Instances | 0 (cost optimization) |
| Max Instances | 100 |
| Concurrency | 20 per instance |
| Gemini Concurrent Limit | 10 per instance |

**Models:**
- Vertex AI: `gemini-2.5-flash`, `gemini-2.5-pro`
- OpenRouter: `gemini-3-flash-preview`, `gemini-3-pro-preview`

---

## Deployment

```bash
cd biqe-htr-pipeline
./deploy/deploy.sh gen-lang-client-0609361296 europe-west4

# With custom parallelism
GEMINI_CONCURRENT_LIMIT=15 ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

---

## C# Integration Example

```csharp
// Step 1: Submit image
var content = new MultipartFormDataContent();
content.Add(new ByteArrayContent(imageBytes), "file", filename);
content.Add(new StringContent(filename), "filename");
content.Add(new StringContent("vertex"), "provider");
content.Add(new StringContent("true"), "two_step_ocr");

var response = await httpClient.PostAsync(
    "https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image",
    content
);
var result = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
var jobId = result.RootElement.GetProperty("job_id").GetString();

// Step 2: Poll for results
while (true)
{
    var statusResponse = await httpClient.GetStringAsync(
        $"https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/jobs/{jobId}/status"
    );
    var doc = JsonDocument.Parse(statusResponse);
    var status = doc.RootElement.GetProperty("status").GetString();
    
    if (status == "completed")
    {
        var text = doc.RootElement.GetProperty("text_content").GetString();
        Console.WriteLine($"Transcription: {text}");
        break;
    }
    else if (status == "failed")
    {
        var error = doc.RootElement.GetProperty("error").GetString();
        Console.WriteLine($"Error: {error}");
        break;
    }
    
    await Task.Delay(2000); // Poll every 2 seconds
}
```

---

## Version History

- **v5.1** (Feb 2026): True async parallelism with semaphore
- **v5.0** (Feb 2026): Removed GCS/Pub/Sub, BackgroundTasks
- **v3.0** (Feb 2026): Single image API for C# integration
- **v2.0** (Jan 2026): GCS batch processing
- **v1.0** (Jan 2026): Initial Pub/Sub-based pipeline
