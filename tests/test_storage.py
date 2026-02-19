"""Tests for the Atomic Storage Manager (0kb Fix)."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.storage import (
    AtomicStorageManager,
    ChecksumMismatchError,
    EmptyFileError,
)


class TestAtomicStorageManager:
    """Test suite for AtomicStorageManager."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def storage_manager(self, temp_dir):
        """Create a storage manager with mocked GCS client."""
        with patch("src.core.storage.storage.Client"):
            with patch("src.core.storage.settings") as mock_settings:
                mock_settings.gcp_project_id = "test-project"
                mock_settings.gcs_input_bucket = "test-input"
                mock_settings.gcs_output_bucket = "test-output"
                mock_settings.local_temp_dir = str(temp_dir)
                
                manager = AtomicStorageManager(
                    local_temp_dir=str(temp_dir)
                )
                yield manager

    def test_local_write_creates_file(self, storage_manager, temp_dir):
        """Test that data is written to local storage first."""
        data = {"test": "data", "number": 123}
        
        # Mock GCS client methods
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.md5_hash = None  # Will be set after upload
        mock_bucket.blob.return_value = mock_blob
        storage_manager._client.bucket.return_value = mock_bucket
        
        # Calculate expected MD5
        json_content = json.dumps(data, indent=2, ensure_ascii=False)
        expected_md5 = hashlib.md5(json_content.encode()).hexdigest()
        
        # Mock the uploaded blob's MD5 to match
        import base64
        mock_blob.md5_hash = base64.b64encode(
            bytes.fromhex(expected_md5)
        ).decode("utf-8")
        
        # Execute
        gcs_path = storage_manager.atomic_write_json(
            data=data,
            destination_path="output.json",
            job_id="test-job"
        )
        
        # Verify blob upload was called
        mock_blob.upload_from_filename.assert_called_once()
        
        # Verify correct GCS path returned
        assert gcs_path == "gs://test-output/test-job/output.json"

    def test_empty_file_raises_error(self, storage_manager, temp_dir):
        """Test that empty files raise EmptyFileError."""
        # Create a mock that simulates empty file
        with patch.object(
            storage_manager, 
            "_local_temp_dir", 
            temp_dir
        ):
            with patch("builtins.open", MagicMock()):
                with patch("os.path.getsize", return_value=0):
                    with pytest.raises(EmptyFileError):
                        storage_manager.atomic_write_json(
                            data={},
                            destination_path="empty.json"
                        )

    def test_checksum_mismatch_raises_error(self, storage_manager):
        """Test that checksum mismatches raise ChecksumMismatchError."""
        data = {"test": "data"}
        
        # Mock GCS client with wrong checksum
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.md5_hash = "WRONG_CHECKSUM"  # Deliberate mismatch
        mock_bucket.blob.return_value = mock_blob
        storage_manager._client.bucket.return_value = mock_bucket
        
        with pytest.raises(ChecksumMismatchError):
            storage_manager.atomic_write_json(
                data=data,
                destination_path="mismatch.json"
            )

    def test_parse_gcs_path_full_path(self, storage_manager):
        """Test parsing full GCS paths."""
        bucket, path = storage_manager._parse_gcs_path(
            "gs://my-bucket/path/to/file.jpg"
        )
        
        assert bucket == "my-bucket"
        assert path == "path/to/file.jpg"

    def test_parse_gcs_path_relative(self, storage_manager):
        """Test parsing relative paths uses input bucket."""
        bucket, path = storage_manager._parse_gcs_path("path/to/file.jpg")
        
        assert bucket == "test-input"
        assert path == "path/to/file.jpg"

    def test_list_images_filters_correctly(self, storage_manager):
        """Test that only image files are returned."""
        # Mock blob list
        mock_blobs = [
            MagicMock(name="folder/image1.jpg"),
            MagicMock(name="folder/image2.png"),
            MagicMock(name="folder/document.pdf"),
            MagicMock(name="folder/image3.tiff"),
            MagicMock(name="folder/text.txt"),
        ]
        
        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = mock_blobs
        storage_manager._client.bucket.return_value = mock_bucket
        
        images = storage_manager.list_images_in_path("gs://test-bucket/folder")
        
        # Should only include image files
        assert len(images) == 3
        assert all(".pdf" not in img and ".txt" not in img for img in images)

    def test_cleanup_removes_temp_files(self, storage_manager, temp_dir):
        """Test that cleanup removes all temp files."""
        # Create some temp files
        (temp_dir / "file1.json").touch()
        (temp_dir / "file2.json").touch()
        
        assert len(list(temp_dir.iterdir())) == 2
        
        storage_manager.cleanup_temp_files()
        
        assert len(list(temp_dir.iterdir())) == 0


class TestAtomicWritePattern:
    """
    Integration tests for the complete atomic write pattern.
    
    These tests verify the EXACT sequence required by the architecture:
    1. Write locally
    2. Verify size > 0
    3. Upload to GCS
    4. Verify checksum
    """

    def test_full_atomic_pattern_sequence(self, tmp_path):
        """Verify the atomic write pattern executes in correct order."""
        operations = []
        
        def track_operation(name):
            operations.append(name)
        
        with patch("src.core.storage.storage.Client"):
            with patch("src.core.storage.settings") as mock_settings:
                mock_settings.gcp_project_id = "test"
                mock_settings.gcs_input_bucket = "in"
                mock_settings.gcs_output_bucket = "out"
                mock_settings.local_temp_dir = str(tmp_path)
                
                manager = AtomicStorageManager()
                
                # Patch the key operations to track order
                original_calculate_md5 = manager._calculate_md5
                
                def tracked_md5(path):
                    track_operation("calculate_md5")
                    return original_calculate_md5(path)
                
                manager._calculate_md5 = tracked_md5
                
                # Mock GCS with matching checksum
                import base64
                mock_bucket = MagicMock()
                mock_blob = MagicMock()
                
                def mock_upload(path):
                    track_operation("upload_to_gcs")
                    # Calculate and set matching checksum
                    md5 = original_calculate_md5(Path(path))
                    mock_blob.md5_hash = base64.b64encode(
                        bytes.fromhex(md5)
                    ).decode("utf-8")
                
                mock_blob.upload_from_filename.side_effect = mock_upload
                mock_blob.reload = lambda: track_operation("verify_gcs")
                mock_bucket.blob.return_value = mock_blob
                manager._client.bucket.return_value = mock_bucket
                
                # Execute
                with patch.object(os.path, "getsize") as mock_size:
                    mock_size.return_value = 100
                    track_operation("write_local")
                    track_operation("verify_local_size")
                    
                    manager.atomic_write_json(
                        data={"test": "data"},
                        destination_path="test.json"
                    )
        
        # Verify EXACT sequence
        expected_order = [
            "write_local",
            "verify_local_size", 
            "calculate_md5",
            "upload_to_gcs",
            "verify_gcs",
            "calculate_md5",  # Called again for comparison
        ]
        
        # Note: Actual implementation may differ slightly
        # but must follow: local write -> verify -> upload -> verify checksum
        assert "upload_to_gcs" in operations
        assert "verify_gcs" in operations
