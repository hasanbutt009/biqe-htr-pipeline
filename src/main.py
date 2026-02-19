"""
BIQE HTR Pipeline - Cloud Run Job Entry Point.

This is the main entry point for the Cloud Run Job that processes
handwritten text images using the Gemini Flash/Pro chained logic.

Usage:
    Cloud Run Job execution:
        python -m src.main
    
    Local testing:
        JOB_ID=test INPUT_PATH=gs://bucket/path python -m src.main
"""

import sys
import time
import structlog
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.config import settings
from src.core import OCREngine, AtomicStorageManager, PubSubNotifier


# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer() if settings.log_format == "json" 
            else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class HTRPipelineJob:
    """
    Main HTR Pipeline Job orchestrator.
    
    Coordinates the OCR engine, storage manager, and notifier
    to process a batch of images from GCS.
    """

    def __init__(self):
        """Initialize pipeline components."""
        self.ocr_engine = OCREngine()
        self.storage = AtomicStorageManager()
        self.notifier = PubSubNotifier()
        
        # Job state
        self.job_id = settings.job_id or str(uuid4())
        self.input_path = settings.input_path
        self.task_index = settings.task_index
        self.task_count = settings.task_count
        
        logger.info(
            "HTR Pipeline Job initialized",
            job_id=self.job_id,
            input_path=self.input_path,
            task_index=self.task_index,
            task_count=self.task_count,
        )

    def run(self) -> int:
        """
        Execute the HTR pipeline job.
        
        Returns:
            Exit code (0 for success, 1 for failure)
        """
        start_time = time.perf_counter()
        successful = 0
        failed = 0
        
        try:
            # Validate configuration
            if not self.input_path:
                raise ValueError("INPUT_PATH environment variable is required")
            
            # List images to process
            images = self.storage.list_images_in_path(self.input_path)
            total_images = len(images)
            
            if total_images == 0:
                logger.warning("No images found in input path", path=self.input_path)
                return 0
            
            # For parallel Cloud Run tasks, partition the work
            if self.task_count > 1:
                images = self._partition_images(images)
                logger.info(
                    "Partitioned images for parallel processing",
                    task_index=self.task_index,
                    images_in_partition=len(images),
                    total_images=total_images,
                )
            
            # Notify job started (only from task 0)
            if self.task_index == 0:
                self.notifier.notify_job_started(
                    job_id=self.job_id,
                    total_images=total_images,
                    input_path=self.input_path,
                )
            
            # Process each image
            for idx, image_gcs_path in enumerate(images, start=1):
                try:
                    result = self._process_single_image(image_gcs_path)
                    
                    if result.error:
                        failed += 1
                        status = "failed"
                    else:
                        successful += 1
                        status = "success"
                    
                    # Notify progress
                    self.notifier.notify_image_processed(
                        job_id=self.job_id,
                        image_index=idx + (self.task_index * len(images)),
                        image_name=Path(image_gcs_path).name,
                        status=status,
                        model_used=result.model_used.value,
                        confidence=result.confidence_score,
                        processing_time_ms=result.processing_time_ms,
                    )
                    
                except Exception as e:
                    logger.error(
                        "Failed to process image",
                        image=image_gcs_path,
                        error=str(e),
                    )
                    failed += 1
            
            # Notify job complete (only from task 0, or in single-task mode)
            if self.task_index == 0 or self.task_count == 1:
                total_time = time.perf_counter() - start_time
                output_path = f"gs://{settings.gcs_output_bucket}/{self.job_id}/"
                
                self.notifier.notify_job_complete(
                    job_id=self.job_id,
                    total_processed=successful + failed,
                    successful=successful,
                    failed=failed,
                    output_path=output_path,
                    total_time_seconds=total_time,
                )
            
            logger.info(
                "Job completed",
                job_id=self.job_id,
                successful=successful,
                failed=failed,
                total_time_seconds=round(time.perf_counter() - start_time, 2),
            )
            
            return 0 if failed == 0 else 1
            
        except Exception as e:
            logger.exception("Job failed with fatal error", error=str(e))
            
            self.notifier.notify_job_failed(
                job_id=self.job_id,
                error_code="FATAL_ERROR",
                error_message=str(e),
                images_processed_before_failure=successful,
            )
            
            return 1
            
        finally:
            # Cleanup temporary files
            self.storage.cleanup_temp_files()

    def _process_single_image(self, gcs_path: str):
        """
        Process a single image through the full pipeline.
        
        Args:
            gcs_path: GCS path to the image
            
        Returns:
            OCRResult from processing
        """
        logger.info("Processing image", gcs_path=gcs_path)
        
        # Download image to local storage
        local_path = self.storage.download_image(gcs_path)
        
        try:
            # Process with OCR engine (Flash -> Pro chained logic)
            result = self.ocr_engine.process_image(local_path)
            
            if not result.error:
                # Prepare output data (BIQE UI compatible format)
                output_data = {
                    "document_id": result.document_id,
                    "job_id": self.job_id,
                    "source_file": result.source_file,
                    "processing": {
                        "model": result.model_used.value,
                        "confidence_score": result.confidence_score,
                        "flash_attempted": result.flash_attempted,
                        "pro_fallback_used": result.pro_fallback_used,
                        "processing_time_ms": result.processing_time_ms,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    "result": {
                        "text_content": result.text_content,
                    },
                    "metadata": {
                        "pipeline_version": "1.0.0",
                    }
                }
                
                # Atomic write to GCS (with verification)
                output_filename = f"{Path(result.source_file).stem}.json"
                self.storage.atomic_write_json(
                    data=output_data,
                    destination_path=output_filename,
                    job_id=self.job_id,
                )
            
            return result
            
        finally:
            # Cleanup local file
            if local_path.exists():
                local_path.unlink()

    def _partition_images(self, images: list[str]) -> list[str]:
        """
        Partition images for parallel Cloud Run tasks.
        
        Each task gets a subset of images based on its task index.
        
        Args:
            images: Full list of images
            
        Returns:
            Subset of images for this task
        """
        # Simple round-robin partitioning
        return [
            img for i, img in enumerate(images) 
            if i % self.task_count == self.task_index
        ]


def main():
    """Main entry point."""
    logger.info(
        "Starting BIQE HTR Pipeline",
        project=settings.gcp_project_id,
        region=settings.gcp_region,
    )
    
    job = HTRPipelineJob()
    exit_code = job.run()
    
    logger.info("Pipeline exiting", exit_code=exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
