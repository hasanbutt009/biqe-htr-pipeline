# BIQE HTR - API Parameters

Complete reference for all API parameters.  
**Version**: 5.1  
**Last Updated**: February 19, 2026

---

## POST /gcs-batch/process-image

### Required Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `file` | binary | Image file (multipart/form-data) |
| `filename` | string | Original filename with extension (e.g., "document.jpg") |

### Optional Parameters

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `provider` | string | `vertex` | vertex, openrouter | AI provider for OCR |
| `two_step_ocr` | boolean | `false` | true, false | Enable two-step: Flash → Pro |
| `confidence_threshold` | float | `0.95` | 0.0-1.0 | Threshold for Pro model |
| `step1_model` | string | auto | - | Override Step 1 model |
| `step2_model` | string | auto | - | Override Step 2 model |
| `custom_prompt` | string | - | - | Custom OCR prompt (advanced) |
| `temperature` | float | `0.0` | 0.0-2.0 | Sampling temperature |
| `top_p` | float | `1.0` | 0.0-1.0 | Nucleus sampling |
| `top_k` | int | `40` | 1-100 | Top-k sampling |

---

## Parameter Details

### provider

Which AI provider to use for processing.

| Value | Models Used | Region | Best For |
|-------|-------------|--------|----------|
| `vertex` | gemini-2.5-flash, gemini-2.5-pro | europe-west4 | GDPR compliance, EU data |
| `openrouter` | gemini-3-flash, gemini-3-pro | Global | Speed, latest models |

**Default:** `vertex`

**Example:**
```bash
-F "provider=vertex"      # GDPR-compliant EU processing
-F "provider=openrouter"  # Faster, global processing
```

---

### two_step_ocr

Enable two-step processing for higher accuracy.

| Value | Behavior | Speed |
|-------|----------|-------|
| `false` | Flash model only | ~3-5s per image |
| `true` | Flash first, then Pro if confidence < threshold | ~4-17s per image |

**Default:** `false`

**How it works:**
1. Flash model processes image (~5-15s)
2. If confidence < `confidence_threshold`, Pro model improves result (~60-90s additional)
3. Pro model sees Flash result as "ground truth hint"

**Example:**
```bash
-F "two_step_ocr=true"
```

---

### confidence_threshold

Minimum confidence to skip Pro model. Only used when `two_step_ocr=true`.

| Value | Meaning |
|-------|---------|
| `0.0` | Always skip Pro (Flash only) |
| `0.90` | Pro runs if Flash < 90% confidence |
| `0.95` | Pro runs if Flash < 95% confidence (default) |
| `0.99` | Pro runs if Flash < 99% confidence (highest accuracy) |
| `1.0` | Always run Pro (maximum accuracy, slowest) |

**Default:** `0.95`

**Recommended:** `0.99` for best accuracy with reasonable speed.

**Example:**
```bash
-F "two_step_ocr=true" -F "confidence_threshold=0.99"
```

---

### step1_model / step2_model

Override the default models for each step.

| Parameter | Purpose | Default |
|-----------|---------|---------|
| `step1_model` | First step (fast) model | Auto (flash) |
| `step2_model` | Second step (accurate) model | Auto (pro) |

**Vertex AI Models:**
- `gemini-2.5-flash` (fast)
- `gemini-2.5-pro` (accurate)

**OpenRouter Models:**
- `gemini-3-flash-preview` (fast)
- `gemini-3-pro-preview` (accurate)

**Example:**
```bash
-F "step1_model=gemini-2.5-flash"
-F "step2_model=gemini-2.5-pro"
```

---

### temperature

Controls randomness in output. Lower = more deterministic.

| Value | Effect |
|-------|--------|
| `0.0` | Deterministic, consistent output (**recommended for OCR**) |
| `0.3` | Slightly creative |
| `0.7` | More variation |
| `1.0` | Creative |
| `2.0` | Maximum randomness |

**Default:** `0.0`

**Recommendation:** Keep at `0.0` for OCR tasks (consistency matters).

---

### top_p

Nucleus sampling - cumulative probability threshold.

| Value | Effect |
|-------|--------|
| `0.0` | Only most likely token |
| `0.9` | Top 90% probability mass |
| `0.95` | Top 95% probability mass |
| `1.0` | All tokens considered |

**Default:** `1.0`

---

### top_k

Top-k sampling - number of top tokens to consider.

| Value | Effect |
|-------|--------|
| `1` | Greedy (only top token) |
| `10` | Conservative |
| `40` | Balanced (default) |
| `100` | More variety |

**Default:** `40`

---

### custom_prompt

Custom OCR prompt for specialized documents.

**Default:** Uses built-in historical document prompt.

**Example:**
```bash
-F "custom_prompt=Transcribe this 18th century German letter exactly as written, preserving all original spelling."
```

---

## Supported File Formats

| Format | Extensions | MIME Type |
|--------|------------|-----------|
| JPEG | .jpg, .jpeg | image/jpeg |
| PNG | .png | image/png |
| TIFF | .tif, .tiff | image/tiff |
| WebP | .webp | image/webp |
| BMP | .bmp | image/bmp |
| GIF | .gif | image/gif |

**Maximum file size:** 20MB

**Note:** TIFF files work with Vertex AI. For OpenRouter, convert TIFF to PNG first.

---

## Response Fields

### Process Image Response

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | UUID for polling (**save this!**) |
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
| `processing_time_seconds` | int | Processing duration |

### Status Response (Failed)

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | UUID |
| `status` | string | `failed` |
| `filename` | string | Original filename |
| `error` | string | Error message |
| `retry_count` | int | Number of retries |

---

## Example Requests

### Basic Request (defaults)
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg"
```

### Two-Step with High Threshold (recommended for accuracy)
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "provider=vertex" \
  -F "two_step_ocr=true" \
  -F "confidence_threshold=0.99"
```

### OpenRouter Provider (faster)
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "provider=openrouter"
```

### Custom Sampling Parameters
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "temperature=0.1" \
  -F "top_p=0.9" \
  -F "top_k=50"
```

### Custom Prompt
```bash
curl -X POST https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/process-image \
  -F "file=@document.jpg" \
  -F "filename=document.jpg" \
  -F "custom_prompt=Transcribe this 17th century Dutch merchant letter."
```

---

## Poll for Results

```bash
# Replace {job_id} with the job_id from submit response
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/gcs-batch/jobs/{job_id}/status
```

**Poll interval:** 2-3 seconds recommended
