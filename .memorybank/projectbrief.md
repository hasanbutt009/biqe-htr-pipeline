# BIQE HTR Pipeline - Project Brief

> **Version:** 5.1  
> **Last Updated:** February 19, 2026

## Overview

OCR middleware service for the BIQE desktop application (C#). Processes historical handwritten documents (15th-19th century) using Google Gemini AI models.

---

## Client

**Jannes Hoekman** - HTR (Handwritten Text Recognition) software developer
- Building a C# desktop application called BIQE
- Needs cloud-based OCR service for document transcription
- Requires GDPR-compliant processing (EU region)

**Business Model:** €2,500 per 100,000 pages processed

---

## Architecture

### Simple Two-Endpoint API

```
POST /gcs-batch/process-image  →  Submit image, get job_id (~100ms)
GET /gcs-batch/jobs/{id}/status  →  Poll until completed
```

### Processing Flow

1. Client sends image bytes via HTTP POST
2. API returns job_id immediately (~100ms)
3. Background task processes in parallel via asyncio.create_task()
4. Gemini AI transcribes the document
5. Client polls until status = "completed"
6. Client extracts text_content from response

---

## Key Features

| Feature | Status |
|---------|--------|
| Single image processing | ✅ |
| Two-step OCR (Flash + Pro) | ✅ |
| Vertex AI (EU/GDPR) | ✅ |
| OpenRouter (faster) | ✅ |
| Confidence scoring | ✅ |
| True async parallelism | ✅ (v5.1) |
| Semaphore rate limiting | ✅ (v5.1) |
| Auto-scaling Cloud Run | ✅ |

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| API | FastAPI (Python) |
| Hosting | Google Cloud Run |
| State | Firestore |
| AI | Vertex AI (Gemini 2.5) |
| Backup AI | OpenRouter (Gemini 3) |
| Parallelism | asyncio + semaphore |

---

## Performance (v5.1)

| Metric | Value |
|--------|-------|
| Response time | ~100ms |
| Processing (flash) | ~3-5s per image |
| Processing (pro) | ~60-90s per image |
| 10 images parallel | ~5-10s total |
| Success rate | 100% |

---

## Configuration

| Setting | Value |
|---------|-------|
| Region | europe-west4 (EU) |
| Memory | 4GB |
| CPU | 4 cores |
| Timeout | 1100s |
| Min instances | 0 (cost optimization) |
| Max instances | 100 |
| Gemini concurrent | 10 per instance |

---

## Removed Components (Simplified)

| Component | Reason |
|-----------|--------|
| GCS Storage | Images in memory, faster |
| Pub/Sub | asyncio.create_task(), simpler |
| Workers | Processing in-process |

---

## Documentation

- [`activeContext.md`](./activeContext.md) - Current implementation
- [`api-reference.md`](./api-reference.md) - API specification
- [`api-parameters.md`](./api-parameters.md) - Parameter details
- [`architecture.md`](./architecture.md) - System design
- [`test-commands.md`](./test-commands.md) - Testing guide
- [`troubleshooting.md`](./troubleshooting.md) - Common issues
- [`changelog.md`](./changelog.md) - Version history

---

## Quick Start for Clients

### curl Example
```bash
# Submit image
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg"

# Poll for results
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/jobs/{job_id}/status
```

### C# Example
```csharp
// Submit
var content = new MultipartFormDataContent();
content.Add(new ByteArrayContent(imageBytes), "file", filename);
content.Add(new StringContent(filename), "filename");

var response = await httpClient.PostAsync(".../gcs-batch/process-image", content);
var jobId = JsonDocument.Parse(await response.Content.ReadAsStringAsync())
    .RootElement.GetProperty("job_id").GetString();

// Poll
while (true)
{
    var status = await httpClient.GetStringAsync($".../gcs-batch/jobs/{jobId}/status");
    var doc = JsonDocument.Parse(status);
    if (doc.RootElement.GetProperty("status").GetString() == "completed")
    {
        var text = doc.RootElement.GetProperty("text_content").GetString();
        break;
    }
    await Task.Delay(2000);
}
```
