"""
Atomic Storage Manager - The "0kb Fix" Implementation.

This module implements the critical "Atomic Write & Verify" pattern
that guarantees zero data loss when writing to Google Cloud Storage.

ARCHITECTURE CONSTRAINT: NEVER write directly to GCS without verification.

The Pattern:
1. Write to LOCAL container storage first
2. VERIFY file size > 0 bytes
3. Upload to GCS
4. VERIFY GCS checksum matches local file
5. ONLY THEN mark as success
"""

import hashlib
import json
import os
import shutil
import structlog
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from google.cloud import storage
from google.cloud.storage import Blob

from src.config import settings


logger = structlog.get_logger(__name__)


class StorageError(Exception):
    """Base exception for storage operations."""
    pass


class EmptyFileError(StorageError):
    """Raised when a file is empty (0 bytes)."""
    pass


class ChecksumMismatchError(StorageError):
    """Raised when upload verification fails."""
    pass


class AtomicStorageManager:
    """
    Manages atomic file operations with verification.
    
    Implements the "0kb Fix" pattern to guarantee data integrity
    when uploading to Google Cloud Storage.
    """

    def __init__(
        self,
        input_bucket: Optional[str] = None,
        output_bucket: Optional[str] = None,
        local_temp_dir: Optional[str] = None,
    ):
        """
        Initialize the storage manager.
        
        Args:
            input_bucket: GCS bucket for input files (default from settings)
            output_bucket: GCS bucket for output files (default from settings)
            local_temp_dir: Local directory for temporary files
        """
        self._client = storage.Client(project=settings.gcp_project_id)
        self._input_bucket = input_bucket or settings.gcs_input_bucket
        self._output_bucket = output_bucket or settings.gcs_output_bucket
        self._local_temp_dir = Path(local_temp_dir or settings.local_temp_dir)
        
        # Ensure temp directory exists
        self._local_temp_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(
            "AtomicStorageManager initialized",
            input_bucket=self._input_bucket,
            output_bucket=self._output_bucket,
            local_temp_dir=str(self._local_temp_dir),
        )

    def download_image(self, gcs_path: str) -> Path:
        """
        Download image from GCS to local storage.
        
        Args:
            gcs_path: Full GCS path (gs://bucket/path) or relative path
            
        Returns:
            Local file path
        """
        # Parse GCS path
        bucket_name, blob_path = self._parse_gcs_path(gcs_path)
        
        # Generate local path
        local_path = self._local_temp_dir / f"input_{uuid4().hex[:8]}_{Path(blob_path).name}"
        
        # Download
        bucket = self._client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(str(local_path))
        
        # Verify download
        if not local_path.exists() or local_path.stat().st_size == 0:
            raise EmptyFileError(f"Downloaded file is empty: {gcs_path}")
        
        logger.debug(
            "Downloaded image from GCS",
            gcs_path=gcs_path,
            local_path=str(local_path),
            size_bytes=local_path.stat().st_size,
        )
        
        return local_path

    def atomic_write_json(
        self,
        data: dict[str, Any],
        destination_path: str,
        job_id: Optional[str] = None,
    ) -> str:
        """
        Atomically write JSON data to GCS with full verification.
        
        THIS IS THE CORE "0kb FIX" IMPLEMENTATION.
        
        Args:
            data: Dictionary to serialize as JSON
            destination_path: Relative path in output bucket
            job_id: Optional job ID for organizing outputs
            
        Returns:
            Full GCS path of uploaded file
            
        Raises:
            EmptyFileError: If local file is empty after write
            ChecksumMismatchError: If upload verification fails
        """
        # Step 1: Generate unique local file path
        local_filename = f"output_{uuid4().hex[:8]}.json"
        local_path = self._local_temp_dir / local_filename
        
        logger.debug("Starting atomic write", destination=destination_path)
        
        try:
            # Step 2: Write to LOCAL container storage first
            json_content = json.dumps(data, indent=2, ensure_ascii=False)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(json_content)
                f.flush()
                os.fsync(f.fileno())  # Force kernel buffer flush
            
            # Step 3: VERIFY local file size > 0 (THE CRITICAL CHECK)
            local_size = os.path.getsize(local_path)
            if local_size == 0:
                raise EmptyFileError(
                    f"Local file is empty after write: {local_path}"
                )
            
            logger.debug(
                "Local file written and verified",
                local_path=str(local_path),
                size_bytes=local_size,
            )
            
            # Step 4: Calculate local file checksum (MD5)
            local_md5 = self._calculate_md5(local_path)
            
            # Step 5: Upload to GCS
            bucket = self._client.bucket(self._output_bucket)
            
            # Construct full blob path
            if job_id:
                blob_path = f"{job_id}/{destination_path}"
            else:
                blob_path = destination_path
            
            blob = bucket.blob(blob_path)
            blob.upload_from_filename(str(local_path))
            
            # Step 6: VERIFY GCS checksum matches local file
            blob.reload()  # Refresh metadata from GCS
            gcs_md5 = blob.md5_hash
            
            # GCS returns base64-encoded MD5, convert local for comparison
            import base64
            local_md5_b64 = base64.b64encode(
                bytes.fromhex(local_md5)
            ).decode("utf-8")
            
            if gcs_md5 != local_md5_b64:
                raise ChecksumMismatchError(
                    f"Checksum mismatch! Local: {local_md5_b64}, GCS: {gcs_md5}"
                )
            
            full_gcs_path = f"gs://{self._output_bucket}/{blob_path}"
            
            logger.info(
                "Atomic write completed successfully",
                gcs_path=full_gcs_path,
                size_bytes=local_size,
                checksum_verified=True,
            )
            
            # Step 7: ONLY NOW return success
            return full_gcs_path
            
        finally:
            # Cleanup local temp file
            if local_path.exists():
                local_path.unlink()

    def list_images_in_path(self, gcs_path: str) -> list[str]:
        """
        List all image files in a GCS path.
        
        Args:
            gcs_path: GCS path to list (gs://bucket/prefix or just prefix)
            
        Returns:
            List of full GCS paths to images
        """
        bucket_name, prefix = self._parse_gcs_path(gcs_path)
        
        # Ensure prefix ends with /
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        
        bucket = self._client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=prefix)
        
        image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
        images = []
        
        for blob in blobs:
            suffix = Path(blob.name).suffix.lower()
            if suffix in image_extensions:
                images.append(f"gs://{bucket_name}/{blob.name}")
        
        logger.info(
            "Listed images in GCS path",
            gcs_path=gcs_path,
            image_count=len(images),
        )
        
        return images

    def cleanup_temp_files(self):
        """Remove all temporary files from local storage."""
        if self._local_temp_dir.exists():
            for f in self._local_temp_dir.iterdir():
                if f.is_file():
                    f.unlink()
            logger.debug("Cleaned up temp files", directory=str(self._local_temp_dir))

    def _parse_gcs_path(self, gcs_path: str) -> tuple[str, str]:
        """
        Parse a GCS path into bucket and blob path.
        
        Args:
            gcs_path: Path like 'gs://bucket/path' or just 'path'
            
        Returns:
            Tuple of (bucket_name, blob_path)
        """
        if gcs_path.startswith("gs://"):
            # Full GCS path
            path = gcs_path[5:]  # Remove 'gs://'
            parts = path.split("/", 1)
            bucket_name = parts[0]
            blob_path = parts[1] if len(parts) > 1 else ""
        else:
            # Relative path, use input bucket
            bucket_name = self._input_bucket
            blob_path = gcs_path
        
        return bucket_name, blob_path

    def _calculate_md5(self, file_path: Path) -> str:
        """Calculate MD5 hash of a file."""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
