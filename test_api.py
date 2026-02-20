#!/usr/bin/env python3
"""
BIQE HTR API Test Script

Simple script to test the HTR OCR API with images.

=== QUICK START ===

1. Test with a folder of images:
   python test_api.py --folder ./images

2. Test with a single image:
   python test_api.py --image ./images/PRO_0001.jpg

3. Use two-step processing (Flash + Pro):
   python test_api.py --folder ./images --two-step

4. Save results to files:
   python test_api.py --folder ./images --output ./results

=== API ENDPOINTS ===

- POST /gcs-batch/process-image  → Submit image, get job_id
- GET /gcs-batch/jobs/{id}/status → Poll for results
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

try:
    import aiofiles
    import httpx
except ImportError:
    print("Missing dependencies. Run: pip install httpx aiofiles")
    sys.exit(1)


# =============================================================================
# Configuration
# =============================================================================

API_URL = "https://biqe-ocr-service-650561295384.europe-west4.run.app"
SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}

# Retry settings - aggressive retries to handle server load
MAX_RETRIES = 10  # More retries for high load scenarios
RETRY_DELAY = 3   # Longer delay between retries

# Parallel settings
PARALLEL_SUBMIT = 10   # Concurrent uploads
PARALLEL_POLL = 10    # Concurrent status checks

# Poll settings
POLL_INTERVAL = 3  # seconds
POLL_TIMEOUT = 600  # 10 minutes max


# =============================================================================
# Submit & Poll Workers
# =============================================================================

async def submit_worker(
    client: httpx.AsyncClient,
    submit_queue: asyncio.Queue,
    poll_queue: asyncio.Queue,
    provider: str,
    two_step: bool,
    confidence_threshold: float,
    results: Dict[str, Any],
    submit_semaphore: asyncio.Semaphore,
    total_images: int,
):
    """Worker that submits images from the queue."""
    while True:
        item = await submit_queue.get()
        if item is None:  # Poison pill
            submit_queue.task_done()
            break
        
        index, image_path = item
        
        async with submit_semaphore:
            file_size_mb = image_path.stat().st_size / (1024 * 1024)
            print(f"  [{index}/{total_images}] Submitting {image_path.name} ({file_size_mb:.1f}MB)...", end="", flush=True)
            
            for attempt in range(MAX_RETRIES):
                try:
                    async with aiofiles.open(image_path, 'rb') as f:
                        file_data = await f.read()
                    
                    response = await client.post(
                        f"{API_URL}/gcs-batch/process-image",
                        files={"file": (image_path.name, file_data)},
                        data={
                            "filename": image_path.name,
                            "provider": provider,
                            "two_step_ocr": str(two_step).lower(),
                            "confidence_threshold": str(confidence_threshold),
                        },
                        timeout=120.0,
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        job_id = result["job_id"]
                        print(f" ✓ {job_id[:8]}...")
                        
                        # Add to poll queue immediately
                        await poll_queue.put({
                            "index": index,
                            "job_id": job_id,
                            "filename": image_path.name,
                            "path": image_path,
                        })
                        results["submitted"] += 1
                        break
                    else:
                        if attempt < MAX_RETRIES - 1:
                            print(f" retry...", end="", flush=True)
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                        print(f" ❌ HTTP {response.status_code}")
                        results["submit_failed"].append({
                            "filename": image_path.name,
                            "error": f"HTTP {response.status_code}"
                        })
                        break
                        
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        print(f" retry...", end="", flush=True)
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    print(f" ❌ {str(e)[:30]}")
                    results["submit_failed"].append({
                        "filename": image_path.name,
                        "error": str(e)[:100]
                    })
                    break
        
        submit_queue.task_done()


async def poll_worker(
    client: httpx.AsyncClient,
    poll_queue: asyncio.Queue,
    output_dir: Optional[Path],
    results: Dict[str, Any],
    poll_semaphore: asyncio.Semaphore,
    total_images: int,
    stop_event: asyncio.Event,
):
    """Worker that polls jobs from the queue."""
    while True:
        try:
            # Wait for job with timeout (to check stop event)
            try:
                job_info = await asyncio.wait_for(poll_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if stop_event.is_set() and poll_queue.empty():
                    break
                continue
            
            index = job_info["index"]
            job_id = job_info["job_id"]
            filename = job_info["filename"]
            
            async with poll_semaphore:
                start_time = time.time()
                
                while time.time() - start_time < POLL_TIMEOUT:
                    try:
                        response = await client.get(
                            f"{API_URL}/gcs-batch/jobs/{job_id}/status",
                            timeout=30.0,
                        )
                        
                        if response.status_code == 200:
                            data = response.json()
                            status = data.get("status")
                            
                            if status == "completed":
                                confidence = data.get("confidence", 0)
                                model = data.get("model_used", "?")
                                text = data.get("text_content", "")
                                
                                print(f"  [{index}/{total_images}] ✓ {filename}: {confidence*100:.0f}% ({model})")
                                
                                results["completed"].append({
                                    "filename": filename,
                                    "confidence": confidence,
                                    "model": model,
                                    "text_length": len(text),
                                })
                                
                                # Save to file if output_dir specified
                                if output_dir and text:
                                    txt_filename = Path(filename).stem + ".txt"
                                    async with aiofiles.open(output_dir / txt_filename, 'w') as f:
                                        await f.write(text)
                                
                                break
                            
                            elif status == "failed":
                                error = data.get("error", "Unknown error")
                                print(f"  [{index}/{total_images}] ❌ {filename}: {error[:50]}")
                                results["poll_failed"].append({
                                    "filename": filename,
                                    "error": error
                                })
                                break
                            
                            else:
                                # Still processing
                                await asyncio.sleep(POLL_INTERVAL)
                        else:
                            await asyncio.sleep(POLL_INTERVAL)
                            
                    except Exception as e:
                        await asyncio.sleep(POLL_INTERVAL)
                else:
                    # Timeout
                    print(f"  [{index}/{total_images}] ⏰ {filename}: Timeout")
                    results["poll_failed"].append({
                        "filename": filename,
                        "error": "Timeout"
                    })
            
            poll_queue.task_done()
            
        except asyncio.CancelledError:
            break


# =============================================================================
# Helper Functions
# =============================================================================

def find_images(folder: Path) -> List[Path]:
    """Find all supported images in a folder."""
    images = []
    for ext in SUPPORTED_FORMATS:
        images.extend(folder.glob(f"*{ext}"))
        images.extend(folder.glob(f"*{ext.upper()}"))
    return sorted(set(images))


async def run_batch(
    images: List[Path],
    provider: str,
    two_step: bool,
    confidence_threshold: float,
    output_dir: Optional[Path],
    parallel_submit: int,
    parallel_poll: int,
):
    """Process a batch of images using pipeline approach."""
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\nProcessing {len(images)} images...")
    print(f"  Provider: {provider}")
    print(f"  Two-step: {two_step}")
    print(f"  Confidence threshold: {confidence_threshold}")
    print(f"  Parallel submit: {parallel_submit}")
    print(f"  Parallel poll: {parallel_poll}")
    if output_dir:
        print(f"  Output: {output_dir}")
    print()
    
    start_time = time.time()
    
    # Shared state
    results = {
        "submitted": 0,
        "completed": [],
        "submit_failed": [],
        "poll_failed": [],
    }
    
    # Queues and semaphores
    submit_queue = asyncio.Queue()
    poll_queue = asyncio.Queue()
    submit_semaphore = asyncio.Semaphore(parallel_submit)
    poll_semaphore = asyncio.Semaphore(parallel_poll)
    stop_event = asyncio.Event()
    
    # Fill submit queue
    for i, img in enumerate(images):
        await submit_queue.put((i + 1, img))
    
    # Add poison pills for submit workers
    num_submit_workers = min(parallel_submit, len(images))
    for _ in range(num_submit_workers):
        await submit_queue.put(None)
    
    async with httpx.AsyncClient() as client:
        # Start workers
        submit_workers = [
            asyncio.create_task(submit_worker(
                client, submit_queue, poll_queue, provider, two_step,
                confidence_threshold, results, submit_semaphore, len(images)
            ))
            for _ in range(num_submit_workers)
        ]
        
        poll_workers = [
            asyncio.create_task(poll_worker(
                client, poll_queue, output_dir, results, poll_semaphore,
                len(images), stop_event
            ))
            for _ in range(parallel_poll)
        ]
        
        # Wait for all submissions to complete
        await asyncio.gather(*submit_workers)
        
        # Signal poll workers that no more jobs are coming
        stop_event.set()
        
        # Wait for all polling to complete
        await poll_queue.join()
        
        # Cancel poll workers
        for w in poll_workers:
            w.cancel()
        
        # Wait for workers to finish
        await asyncio.gather(*poll_workers, return_exceptions=True)
    
    total_time = time.time() - start_time
    
    # Summary
    print()
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    
    total = len(images)
    submitted = results["submitted"]
    completed = len(results["completed"])
    submit_failed = len(results["submit_failed"])
    poll_failed = len(results["poll_failed"])
    
    print(f"  Total images: {total}")
    print(f"  Submitted: {submitted}")
    print(f"  Completed: {completed}")
    print(f"  Failed submissions: {submit_failed}")
    print(f"  Failed processing: {poll_failed}")
    print()
    print(f"  Total time: {total_time:.1f}s")
    print(f"  Avg per image: {total_time/total:.1f}s")
    
    if output_dir:
        print(f"\n  Results saved to: {output_dir}")
    
    print("=" * 60)
    
    if completed == total:
        print(f"\n✅ SUCCESS RATE: 100%")
    else:
        success_rate = completed / total * 100 if total else 0
        print(f"\n⚠️  SUCCESS RATE: {success_rate:.1f}%")
        
        if results["submit_failed"]:
            print("\nFailed submissions:")
            for r in results["submit_failed"]:
                print(f"  - {r['filename']}: {r.get('error', 'Unknown')}")
        
        if results["poll_failed"]:
            print("\nFailed processing:")
            for r in results["poll_failed"]:
                print(f"  - {r['filename']}: {r.get('error', 'Unknown')}")


async def run_single(
    image_path: Path,
    provider: str,
    two_step: bool,
    confidence_threshold: float,
    output_dir: Optional[Path],
):
    """Process a single image."""
    print(f"\nProcessing: {image_path}")
    print(f"  Provider: {provider}")
    print(f"  Two-step: {two_step}")
    print()
    
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    
    async with httpx.AsyncClient() as client:
        # Submit
        file_size_mb = image_path.stat().st_size / (1024 * 1024)
        print(f"  [1/1] Submitting {image_path.name} ({file_size_mb:.1f}MB)...", end="", flush=True)
        
        try:
            async with aiofiles.open(image_path, 'rb') as f:
                file_data = await f.read()
            
            response = await client.post(
                f"{API_URL}/gcs-batch/process-image",
                files={"file": (image_path.name, file_data)},
                data={
                    "filename": image_path.name,
                    "provider": provider,
                    "two_step_ocr": str(two_step).lower(),
                    "confidence_threshold": str(confidence_threshold),
                },
                timeout=120.0,
            )
            
            if response.status_code != 200:
                print(f" ❌ HTTP {response.status_code}")
                return
            
            job_id = response.json()["job_id"]
            print(f" ✓ {job_id[:8]}...")
        except Exception as e:
            print(f" ❌ {str(e)[:50]}")
            return
        
        # Poll
        print(f"  [1/1] Waiting for result...", end="", flush=True)
        start_time = time.time()
        
        while time.time() - start_time < POLL_TIMEOUT:
            try:
                response = await client.get(
                    f"{API_URL}/gcs-batch/jobs/{job_id}/status",
                    timeout=30.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    status = data.get("status")
                    
                    if status == "completed":
                        confidence = data.get("confidence", 0)
                        model = data.get("model_used", "?")
                        text = data.get("text_content", "")
                        processing_time = data.get("processing_time_seconds", 0)
                        
                        print(f" ✓ {confidence*100:.0f}% ({model})")
                        print()
                        print("=" * 60)
                        print("  RESULT")
                        print("=" * 60)
                        print(f"  Confidence: {confidence*100:.1f}%")
                        print(f"  Model: {model}")
                        print(f"  Processing time: {processing_time}s")
                        print(f"  Text length: {len(text)} chars")
                        print()
                        print("  Text preview:")
                        print("-" * 60)
                        # Show first 500 chars
                        preview = text[:500] + "..." if len(text) > 500 else text
                        print(preview)
                        print("-" * 60)
                        
                        # Save to file if output_dir specified
                        if output_dir and text:
                            txt_filename = image_path.stem + ".txt"
                            async with aiofiles.open(output_dir / txt_filename, 'w') as f:
                                await f.write(text)
                            print(f"\n  Saved to: {output_dir / txt_filename}")
                        
                        return
                    
                    elif status == "failed":
                        error = data.get("error", "Unknown error")
                        print(f" ❌ {error[:50]}")
                        return
                
                await asyncio.sleep(POLL_INTERVAL)
                
            except Exception as e:
                await asyncio.sleep(POLL_INTERVAL)
        
        print(f" ⏰ Timeout after {POLL_TIMEOUT}s")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="BIQE HTR API Test Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_api.py --folder ./images
  python test_api.py --image ./images/PRO_0001.jpg
  python test_api.py --folder ./images --two-step --output ./results
  python test_api.py --folder ./images --provider openrouter
        """,
    )
    
    # Input source (mutually exclusive)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--folder", type=Path, help="Folder containing images")
    source.add_argument("--image", type=Path, help="Single image file")
    
    # Processing options
    parser.add_argument(
        "--provider", 
        choices=["vertex", "openrouter"], 
        default="vertex",
        help="AI provider (default: vertex)"
    )
    parser.add_argument(
        "--two-step", 
        action="store_true",
        help="Use two-step processing (Flash + Pro)"
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.95,
        help="Confidence threshold for Pro fallback (default: 0.95)"
    )
    
    # Output options
    parser.add_argument(
        "--output", 
        type=Path,
        help="Output folder for transcriptions"
    )
    parser.add_argument(
        "--parallel-submit",
        type=int,
        default=PARALLEL_SUBMIT,
        help=f"Parallel uploads (default: {PARALLEL_SUBMIT})"
    )
    parser.add_argument(
        "--parallel-poll",
        type=int,
        default=PARALLEL_POLL,
        help=f"Parallel status checks (default: {PARALLEL_POLL})"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of images to process"
    )
    
    args = parser.parse_args()
    
    # Print header
    print("=" * 60)
    print("  BIQE HTR API Test")
    print("=" * 60)
    
    if args.folder:
        if not args.folder.exists():
            print(f"Error: Folder not found: {args.folder}")
            sys.exit(1)
        
        images = find_images(args.folder)
        if not images:
            print(f"Error: No images found in {args.folder}")
            sys.exit(1)
        
        if args.limit:
            images = images[:args.limit]
        
        asyncio.run(run_batch(
            images, args.provider, args.two_step,
            args.confidence_threshold, args.output,
            args.parallel_submit, args.parallel_poll
        ))
    
    else:  # Single image
        if not args.image.exists():
            print(f"Error: Image not found: {args.image}")
            sys.exit(1)
        
        asyncio.run(run_single(
            args.image, args.provider, args.two_step,
            args.confidence_threshold, args.output
        ))


if __name__ == "__main__":
    main()
