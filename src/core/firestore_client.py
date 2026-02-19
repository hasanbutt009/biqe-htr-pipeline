"""
Firestore client for batch job tracking.

Stores job status, progress, and results metadata.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from google.cloud import firestore

logger = logging.getLogger(__name__)


class BatchStatus:
    """Batch job status constants."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class FileStatus:
    """Individual file status constants."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class FirestoreClient:
    """
    Firestore client for batch job tracking.
    
    Collections:
    - batches/{batch_id}: Batch metadata and status
    - batches/{batch_id}/files/{file_id}: Individual file status
    """
    
    def __init__(self, project_id: str):
        """Initialize Firestore client."""
        self._db = firestore.Client(project=project_id)
        self._batches = self._db.collection("batches")
        logger.info(f"Firestore client initialized for project: {project_id}")
    
    def create_batch(
        self,
        batch_id: str,
        total_files: int,
        params: dict,
    ) -> dict:
        """
        Create a new batch job.
        
        Args:
            batch_id: Unique batch identifier
            total_files: Total number of files in batch
            params: OCR parameters (temperature, model, etc.)
            
        Returns:
            Batch document data
        """
        now = datetime.now(timezone.utc)
        batch_data = {
            "batch_id": batch_id,
            "status": BatchStatus.PENDING,
            "total_files": total_files,
            "completed_files": 0,
            "failed_files": 0,
            "params": params,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        
        self._batches.document(batch_id).set(batch_data)
        logger.info(f"Created batch: {batch_id} with {total_files} files")
        return batch_data
    
    def get_batch(self, batch_id: str) -> Optional[dict]:
        """Get batch status."""
        doc = self._batches.document(batch_id).get()
        if doc.exists:
            return doc.to_dict()
        return None
    
    def update_batch_status(self, batch_id: str, status: str) -> None:
        """Update batch status."""
        self._batches.document(batch_id).update({
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        })
    
    def add_file(
        self,
        batch_id: str,
        file_id: str,
        filename: str,
        gcs_input_path: Optional[str] = None,
        gcs_output_path: Optional[str] = None,
    ) -> None:
        """
        Add a file to the batch.
        
        Args:
            batch_id: Batch identifier
            file_id: Unique file identifier
            filename: Original filename
            gcs_input_path: Optional GCS path where image is/will be stored
            gcs_output_path: Optional GCS path where result will be stored
        """
        self._batches.document(batch_id).collection("files").document(file_id).set({
            "file_id": file_id,
            "filename": filename,
            "status": FileStatus.PENDING,
            "confidence": None,
            "model_used": None,
            "error": None,
            "text_preview": None,
            "gcs_input_path": gcs_input_path,
            "gcs_output_path": gcs_output_path,
            "created_at": datetime.now(timezone.utc),
        })
    
    def update_file_status(
        self,
        batch_id: str,
        file_id: str,
        status: str,
        confidence: Optional[float] = None,
        model_used: Optional[str] = None,
        text_preview: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Update file status after processing.
        
        Also updates the batch counters. Counters are only incremented when
        a file transitions FROM pending/processing TO completed/failed.
        This prevents overcounting when messages are redelivered.
        """
        file_ref = self._batches.document(batch_id).collection("files").document(file_id)
        batch_ref = self._batches.document(batch_id)
        
        # Use transaction for both file update and counter update to ensure atomicity
        # IMPORTANT: All reads must happen BEFORE any writes in a Firestore transaction
        @firestore.transactional
        def update_with_counter_guard(transaction):
            # STEP 1: Read ALL documents FIRST (before any writes)
            file_doc = file_ref.get(transaction=transaction)
            batch_doc = batch_ref.get(transaction=transaction)
            
            old_status = file_doc.to_dict().get("status") if file_doc.exists else None
            batch_data = batch_doc.to_dict() if batch_doc.exists else {}
            
            # STEP 2: Prepare file update data
            file_data = {
                "status": status,
                "updated_at": datetime.now(timezone.utc),
            }
            if confidence is not None:
                file_data["confidence"] = confidence
            if model_used is not None:
                file_data["model_used"] = model_used
            if text_preview is not None:
                file_data["text_preview"] = text_preview[:200]  # First 200 chars
            if error is not None:
                file_data["error"] = error
            
            # STEP 3: Determine if we should update counters
            # Only update batch counters if transitioning FROM pending/processing
            # This prevents overcounting when same file is processed multiple times
            should_increment = old_status in [FileStatus.PENDING, FileStatus.PROCESSING]
            
            # STEP 4: Prepare batch updates (if needed)
            batch_updates = {"updated_at": datetime.now(timezone.utc)}
            
            if should_increment:
                if status == FileStatus.COMPLETED:
                    batch_updates["completed_files"] = batch_data.get("completed_files", 0) + 1
                    logger.debug(f"Incrementing completed_files for batch {batch_id}")
                elif status == FileStatus.FAILED:
                    batch_updates["failed_files"] = batch_data.get("failed_files", 0) + 1
                    logger.debug(f"Incrementing failed_files for batch {batch_id}")
                
                # Check if batch is complete
                total = batch_data.get("total_files", 0)
                completed = batch_updates.get("completed_files", batch_data.get("completed_files", 0))
                failed = batch_updates.get("failed_files", batch_data.get("failed_files", 0))
                
                if completed + failed >= total:
                    batch_updates["status"] = BatchStatus.COMPLETED
                    batch_updates["completed_at"] = datetime.now(timezone.utc)
                    logger.info(f"Batch {batch_id} completed: {completed} completed, {failed} failed")
            else:
                logger.debug(f"Skipping counter update: file {file_id} already in {old_status}")
            
            # STEP 5: Write ALL updates AFTER all reads
            transaction.update(file_ref, file_data)
            transaction.update(batch_ref, batch_updates)
        
        transaction = self._db.transaction()
        update_with_counter_guard(transaction)
    
    def get_batch_files(self, batch_id: str) -> list[dict]:
        """Get all files in a batch."""
        files = self._batches.document(batch_id).collection("files").stream()
        return [f.to_dict() for f in files]
    
    def get_batch_progress(self, batch_id: str) -> dict:
        """
        Get batch progress summary.
        
        Calculates counts from actual file documents for accuracy,
        rather than relying on cached counters (which could be stale
        due to race conditions or redelivered messages).
        
        Returns:
            Dict with status, total, completed, failed, progress percentage
        """
        batch = self.get_batch(batch_id)
        if not batch:
            return None
        
        # Count actual file statuses from documents (accurate source of truth)
        files = self._batches.document(batch_id).collection("files").stream()
        
        completed = 0
        failed = 0
        pending = 0
        processing = 0
        recent_errors = []  # Collect most recent errors for client visibility
        
        for f in files:
            file_data = f.to_dict()
            status = file_data.get("status")
            if status == FileStatus.COMPLETED:
                completed += 1
            elif status == FileStatus.FAILED:
                failed += 1
                # Collect error info for failed files
                error = file_data.get("error", "Unknown error")
                filename = file_data.get("filename", "unknown")
                # Check if it's a quota/429 error
                is_quota_error = "429" in error or "Resource exhausted" in error or "quota" in error.lower()
                recent_errors.append({
                    "filename": filename,
                    "error": error[:200],  # Truncate long errors
                    "is_quota_error": is_quota_error,
                })
            elif status == FileStatus.PROCESSING:
                processing += 1
            else:  # PENDING
                pending += 1
        
        total = batch.get("total_files", 0)
        
        # Determine batch status based on file counts
        if completed + failed >= total and total > 0:
            status = BatchStatus.COMPLETED
        elif processing > 0 or completed > 0 or failed > 0:
            status = BatchStatus.PROCESSING
        else:
            status = batch.get("status")
        
        # Count quota errors specifically
        quota_errors = sum(1 for e in recent_errors if e.get("is_quota_error"))
        
        return {
            "batch_id": batch_id,
            "status": status,
            "total_files": total,
            "completed_files": completed,
            "failed_files": failed,
            "pending_files": pending + processing,  # Pending + in-flight
            "progress_percentage": (completed + failed) / total * 100 if total > 0 else 0,
            "quota_errors": quota_errors,
            "recent_errors": recent_errors[-5:] if recent_errors else [],  # Last 5 errors
        }
    
    # =========================================================================
    # v3.0: Single-Image Job Management
    # =========================================================================
    
    def create_job(
        self,
        job_id: str,
        filename: str,
        gcs_input_path: Optional[str],
        params: dict,
        file_size_bytes: int = 0,
    ) -> None:
        """
        Create a new individual job document.
        
        v3.0: For single-image processing (HTR architecture compatibility).
        v3.x: gcs_input_path can be None for V3 direct processing (no GCS).
        """
        now = datetime.now(timezone.utc)
        job_doc = {
            "job_id": job_id,
            "filename": filename,
            "status": BatchStatus.PROCESSING,
            "gcs_input_path": gcs_input_path,
            "params": params,
            "file_size_bytes": file_size_bytes,
            "created_at": now,
            "started_processing_at": None,
            "completed_at": None,
            "failed_at": None,
            # Results (populated later)
            "confidence": None,
            "model_used": None,
            "text_content": None,
            "text_url": None,
            "error": None,
            "retry_count": 0,
            "processing_time_ms": None,
        }
        
        # Store in 'jobs' collection (separate from 'batches')
        self._db.collection("jobs").document(job_id).set(job_doc)
        logger.info(f"Created job: {job_id}, filename={filename}")
    
    def get_job(self, job_id: str) -> Optional[dict]:
        """Get job document by ID."""
        doc =self._db.collection("jobs").document(job_id).get()
        if doc.exists:
            return doc.to_dict()
        return None
    
    def update_job_status(self, job_id: str, status: str, error: str = None) -> None:
        """
        Update job status.
        
        Raises:
            ValueError: If job document doesn't exist (indicates a bug in job creation flow)
        """
        # First, verify the job exists - if it doesn't, something is wrong in our flow
        doc_ref = self._db.collection("jobs").document(job_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.error(f"Job {job_id} not found when trying to update status to {status} - this indicates a bug in job creation")
            raise ValueError(f"Job {job_id} does not exist - cannot update status")
        
        update_data = {"status": status}
        if error:
            update_data["error"] = error
        if status == BatchStatus.FAILED:
            update_data["failed_at"] = datetime.now(timezone.utc)
        
        doc_ref.update(update_data)
        logger.info(f"Job {job_id} status updated: {status}")
    
    def update_job_result(
        self,
        job_id: str,
        text_content: str,
        confidence: float,
        model_used: str,
        text_url: Optional[str],
        processing_time_ms: int,
    ) -> None:
        """
        Update job with OCR results.
        
        v3.0: Marks job as completed and stores all result data.
        v3.x: text_url can be None for V3 direct processing (no GCS).
        
        Raises:
            ValueError: If job document doesn't exist (indicates a bug in job creation flow)
        """
        # First, verify the job exists - if it doesn't, something is wrong in our flow
        doc_ref = self._db.collection("jobs").document(job_id)
        doc = doc_ref.get()
        if not doc.exists:
            logger.error(f"Job {job_id} not found when trying to update result - this indicates a bug in job creation")
            raise ValueError(f"Job {job_id} does not exist - cannot update result")
        
        now = datetime.now(timezone.utc)
        update_data = {
            "status": BatchStatus.COMPLETED,
            "text_content": text_content,
            "confidence": confidence,
            "model_used": model_used,
            "text_url": text_url,
            "processing_time_ms": processing_time_ms,
            "completed_at": now,
        }
        
        doc_ref.update(update_data)
        logger.info(f"Job {job_id} completed: confidence={confidence:.2f}, model={model_used}")


# Singleton instance
_firestore_client: Optional[FirestoreClient] = None


def get_firestore_client(project_id: str) -> FirestoreClient:
    """Get or create Firestore client singleton."""
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = FirestoreClient(project_id)
    return _firestore_client
