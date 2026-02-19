"""Tests for the Pub/Sub Notifier."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.core.notifier import EventType, PubSubNotifier


class TestPubSubNotifier:
    """Test suite for PubSubNotifier."""

    @pytest.fixture
    def mock_publisher(self):
        """Mock the Pub/Sub publisher client."""
        with patch("src.core.notifier.pubsub_v1.PublisherClient") as mock:
            mock_instance = MagicMock()
            mock_instance.topic_path.return_value = "projects/test/topics/test-topic"
            mock.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def notifier(self, mock_publisher):
        """Create a notifier with mocked publisher."""
        with patch("src.core.notifier.settings") as mock_settings:
            mock_settings.gcp_project_id = "test-project"
            mock_settings.pubsub_topic = "test-topic"
            return PubSubNotifier()

    def test_notify_job_started_publishes_correct_event(self, notifier, mock_publisher):
        """Test that JOB_STARTED event is published correctly."""
        # Setup mock future
        mock_future = MagicMock()
        mock_future.result.return_value = "message-id"
        mock_publisher.publish.return_value = mock_future

        # Execute
        notifier.notify_job_started(
            job_id="job-123",
            total_images=100,
            input_path="gs://bucket/input/"
        )

        # Verify publish was called
        mock_publisher.publish.assert_called_once()
        
        # Get the published message
        call_args = mock_publisher.publish.call_args
        message_bytes = call_args[0][1]
        message = json.loads(message_bytes.decode("utf-8"))

        # Verify message content
        assert message["event_type"] == "JOB_STARTED"
        assert message["job_id"] == "job-123"
        assert message["payload"]["total_images"] == 100
        assert message["payload"]["input_path"] == "gs://bucket/input/"
        assert "timestamp" in message

    def test_notify_image_processed_publishes_correct_event(self, notifier, mock_publisher):
        """Test that IMAGE_PROCESSED event is published correctly."""
        mock_future = MagicMock()
        mock_future.result.return_value = "message-id"
        mock_publisher.publish.return_value = mock_future

        notifier.notify_image_processed(
            job_id="job-123",
            image_index=5,
            image_name="page_005.jpg",
            status="success",
            model_used="flash",
            confidence=0.92,
            processing_time_ms=1500
        )

        call_args = mock_publisher.publish.call_args
        message = json.loads(call_args[0][1].decode("utf-8"))

        assert message["event_type"] == "IMAGE_PROCESSED"
        assert message["payload"]["image_index"] == 5
        assert message["payload"]["image_name"] == "page_005.jpg"
        assert message["payload"]["status"] == "success"
        assert message["payload"]["model_used"] == "flash"
        assert message["payload"]["confidence"] == 0.92

    def test_notify_job_complete_publishes_correct_event(self, notifier, mock_publisher):
        """Test that JOB_COMPLETE event is published correctly."""
        mock_future = MagicMock()
        mock_future.result.return_value = "message-id"
        mock_publisher.publish.return_value = mock_future

        notifier.notify_job_complete(
            job_id="job-123",
            total_processed=100,
            successful=95,
            failed=5,
            output_path="gs://bucket/output/job-123/",
            total_time_seconds=300.5
        )

        call_args = mock_publisher.publish.call_args
        message = json.loads(call_args[0][1].decode("utf-8"))

        assert message["event_type"] == "JOB_COMPLETE"
        assert message["payload"]["total_processed"] == 100
        assert message["payload"]["successful"] == 95
        assert message["payload"]["failed"] == 5
        assert message["payload"]["output_path"] == "gs://bucket/output/job-123/"
        assert message["payload"]["total_time_seconds"] == 300.5

    def test_notify_job_failed_publishes_correct_event(self, notifier, mock_publisher):
        """Test that JOB_FAILED event is published correctly."""
        mock_future = MagicMock()
        mock_future.result.return_value = "message-id"
        mock_publisher.publish.return_value = mock_future

        notifier.notify_job_failed(
            job_id="job-123",
            error_code="QUOTA_EXCEEDED",
            error_message="API quota limit reached",
            images_processed_before_failure=45
        )

        call_args = mock_publisher.publish.call_args
        message = json.loads(call_args[0][1].decode("utf-8"))

        assert message["event_type"] == "JOB_FAILED"
        assert message["payload"]["error_code"] == "QUOTA_EXCEEDED"
        assert message["payload"]["error_message"] == "API quota limit reached"
        assert message["payload"]["images_processed_before_failure"] == 45

    def test_publish_failure_does_not_raise(self, notifier, mock_publisher):
        """Test that publish failures are logged but don't crash the pipeline."""
        mock_future = MagicMock()
        mock_future.result.side_effect = Exception("Network error")
        mock_publisher.publish.return_value = mock_future

        # Should not raise
        notifier.notify_job_started(
            job_id="job-123",
            total_images=100,
            input_path="gs://bucket/input/"
        )

    def test_message_includes_attributes(self, notifier, mock_publisher):
        """Test that messages include event_type and job_id as attributes."""
        mock_future = MagicMock()
        mock_future.result.return_value = "message-id"
        mock_publisher.publish.return_value = mock_future

        notifier.notify_job_started(
            job_id="job-456",
            total_images=50,
            input_path="gs://bucket/test/"
        )

        call_args = mock_publisher.publish.call_args
        assert call_args[1]["event_type"] == "JOB_STARTED"
        assert call_args[1]["job_id"] == "job-456"


class TestEventType:
    """Tests for EventType enum."""

    def test_event_type_values(self):
        """Verify all required event types exist."""
        assert EventType.JOB_STARTED.value == "JOB_STARTED"
        assert EventType.IMAGE_PROCESSED.value == "IMAGE_PROCESSED"
        assert EventType.JOB_COMPLETE.value == "JOB_COMPLETE"
        assert EventType.JOB_FAILED.value == "JOB_FAILED"
