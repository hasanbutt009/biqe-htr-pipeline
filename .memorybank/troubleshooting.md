# BIQE HTR - Troubleshooting Guide

> **Version:** 5.1  
> **Last Updated:** February 19, 2026

---

## Quick Diagnostics

```bash
# View Cloud Run logs
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="biqe-ocr-service"' \
  --project=gen-lang-client-0609361296 --limit=50 --format="table(timestamp,severity,textPayload)"

# Search for errors
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:("ERROR" OR "Failed")' \
  --project=gen-lang-client-0609361296 --limit=30

# Check parallelism (should see multiple at same timestamp)
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"acquired semaphore"' \
  --project=gen-lang-client-0609361296 --limit=20 --freshness=10m
```

---

## Common Issues

### 1. RECITATION Errors

**Symptoms:**
```
Error: RECITATION - Content policy violation
Status: failed
```

**Cause:** Gemini's content safety filter blocked the image. Usually happens with:
- Modern photographs that contain faces
- Images with sensitive content
- Very low-quality scans

**Solution:**
- The API automatically retries with Pro model (up to 3 times)
- If still failing, the image cannot be processed by Gemini
- Try with a different scan of the same document

---

### 2. Job Stuck in "processing"

**Symptoms:**
- Status stays "processing" for >5 minutes
- No completion

**Possible Causes:**
1. Cloud Run timeout (unlikely with 1100s timeout)
2. Memory issue with very large image
3. asyncio task crashed silently

**Solution:**
```bash
# Check logs for the specific job
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"JOB_ID"' \
  --project=gen-lang-client-0609361296 --limit=20
```

**Prevention:**
- Images should be <20MB
- Cloud Run has 4GB memory, 1100s timeout

---

### 3. 429 Rate Limit Errors

**Symptoms:**
```
Error: 429 Resource Exhausted
```

**Cause:** Gemini API rate limit exceeded. The semaphore should prevent this, but can happen with:
- Multiple Cloud Run instances
- Very high GEMINI_CONCURRENT_LIMIT

**Solution:**
- Reduce `--parallel-submit` in test script
- Lower `GEMINI_CONCURRENT_LIMIT` environment variable
- Default is 10 concurrent Gemini calls per instance

```bash
# Safer settings
python test_api.py --folder ./images --parallel-submit 3

# Or redeploy with lower limit
GEMINI_CONCURRENT_LIMIT=5 ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

---

### 4. Connection Errors

**Symptoms:**
```
Error: Connection refused
Error: Timeout
```

**Possible Causes:**
1. Cloud Run service not deployed
2. Network issues
3. Service URL incorrect
4. Cold start (min-instances=0)

**Solution:**
```bash
# Check service status
gcloud run services describe biqe-ocr-service \
  --region europe-west4 \
  --project gen-lang-client-0609361296

# Check health endpoint
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/health
```

**For cold start issues:**
- First request may take 5-10 seconds
- cpu-boost is enabled to reduce this
- Consider `--min-instances=1` if cold starts are critical

---

### 5. 404 Job Not Found

**Symptoms:**
```json
{"detail": "Job not found: abc123..."}
```

**Cause:** Job ID doesn't exist in Firestore.

**Possible Causes:**
1. Job submission failed silently
2. Wrong job_id copied
3. Firestore connectivity issue
4. Job created but not yet visible (rare)

**Solution:**
- Verify job_id from original submission response
- Check Firestore directly in GCP Console
- Ensure the 200 response was received on submit

---

### 6. Empty text_content

**Symptoms:**
- Status: completed
- text_content: "" (empty)

**Possible Causes:**
1. Blank page
2. Image too dark/light
3. Resolution too low
4. Non-text image (photograph)

**Solution:**
- Check confidence score (low = uncertain)
- Try preprocessing image (adjust contrast)
- Ensure minimum 150 DPI
- Verify image contains actual text

---

### 7. Low Confidence Scores

**Symptoms:**
- confidence < 0.8
- Poor transcription quality

**Possible Causes:**
1. Difficult handwriting
2. Poor image quality
3. Unusual language/script
4. Ink bleeding or faded text

**Solution:**
- Use `--two-step` for Flash→Pro processing
- Lower `--confidence-threshold` to trigger Pro more often
- Example: `--two-step --confidence-threshold 0.95`

---

### 8. Sequential Processing (Not Parallel)

**Symptoms:**
- 10 images take ~60 seconds instead of ~10 seconds
- Logs show jobs starting one after another

**Cause:** Using old version without asyncio.create_task()

**Solution:**
```bash
# Verify v5.1 is deployed
curl https://biqe-ocr-service-650561295384.europe-west4.run.app/health
# Should return: {"status": "healthy", "version": "5.1"}

# Redeploy
./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

**Verify parallelism in logs:**
```bash
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"acquired semaphore"' \
  --project=gen-lang-client-0609361296 --limit=20 --freshness=5m
```
Should see multiple "acquired semaphore" logs at the same timestamp.

---

### 9. TIFF Files Failing with OpenRouter

**Symptoms:**
```
Error: 400 Bad Request (TIFF with OpenRouter)
```

**Cause:** OpenRouter API doesn't support TIFF format directly.

**Solution:**
- Use `provider=vertex` for TIFF files (works natively)
- Or convert TIFF to PNG before submission
- Client-side preprocessing recommended for OpenRouter

---

### 10. Memory Errors

**Symptoms:**
```
Error: Container killed due to memory
```

**Cause:** Image too large or too many concurrent processes.

**Solution:**
```bash
# Current config: 4GB memory
# Reduce concurrency if needed
gcloud run deploy biqe-ocr-service \
  --memory=8Gi \
  --concurrency=10
```

---

## Deployment Issues

### Service Won't Deploy

```bash
# Check build logs
gcloud builds list --project=gen-lang-client-0609361296 --limit=5

# View build details
gcloud builds describe BUILD_ID --project=gen-lang-client-0609361296

# Redeploy
cd biqe-htr-pipeline
./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

### Wrong Service URL

The service URL is:
```
https://biqe-ocr-service-650561295384.europe-west4.run.app
```

Get current URL:
```bash
gcloud run services describe biqe-ocr-service \
  --region europe-west4 \
  --format='value(status.url)'
```

---

## Monitoring

### Cloud Run Metrics
```bash
# Current service status
gcloud run services describe biqe-ocr-service \
  --region europe-west4 \
  --format="value(status.latestCreatedRevisionName)"

# List recent revisions
gcloud run revisions list --service=biqe-ocr-service --region=europe-west4
```

### Firestore Jobs
Check in GCP Console:
- Firestore → jobs collection
- Filter by status: "processing" (stuck jobs)
- Filter by status: "failed" (error patterns)

### Real-time Logs
```bash
# Stream logs
gcloud logging tail 'resource.type="cloud_run_revision" AND resource.labels.service_name="biqe-ocr-service"' \
  --project=gen-lang-client-0609361296
```

---

## Performance Tuning

### Increase Parallelism
```bash
# Higher concurrent Gemini calls (be careful of rate limits)
GEMINI_CONCURRENT_LIMIT=15 ./deploy/deploy.sh gen-lang-client-0609361296 europe-west4
```

### Reduce Cold Starts
```bash
# Keep 1 instance warm (costs ~$30/month)
gcloud run deploy biqe-ocr-service \
  --min-instances=1
```

### Handle Higher Load
```bash
# Increase max instances
gcloud run deploy biqe-ocr-service \
  --max-instances=200
```

---

## Contact

For persistent issues not covered here:
1. Check Cloud Run logs with timestamp
2. Note the job_id
3. Review error messages in Firestore job document
4. Check the Gemini API quotas in GCP Console
