"""Pytest configuration and fixtures."""

import os
import sys
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Set required environment variables for all tests."""
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("GCS_INPUT_BUCKET", "test-input-bucket")
    monkeypatch.setenv("GCS_OUTPUT_BUCKET", "test-output-bucket")
    monkeypatch.setenv("PUBSUB_TOPIC", "test-topic")
    monkeypatch.setenv("GEMINI_MODEL_FLASH", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_MODEL_PRO", "gemini-2.5-pro")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.85")
