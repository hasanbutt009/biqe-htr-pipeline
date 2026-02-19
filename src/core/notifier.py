"""
Pub/Sub Notifier for C# UI Integration.

This module publishes status events to Google Pub/Sub so the
BIQE C# Desktop application can update its progress bar in real-time.

Event Types:
- JOB_STARTED: Job has begun processing
- IMAGE_PROCESSED: Individual image completed
- JOB_COMPLETE: All images processed successfully
- JOB_FAILED: Job encountered a fatal error
"""

import json
import structlog
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from google.cloud import pubsub_v1

from src.config import settings


logger = structlog.get_logger(__name__)


class EventType(Enum):
    """Pub/Sub event types for C# UI integration."""
    JOB_STARTED = "JOB_STARTED"
    IMAGE_PROCESSED = "IMAGE_PROCESSED"
    JOB_COMPLETE = "JOB_COMPLETE"
    JOB_FAILED = "JOB_FAILED"


class PubSubNotifier:
    """
    Publishes job status events to Pub/Sub for C# UI updates.
    
    The C# application subscribes to these events to:
    - Initialize and update the progress bar
    - Show completion notifications
    - Display error messages
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        topic_name: Optional[str] = None,
    ):
        """
        Initialize the Pub/Sub notifier.
        
        Args:
            project_id: GCP project ID (default from settings)
            topic_name: Pub/Sub topic name (default from settings)
        """
        self._project_id = project_id or settings.gcp_project_id
        self._topic_name = topic_name or settings.pubsub_topic
        self._publisher = pubsub_v1.PublisherClient()
        self._topic_path = self._publisher.topic_path(
            self._project_id, 
            self._topic_name
        )
        
        logger.info(
            "PubSubNotifier initialized",
            project_id=self._project_id,
            topic=self._topic_name,
        )

    def notify_job_started(
        self,
        job_id: str,
        total_images: int,
        input_path: str,
    ) -> None:
        """
        Notify that a job has started processing.
        
        Args:
            job_id: Unique job identifier
            total_images: Total number of images to process
            input_path: GCS path containing input images
        """
        self._publish_event(
            event_type=EventType.JOB_STARTED,
            job_id=job_id,
            payload={
                "total_images": total_images,
                "input_path": input_path,
            }
        )
        logger.info(
            "Published JOB_STARTED event",
            job_id=job_id,
            total_images=total_images,
        )

    def notify_image_processed(
        self,
        job_id: str,
        image_index: int,
        image_name: str,
        status: str,
        model_used: str,
        confidence: float,
        processing_time_ms: int,
    ) -> None:
        """
        Notify that an individual image has been processed.
        
        Args:
            job_id: Unique job identifier
            image_index: 1-based index of the processed image
            image_name: Filename of the processed image
            status: Processing status ('success' or 'failed')
            model_used: Model that processed the image
            confidence: Confidence score of the result
            processing_time_ms: Processing time in milliseconds
        """
        self._publish_event(
            event_type=EventType.IMAGE_PROCESSED,
            job_id=job_id,
            payload={
                "image_index": image_index,
                "image_name": image_name,
                "status": status,
                "model_used": model_used,
                "confidence": confidence,
                "processing_time_ms": processing_time_ms,
            }
        )
        logger.debug(
            "Published IMAGE_PROCESSED event",
            job_id=job_id,
            image_index=image_index,
            status=status,
        )

    def notify_job_complete(
        self,
        job_id: str,
        total_processed: int,
        successful: int,
        failed: int,
        output_path: str,
        total_time_seconds: float,
    ) -> None:
        """
        Notify that a job has completed successfully.
        
        Args:
            job_id: Unique job identifier
            total_processed: Total images processed
            successful: Number of successful images
            failed: Number of failed images
            output_path: GCS path containing output files
            total_time_seconds: Total processing time in seconds
        """
        self._publish_event(
            event_type=EventType.JOB_COMPLETE,
            job_id=job_id,
            payload={
                "total_processed": total_processed,
                "successful": successful,
                "failed": failed,
                "output_path": output_path,
                "total_time_seconds": round(total_time_seconds, 2),
            }
        )
        logger.info(
            "Published JOB_COMPLETE event",
            job_id=job_id,
            successful=successful,
            failed=failed,
        )

    def notify_job_failed(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        images_processed_before_failure: int = 0,
    ) -> None:
        """
        Notify that a job has failed.
        
        Args:
            job_id: Unique job identifier
            error_code: Error code category
            error_message: Human-readable error description
            images_processed_before_failure: Images processed before failure
        """
        self._publish_event(
            event_type=EventType.JOB_FAILED,
            job_id=job_id,
            payload={
                "error_code": error_code,
                "error_message": error_message,
                "images_processed_before_failure": images_processed_before_failure,
            }
        )
        logger.error(
            "Published JOB_FAILED event",
            job_id=job_id,
            error_code=error_code,
            error_message=error_message,
        )

    def _publish_event(
        self,
        event_type: EventType,
        job_id: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Publish an event to Pub/Sub.
        
        Args:
            event_type: Type of event
            job_id: Job identifier
            payload: Event-specific data
        """
        message = {
            "event_type": event_type.value,
            "job_id": job_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        
        # Serialize to JSON bytes
        message_bytes = json.dumps(message).encode("utf-8")
        
        # Publish asynchronously
        future = self._publisher.publish(
            self._topic_path,
            message_bytes,
            event_type=event_type.value,
            job_id=job_id,
        )
        
        # Wait for publish to complete (with timeout)
        try:
            future.result(timeout=10.0)
        except Exception as e:
            logger.error(
                "Failed to publish Pub/Sub message",
                event_type=event_type.value,
                error=str(e),
            )
            # Don't raise - notification failure shouldn't stop processing
