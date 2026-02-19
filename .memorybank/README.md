# BIQE HTR Pipeline - Memory Bank

> **Purpose:** Documentation hub for BIQE HTR Service  
> **Last Updated:** February 19, 2026  
> **Version:** 5.1 (True Async Parallelism)

---

## 📋 Quick Reference

| Document | Description |
|----------|-------------|
| [`activeContext.md`](./activeContext.md) | **START HERE** - Current implementation status |
| [`api-reference.md`](./api-reference.md) | API endpoints and responses |
| [`api-parameters.md`](./api-parameters.md) | All parameters with examples |
| [`architecture.md`](./architecture.md) | System architecture diagrams |
| [`test-commands.md`](./test-commands.md) | Test script usage |
| [`troubleshooting.md`](./troubleshooting.md) | Common issues and solutions |
| [`techContext.md`](./techContext.md) | GCP configuration |
| [`projectbrief.md`](./projectbrief.md) | Original project requirements |
| [`changelog.md`](./changelog.md) | Version history |

---

## 🎯 Current Architecture (v5.1)

**Two-endpoint API with True Async Parallelism:**

```
POST /gcs-batch/process-image  →  Submit image, get job_id (~100ms)
GET /gcs-batch/jobs/{id}/status  →  Poll until completed
```

**Key Features:**
- ✅ True async parallelism using `asyncio.create_task()`
- ✅ Semaphore rate limiting (10 concurrent Gemini calls per instance)
- ✅ No GCS storage - images processed in memory
- ✅ Results stored directly in Firestore `text_content` field
- ✅ Auto-scaling Cloud Run (0-100 instances)
- ✅ ~5-10s for 10 images (parallel), not ~60s (sequential)

---

## 🚀 Quick Start

```bash
cd biqe-htr-pipeline

# Test ALL images in folder
python test_api.py --folder ./images

# Limit to 10 images
python test_api.py --folder ./images --limit 10

# Two-step processing (more accurate)
python test_api.py --folder ./images --two-step --confidence-threshold 0.99

# Fast parallel with OpenRouter
python test_api.py --folder ./images --provider openrouter --parallel-submit 10

# Single image
python test_api.py --image ./images/document.jpg

# Save results
python test_api.py --folder ./images --output ./results
```

---

## 📊 Performance

| Mode | Time/Image | 10 Images | Notes |
|------|------------|-----------|-------|
| Single-step (flash) | ~3-5s | ~5-10s parallel | Fast, good for most documents |
| Two-step (flash→pro) | ~4-17s | ~15-30s parallel | Higher accuracy |

**Parallelism:**
- Server: Up to 10 concurrent Gemini calls per Cloud Run instance
- Client: Use `--parallel-submit 10` for max throughput
- Auto-scaling: Up to 100 Cloud Run instances

---

## 🔧 Configuration

| Setting | Value |
|---------|-------|
| Service URL | `https://biqe-ocr-service-650561295384.europe-west4.run.app` |
| Region | europe-west4 (EU/GDPR) |
| Default Provider | vertex |
| Models | gemini-2.5-flash, gemini-2.5-pro |
| Cloud Run Memory | 4GB |
| Cloud Run CPU | 4 |
| Timeout | 1100s |
| Min Instances | 0 (cost optimization) |
| Max Instances | 100 |
| Concurrency | 20 per instance |
| Gemini Concurrent | 10 per instance |

---

## 📁 File Structure

```
biqe-htr-pipeline/
├── src/
│   ├── api/
│   │   ├── main.py          # FastAPI app
│   │   └── v3_router.py     # Main API router
│   └── core/
│       ├── firestore_client.py
│       └── ocr_engine.py
├── deploy/
│   ├── deploy.sh
│   └── Dockerfile
├── test_api.py              # Test script
├── README.md
└── .memorybank/             # This documentation
```

---

**Maintainer:** BIQE Development Team  
**Client:** Jannes (HTR Integration)
