"""
Google Cloud Storage Client for BIQE HTR Pipeline.

Handles:
- Signed URL generation for client uploads
- Image download for worker processing
- Result upload to output bucket
- Signed URL generation for result downloads

Note: On Cloud Run, we use IAM signBlob API to sign URLs since
Compute Engine credentials don't have a private key for signing.
We need specific scopes for the IAM Credentials API.
"""

import datetime
import json
import logging
from pathlib import Path
from typing import Optional
from uuid import uuid4

import google.auth
from google.auth import compute_engine
from google.auth.transport import requests as google_auth_requests
from google.cloud import storage
from google.cloud.storage import Blob
from google.oauth2 import service_account

from src.config import settings

logger = logging.getLogger(__name__)

# Scopes needed for IAM signBlob API
IAM_SIGNING_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/iam",
]


class GCSClient:
    """
    GCS client for managing image uploads and result storage.
    
    Buckets:
    - Input: Client uploads images here
    - Output: Worker stores results here
    """
    
    UPLOAD_EXPIRY_MINUTES = 60  # Signed URLs valid for 1 hour
    DOWNLOAD_EXPIRY_MINUTES = 60 * 24  # Download URLs valid for 24 hours
    
    def __init__(
        self,
        project_id: str,
        input_bucket: Optional[str] = None,
        output_bucket: Optional[str] = None,
    ):
        """
        Initialize GCS client.
        
        Args:
            project_id: GCP project ID
            input_bucket: Bucket for image uploads (default from settings)
            output_bucket: Bucket for result storage (default from settings)
        """
        self._client = storage.Client(project=project_id)
        self._project_id = project_id
        
        # Use settings or override
        self._input_bucket_name = input_bucket or settings.gcs_input_bucket or f"{project_id}-htr-input"
        self._output_bucket_name = output_bucket or settings.gcs_output_bucket or f"{project_id}-htr-output"
        
        self._input_bucket = self._client.bucket(self._input_bucket_name)
        self._output_bucket = self._client.bucket(self._output_bucket_name)
        
        # Get service account email and scoped credentials for IAM-based signing
        self._service_account_email, self._signing_credentials = self._setup_signing_credentials()
        
        logger.info(
            f"GCS client initialized: input={self._input_bucket_name}, output={self._output_bucket_name}, sa={self._service_account_email}"
        )
    
    def _setup_signing_credentials(self) -> tuple[Optional[str], Optional[object]]:
        """
        Setup credentials for IAM-based URL signing.
        
        On Cloud Run, we need:
        1. Service account email to identify who is signing
        2. Credentials with IAM scope to call the signBlob API
        
        Returns:
            Tuple of (service_account_email, scoped_credentials)
        """
        try:
            credentials = self._client._credentials
            logger.info(f"GCS Client credentials type: {type(credentials)}")
            
            # Get or refresh credentials to get service account email
            if isinstance(credentials, compute_engine.Credentials):
                logger.info("Detected compute_engine.Credentials (Cloud Run)")
                auth_request = google_auth_requests.Request()
                credentials.refresh(auth_request)
                email = getattr(credentials, 'service_account_email', None)
                
                if email:
                    logger.info(f"Service account email: {email}")
                    # Create scoped credentials with IAM scope for signing
                    # Note: Compute Engine credentials already have cloud-platform scope
                    # We need to create IDTokenCredentials or use the access token approach
                    scoped_creds = compute_engine.Credentials(
                        service_account_email=email,
                        scopes=IAM_SIGNING_SCOPES,
                    )
                    scoped_creds.refresh(auth_request)
                    logger.info(f"Created scoped credentials with IAM scope, token: {scoped_creds.token[:20] if scoped_creds.token else 'None'}...")
                    return email, scoped_creds
                    
            # Service account credentials (local development)
            elif hasattr(credentials, 'service_account_email'):
                email = credentials.service_account_email
                logger.info(f"Using service account credentials: {email}")
                return email, credentials
            
            logger.warning(f"Could not setup signing credentials from {type(credentials)}")
            return None, None
        except Exception as e:
            logger.error(f"Error setting up signing credentials: {e}")
            return None, None
    
    @property
    def input_bucket_name(self) -> str:
        """Get input bucket name."""
        return self._input_bucket_name
    
    @property
    def output_bucket_name(self) -> str:
        """Get output bucket name."""
        return self._output_bucket_name
    
    def generate_upload_urls(
        self,
        batch_id: str,
        filenames: list[str],
    ) -> dict[str, dict]:
        """
        Generate signed upload URLs for a batch of files.
        
        Args:
            batch_id: Batch identifier (used as folder prefix)
            filenames: List of filenames to generate URLs for
            
        Returns:
            Dict mapping filename to {file_id, upload_url, gcs_path}
        """
        result = {}
        expiry = datetime.timedelta(minutes=self.UPLOAD_EXPIRY_MINUTES)
        
        for filename in filenames:
            file_id = str(uuid4())
            
            # Path: batches/{batch_id}/input/{file_id}_{filename}
            gcs_path = f"batches/{batch_id}/input/{file_id}_{filename}"
            blob = self._input_bucket.blob(gcs_path)
            
            # Generate signed URL for upload (PUT request)
            # Use service_account_email for IAM-based signing on Cloud Run
            try:
                logger.info(f"Generating signed URL for {filename}, service_account_email={self._service_account_email}")
                
                if self._service_account_email and self._signing_credentials:
                    # Cloud Run: use IAM signBlob API with scoped credentials
                    # Ensure credentials are refreshed to get a valid token with IAM scope
                    credentials = self._signing_credentials
                    if hasattr(credentials, 'refresh') and (not credentials.token or credentials.expired):
                        logger.info("Refreshing signing credentials...")
                        auth_request = google_auth_requests.Request()
                        credentials.refresh(auth_request)
                    
                    token = credentials.token
                    logger.info(f"Using scoped token (first 20 chars): {token[:20] if token else 'None'}...")
                    
                    # Don't specify content_type - let client use any type (image/jpeg, etc.)
                    upload_url = blob.generate_signed_url(
                        version="v4",
                        expiration=expiry,
                        method="PUT",
                        service_account_email=self._service_account_email,
                        access_token=token,
                    )
                else:
                    # Local dev with service account key
                    logger.info("Using local service account key signing")
                    # Don't specify content_type - let client use any type
                    upload_url = blob.generate_signed_url(
                        version="v4",
                        expiration=expiry,
                        method="PUT",
                    )
            except Exception as e:
                logger.error(f"Failed to generate signed URL for {filename}: {e}")
                raise
            
            result[filename] = {
                "file_id": file_id,
                "upload_url": upload_url,
                "gcs_path": f"gs://{self._input_bucket_name}/{gcs_path}",
            }
            
            logger.debug(f"Generated upload URL for {filename}: {gcs_path}")
        
        return result
    
    def download_image(self, gcs_path: str, max_retries: int = 3) -> bytes:
        """
        Download image from GCS with retry logic for SSL/timeout errors.
        
        v2.4.3: Increased timeout to 300s for large TIFF files (20-50MB).
        Added timeout exception handling.
        
        Args:
            gcs_path: Full GCS path (gs://bucket/path)
            max_retries: Maximum number of retry attempts for SSL errors
            
        Returns:
            Image bytes
        """
        import time
        from requests.exceptions import SSLError, Timeout, ReadTimeout, ConnectionError
        from google.api_core.exceptions import RetryError
        from httpx import TimeoutException as HTTPXTimeout
        
        bucket_name, blob_path = self._parse_gcs_path(gcs_path)
        
        last_error = None
        for attempt in range(max_retries):
            try:
                # Create fresh client and bucket for each attempt to avoid connection pool issues
                if attempt > 0:
                    logger.warning(f"GCS download retry {attempt + 1}/{max_retries} for {blob_path}")
                    time.sleep(2 ** attempt)  # Exponential backoff: 2s, 4s
                    # Force new connection by creating fresh client
                    from google.cloud import storage
                    fresh_client = storage.Client(project=self._project_id)
                    bucket = fresh_client.bucket(bucket_name)
                else:
                    bucket = self._client.bucket(bucket_name)
                
                blob = bucket.blob(blob_path)
                
                # v2.4.3: Increased timeout from 60s to 300s for large TIFF files (20-50MB)
                # Large TIFF files can take 2-5 minutes to download from GCS on slower Cloud Run instances
                image_data = blob.download_as_bytes(timeout=300)
                
                logger.debug(f"Downloaded {len(image_data)} bytes from {gcs_path}")
                return image_data
                
            except (SSLError, RetryError, Timeout, ReadTimeout, ConnectionError, HTTPXTimeout) as e:
                last_error = e
                error_msg = str(e)
                # Retry on SSL, timeout, and connection errors
                if any(keyword in error_msg for keyword in ["SSL", "UNEXPECTED_EOF", "Timeout", "timed out", "Connection"]):
                    logger.warning(f"SSL/Timeout error on attempt {attempt + 1}/{max_retries}: {error_msg[:100]}")
                    continue
                else:
                    # Other error, re-raise immediately
                    raise
            except Exception as e:
                # Catch any other timeout-related errors
                error_str = str(e)
                if any(keyword in error_str for keyword in ["SSL", "UNEXPECTED_EOF", "Timeout", "timed out", "exceeded"]):
                    last_error = e
                    logger.warning(f"GCS error on attempt {attempt + 1}/{max_retries}: {error_str[:150]}")
                    continue
                raise
        
        # All retries failed
        logger.error(f"GCS download failed after {max_retries} attempts: {gcs_path}")
        raise last_error
    
    def upload_result(
        self,
        batch_id: str,
        file_id: str,
        filename: str,
        result_data: dict,
    ) -> str:
        """
        Upload OCR result to output bucket.
        
        Args:
            batch_id: Batch identifier
            file_id: File identifier
            filename: Original filename
            result_data: OCR result dict (text, confidence, etc.)
            
        Returns:
            GCS path to result file
        """
        # Path: batches/{batch_id}/output/{file_id}_{filename}.json
        base_filename = Path(filename).stem
        gcs_path = f"batches/{batch_id}/output/{file_id}_{base_filename}.json"
        
        blob = self._output_bucket.blob(gcs_path)
        blob.upload_from_string(
            json.dumps(result_data, indent=2, ensure_ascii=False),
            content_type="application/json",
        )
        
        full_path = f"gs://{self._output_bucket_name}/{gcs_path}"
        logger.debug(f"Uploaded result to {full_path}")
        
        return full_path
    
    def upload_result_text(
        self,
        batch_id: str,
        file_id: str,
        filename: str,
        text_content: str,
    ) -> str:
        """
        Upload just the text content as a .txt file.
        
        Args:
            batch_id: Batch identifier
            file_id: File identifier
            filename: Original filename
            text_content: OCR text result
            
        Returns:
            GCS path to text file
        """
        # Path: batches/{batch_id}/output/{file_id}_{filename}.txt
        base_filename = Path(filename).stem
        gcs_path = f"batches/{batch_id}/output/{file_id}_{base_filename}.txt"
        
        blob = self._output_bucket.blob(gcs_path)
        blob.upload_from_string(
            text_content,
            content_type="text/plain; charset=utf-8",
        )
        
        full_path = f"gs://{self._output_bucket_name}/{gcs_path}"
        logger.debug(f"Uploaded text to {full_path}")
        
        return full_path
    
    def generate_download_urls(self, batch_id: str) -> list[dict]:
        """
        Generate signed download URLs for all results in a batch.
        
        Args:
            batch_id: Batch identifier
            
        Returns:
            List of {filename, json_url, text_url} dicts
        """
        expiry = datetime.timedelta(minutes=self.DOWNLOAD_EXPIRY_MINUTES)
        prefix = f"batches/{batch_id}/output/"
        
        blobs = list(self._output_bucket.list_blobs(prefix=prefix))
        
        results = {}
        for blob in blobs:
            # Extract base name from path
            name = Path(blob.name).stem  # file_id_originalname
            original_name = "_".join(name.split("_")[1:])  # Remove file_id prefix
            
            if original_name not in results:
                results[original_name] = {}
            
            try:
                if self._service_account_email and self._signing_credentials:
                    # Ensure signing credentials are fresh
                    if hasattr(self._signing_credentials, 'refresh') and (not self._signing_credentials.token or self._signing_credentials.expired):
                        auth_request = google_auth_requests.Request()
                        self._signing_credentials.refresh(auth_request)
                    
                    url = blob.generate_signed_url(
                        version="v4",
                        expiration=expiry,
                        method="GET",
                        service_account_email=self._service_account_email,
                        access_token=self._signing_credentials.token,
                    )
                else:
                    url = blob.generate_signed_url(
                        version="v4",
                        expiration=expiry,
                        method="GET",
                    )
                
                if blob.name.endswith(".json"):
                    results[original_name]["json_url"] = url
                elif blob.name.endswith(".txt"):
                    results[original_name]["text_url"] = url
            except Exception as e:
                logger.warning(f"Failed to generate download URL for {blob.name}: {e}")
        
        return [
            {"filename": name, **urls}
            for name, urls in results.items()
        ]
    
    def check_file_exists(self, gcs_path: str) -> bool:
        """
        Check if a file exists in GCS.
        
        Args:
            gcs_path: Full GCS path (gs://bucket/path)
            
        Returns:
            True if file exists
        """
        bucket_name, blob_path = self._parse_gcs_path(gcs_path)
        bucket = self._client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        return blob.exists()
    
    def _parse_gcs_path(self, gcs_path: str) -> tuple[str, str]:
        """
        Parse GCS path into bucket and blob path.
        
        Args:
            gcs_path: Path like 'gs://bucket/path' or just 'path'
            
        Returns:
            Tuple of (bucket_name, blob_path)
        """
        if gcs_path.startswith("gs://"):
            path = gcs_path[5:]  # Remove 'gs://'
            parts = path.split("/", 1)
            return parts[0], parts[1] if len(parts) > 1 else ""
        else:
            # Relative path, assume input bucket
            return self._input_bucket_name, gcs_path
    
    # =========================================================================
    # v3.0: Single-Image Job Methods
    # =========================================================================
    
    def blob_exists(self, blob_path: str, bucket_type: str = "input") -> bool:
        """
        Check if a blob exists in the specified bucket.
        
        v3.1: Added for verifying uploads succeeded before creating jobs.
        
        Args:
            blob_path: Path within bucket (e.g., "jobs/{job_id}/input/file.tif")
            bucket_type: "input" or "output" bucket
            
        Returns:
            True if blob exists, False otherwise
        """
        try:
            bucket = self._input_bucket if bucket_type == "input" else self._output_bucket
            blob = bucket.blob(blob_path)
            return blob.exists()
        except Exception as e:
            logger.warning(f"Error checking blob existence: {e}")
            return False
    
    def upload_to_input(
        self, 
        blob_path: str, 
        image_data: bytes, 
        content_type: str = "image/jpeg",
        max_retries: int = 3,
        verify_upload: bool = True,
    ) -> str:
        """
        Upload image data to input bucket with retry and verification.
        
        v3.0: For single-image job API where HTR sends preprocessed image.
        v3.1: Added retry logic and upload verification to ensure 0% failure rate.
        
        Args:
            blob_path: Path within input bucket (e.g., "jobs/{job_id}/input.jpg")
            image_data: Raw image bytes
            content_type: MIME type
            max_retries: Maximum retry attempts (default 3)
            verify_upload: Verify blob exists after upload (default True)
            
        Returns:
            Full GCS path (gs://bucket/path)
            
        Raises:
            RuntimeError: If upload fails after all retries or verification fails
        """
        import time
        
        blob = self._input_bucket.blob(blob_path)
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                # Upload with explicit timeout
                blob.upload_from_string(
                    image_data, 
                    content_type=content_type,
                    timeout=120,  # 2 minute timeout for large files
                )
                
                # Verify upload succeeded
                if verify_upload:
                    # Give GCS a moment for consistency
                    time.sleep(0.1)
                    if not blob.exists():
                        raise RuntimeError(f"Upload verification failed: blob does not exist after upload")
                    
                    # Verify file size matches
                    blob.reload()
                    if blob.size != len(image_data):
                        raise RuntimeError(
                            f"Upload verification failed: size mismatch "
                            f"(uploaded={blob.size}, expected={len(image_data)})"
                        )
                
                full_path = f"gs://{self._input_bucket_name}/{blob_path}"
                logger.info(f"Upload verified: {len(image_data)} bytes to {full_path}")
                return full_path
                
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Upload attempt {attempt}/{max_retries} failed: {e}"
                )
                if attempt < max_retries:
                    # Exponential backoff: 1s, 2s, 4s
                    sleep_time = 2 ** (attempt - 1)
                    logger.info(f"Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
        
        # All retries exhausted
        raise RuntimeError(
            f"Failed to upload after {max_retries} attempts: {last_error}"
        )
    
    def upload_job_result_text(self, job_id: str, filename: str, text_content: str) -> str:
        """
        Upload transcription text for a single job.
        
        v3.0: Stores result in jobs/{job_id}/output.txt
        
        Returns:
            Full GCS path
        """
        from pathlib import Path
        base_filename = Path(filename).stem
        blob_path = f"jobs/{job_id}/{base_filename}.txt"
        
        blob = self._output_bucket.blob(blob_path)
        blob.upload_from_string(
            text_content,
            content_type="text/plain; charset=utf-8",
        )
        
        full_path = f"gs://{self._output_bucket_name}/{blob_path}"
        logger.debug(f"Uploaded job result text to {full_path}")
        return full_path
    
    def delete_job_input(self, job_id: str) -> None:
        """
        Delete input image for a job to save storage costs.
        
        v3.0: Called after successful processing.
        """
        # List all blobs in jobs/{job_id}/input/
        prefix = f"jobs/{job_id}/input/"
        blobs = list(self._input_bucket.list_blobs(prefix=prefix))
        
        for blob in blobs:
            blob.delete()
            logger.debug(f"Deleted input: gs://{self._input_bucket_name}/{blob.name}")
    
    def generate_signed_url(self, gcs_path: str, method: str = "GET", expiry_minutes: int = None) -> str:
        """
        Generate a signed URL for accessing a file.
        
        Args:
            gcs_path: Full GCS path (gs://bucket/path)
            method: HTTP method (GET or PUT)
            expiry_minutes: URL validity in minutes (default: DOWNLOAD_EXPIRY_MINUTES)
            
        Returns:
            Signed URL string
        """
        bucket_name, blob_path = self._parse_gcs_path(gcs_path)
        bucket = self._client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        
        expiry = datetime.timedelta(minutes=expiry_minutes or self.DOWNLOAD_EXPIRY_MINUTES)
        
        try:
            if self._service_account_email and self._signing_credentials:
                # Ensure signing credentials are fresh
                if hasattr(self._signing_credentials, 'refresh') and (
                    not self._signing_credentials.token or self._signing_credentials.expired
                ):
                    auth_request = google_auth_requests.Request()
                    self._signing_credentials.refresh(auth_request)
                
                url = blob.generate_signed_url(
                    version="v4",
                    expiration=expiry,
                    method=method.upper(),
                    service_account_email=self._service_account_email,
                    access_token=self._signing_credentials.token,
                )
            else:
                url = blob.generate_signed_url(
                    version="v4",
                    expiration=expiry,
                    method=method.upper(),
                )
            
            logger.debug(f"Generated signed URL for {gcs_path}")
            return url
            
        except Exception as e:
            logger.error(f"Failed to generate signed URL for {gcs_path}: {e}")
            raise


# Singleton instance
_gcs_client: Optional[GCSClient] = None


def get_gcs_client(project_id: Optional[str] = None) -> GCSClient:
    """Get or create GCS client singleton."""
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = GCSClient(project_id or settings.gcp_project_id)
    return _gcs_client
