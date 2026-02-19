"""
Configuration management for BIQE HTR Pipeline.

All environment variables and settings are centralized here.
Uses Pydantic for validation and type safety.
"""

import os
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # GCP Project Configuration
    gcp_project_id: str = Field(..., env="GCP_PROJECT_ID")
    gcp_region: str = Field(default="us-central1", env="GCP_REGION")

    # Cloud Storage Buckets (optional for middleware mode)
    gcs_input_bucket: str = Field(default="", env="GCS_INPUT_BUCKET")
    gcs_output_bucket: str = Field(default="", env="GCS_OUTPUT_BUCKET")
    gcs_staging_bucket: str = Field(default="", env="GCS_STAGING_BUCKET")

    # Pub/Sub Configuration (optional for middleware mode)
    pubsub_topic: str = Field(default="", env="PUBSUB_TOPIC")

    # Gemini Model Configuration
    gemini_model_flash: str = Field(
        default="gemini-2.5-flash-preview-05-20", 
        env="GEMINI_MODEL_FLASH"
    )
    gemini_model_pro: str = Field(
        default="gemini-2.5-pro-preview-05-06", 
        env="GEMINI_MODEL_PRO"
    )

    # AI Logic Configuration
    confidence_threshold: float = Field(
        default=0.95, 
        env="CONFIDENCE_THRESHOLD",
        ge=0.0,
        le=1.0
    )
    temperature: float = Field(
        default=0.0,
        env="TEMPERATURE",
        ge=0.0,
        le=2.0
    )
    max_retries: int = Field(default=3, env="MAX_RETRIES")
    retry_delay_seconds: float = Field(default=1.0, env="RETRY_DELAY_SECONDS")
    
    # Rate Limiting Configuration (for Gemini quota management)
    # Default: 5 RPM = 12 seconds minimum between requests on single worker
    # Adjust these when quota is increased
    api_rpm_limit: int = Field(
        default=5,
        env="API_RPM_LIMIT",
        description="Gemini API requests per minute limit"
    )
    worker_rate_limit_seconds: float = Field(
        default=15.0,
        env="WORKER_RATE_LIMIT_SECONDS",
        description="Minimum seconds between Gemini API calls per worker"
    )
    
    # Parallel Processing Configuration (for Tier 2+ accounts)
    # With Tier 2, you can process multiple images simultaneously
    # Default: 5 parallel workers (adjust based on your tier)
    ocr_parallel_workers: int = Field(
        default=5,
        env="OCR_PARALLEL_WORKERS",
        description="Number of parallel OCR workers (set to 1 for serial processing)"
    )
    
    # OpenRouter Configuration (v2.4.0)
    # Only required when using provider="openrouter"
    openrouter_api_key: str = Field(
        default="",
        env="OPENROUTER_API_KEY",
        description="OpenRouter API key (required for openrouter provider)"
    )

    # Job Configuration (injected by Cloud Run)
    job_id: str = Field(default="", env="JOB_ID")
    input_path: str = Field(default="", env="INPUT_PATH")
    task_index: int = Field(default=0, env="CLOUD_RUN_TASK_INDEX")
    task_count: int = Field(default=1, env="CLOUD_RUN_TASK_COUNT")

    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    log_format: str = Field(default="json", env="LOG_FORMAT")

    # Local Processing
    local_temp_dir: str = Field(default="/tmp/biqe-htr", env="LOCAL_TEMP_DIR")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    
    Uses lru_cache to ensure settings are only loaded once.
    """
    return Settings()


# Lazy settings accessor - don't load at import time
class _SettingsProxy:
    """Lazy proxy for settings to avoid import-time validation errors."""
    
    _instance: Settings | None = None
    
    def __getattr__(self, name: str):
        if self._instance is None:
            self._instance = get_settings()
        return getattr(self._instance, name)


# Convenience export - now lazy
settings = _SettingsProxy()
