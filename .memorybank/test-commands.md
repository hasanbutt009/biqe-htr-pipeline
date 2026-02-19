# BIQE HTR - Test Commands

Test script: `test_api.py`  
**Last Updated:** February 19, 2026

---

## Basic Usage

```bash
cd biqe-htr-pipeline

# Test ALL images in folder
python test_api.py --folder ./images

# Test single image
python test_api.py --image ./images/document.jpg
```

---

## Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--folder` | Folder containing images | - |
| `--image` | Single image file | - |
| `--limit` | Maximum images to process | ALL |
| `--provider` | `vertex` or `openrouter` | `vertex` |
| `--two-step` | Enable two-step processing | disabled |
| `--confidence-threshold` | Threshold for Pro model | `0.95` |
| `--output` | Save results to folder | - |
| `--parallel-submit` | Concurrent submissions | `5` |
| `--parallel-poll` | Concurrent polls | `20` |

---

## Common Commands

### Process ALL Images
```bash
python test_api.py --folder ./images
```
Processes every image in the folder.

### Limit to First 10 Images
```bash
python test_api.py --folder ./images --limit 10
```
Useful for quick testing.

### Single-Step (Fast)
```bash
python test_api.py --folder ./images
```
~3-5s per image (Flash model only)

### Two-Step (Accurate)
```bash
python test_api.py --folder ./images --two-step --confidence-threshold 0.99
```
~4-17s per image (Flash + Pro if needed)

### Save Results to Files
```bash
python test_api.py --folder ./images --output ./results --two-step
```
Creates `.txt` and `.json` files for each image.

### OpenRouter Provider (Faster)
```bash
python test_api.py --folder ./images --provider openrouter
```

### Single Image
```bash
python test_api.py --image ./images/PRO_0001.jpg --output ./results
```

### Maximum Parallel Throughput
```bash
python test_api.py --folder ./images --parallel-submit 10 --parallel-poll 30
```
For large batches with fast processing.

### Full Production Test
```bash
python test_api.py \
  --folder /path/to/images \
  --provider vertex \
  --two-step \
  --confidence-threshold 0.99 \
  --parallel-submit 10 \
  --output ./production_results
```

---



---

## Result Files

When using `--output`, creates:
```
output/
├── image1.txt       # Transcribed text only
├── image1.json      # Full result with metadata
├── image2.txt
├── image2.json
└── ...
```

**Text file format:**
```
The transcribed text from the document...
```

**JSON format:**
```json
{
  "job_id": "abc123...",
  "filename": "image1.jpg",
  "status": "completed",
  "confidence": 0.96,
  "model_used": "gemini-2.5-flash",
  "text_content": "Full transcription...",
  "processing_time_seconds": 5
}
```

---

## Performance Results (v5.1)

### With True Async Parallelism

| Scenario | Time | Notes |
|----------|------|-------|
| 10 images (parallel) | ~5-10s | 6-12x faster than sequential |
| 50 images (parallel) | ~25-50s | Auto-scales Cloud Run |
| 200 images (parallel) | ~100-200s | Multiple instances |

### Time Per Image

| Mode | Time/Image | Total for 10 |
|------|------------|--------------|
| Single-step (flash) | ~3-5s | ~5-10s parallel |
| Two-step (0.95) | ~3-5s | ~5-10s parallel |
| Two-step (0.99) | ~4-17s | ~15-30s parallel |

---

## Troubleshooting

### RECITATION Errors
Some images trigger content policy. The script retries automatically up to 3 times.

### Timeout
Default timeout is 300s per job. Very complex documents may take longer with Pro model.

### Connection Errors
The script retries failed submissions automatically.

### Rate Limits
If seeing 429 errors, reduce `--parallel-submit`:
```bash
python test_api.py --folder ./images --parallel-submit 3
```

### Too Many Open Files
On macOS, increase limit:
```bash
ulimit -n 1024
```

---

## Environment Setup

```bash
cd biqe-htr-pipeline

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run tests
python test_api.py --folder ./images --limit 5
```

---

## curl Testing

### Submit single image:
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "provider=vertex"
```

### Check status:
```bash
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/jobs/{job_id}/status
```

### Health check:
```bash
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/health
```
