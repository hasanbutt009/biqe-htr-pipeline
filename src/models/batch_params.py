"""
Batch processing models and parameters.

Defines request/response schemas for batch OCR operations.

Version 2.2.0 - Added per-step configuration with custom prompts, TopP/TopK, and output formatting.
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class ThinkingLevel(str, Enum):
    """Gemini 3 thinking level parameter."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ModelChoice(str, Enum):
    """Available model choices."""
    FLASH = "flash"
    PRO = "pro"
    AUTO = "auto"  # Use Flash first, Pro fallback


class AIProvider(str, Enum):
    """AI provider selection (v2.4.0)."""
    VERTEX = "vertex"      # Google Vertex AI (default, GDPR-compliant EU)
    OPENROUTER = "openrouter"  # OpenRouter API (global, for USA market)


# =============================================================================
# NEW: Per-Step Configuration (v2.2.0)
# =============================================================================

class StepConfig(BaseModel):
    """
    Configuration for a single OCR step (Step 1 or Step 2).
    
    Allows customizing model, prompt, and generation parameters per step.
    """
    
    model: str = Field(
        default="gemini-3-flash-preview",
        description="Model to use for this step (gemini-3-flash-preview or gemini-3-pro-preview)"
    )
    
    prompt: Optional[str] = Field(
        default=None,
        description="Custom prompt for this step. If None, uses the default system prompt."
    )
    
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="Temperature for generation (0.0 = deterministic)"
    )
    
    top_p: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Nucleus sampling: cumulative probability threshold"
    )
    
    top_k: int = Field(
        default=40,
        ge=1,
        le=100,
        description="Top-K sampling: number of highest probability tokens to consider"
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage/messaging."""
        return {
            "model": self.model,
            "prompt": self.prompt,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }


class OutputOptions(BaseModel):
    """
    Output formatting options for transcription results.
    
    Controls how the final text is formatted.
    """
    
    preserve_newlines: bool = Field(
        default=True,
        description="If False, removes \\n from text output (joins lines with spaces)"
    )
    
    preserve_hyphens: bool = Field(
        default=True,
        description="If False, merges line-break hyphens (e.g., 'recog-\\nnition' → 'recognition')"
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage/messaging."""
        return {
            "preserve_newlines": self.preserve_newlines,
            "preserve_hyphens": self.preserve_hyphens,
        }


class BatchParams(BaseModel):
    """
    OCR parameters for batch processing.
    
    These can be set per-request to control OCR behavior.
    
    Version 2.2.0 adds:
    - step1: Per-step configuration for initial OCR
    - step2: Per-step configuration for correction
    - output_options: Text formatting options
    """
    
    # Model selection (legacy, for backward compatibility)
    model: ModelChoice = Field(
        default=ModelChoice.AUTO,
        description="Model to use: flash (fast), pro (quality), auto (flash with pro fallback)"
    )
    
    # Generation config (legacy, for backward compatibility)
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="Temperature for generation (0.0 = deterministic)"
    )
    
    thinking_level: ThinkingLevel = Field(
        default=ThinkingLevel.LOW,
        description="Gemini 3 thinking level: LOW (fast), MEDIUM, HIGH (quality)"
    )
    
    # OCR config
    confidence_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Confidence threshold for Flash-to-Pro routing"
    )
    
    two_step_ocr: bool = Field(
        default=False,
        description="Enable two-step OCR: Flash → Pro ground truth correction"
    )
    
    # NEW v2.3.0: Step 1 only mode (for manual Step 2 verification workflow)
    step1_only: bool = Field(
        default=False,
        description="If True, only run Step 1 and skip auto Step 2 (for manual verification)"
    )
    
    # NEW v2.2.0: Per-step configuration
    step1: Optional[StepConfig] = Field(
        default=None,
        description="Step 1 configuration (initial OCR). If None, uses legacy params."
    )
    
    step2: Optional[StepConfig] = Field(
        default=None,
        description="Step 2 configuration (correction). If None, uses default correction prompt."
    )
    
    # NEW v2.2.0: Output formatting
    output_options: Optional[OutputOptions] = Field(
        default=None,
        description="Output formatting options. If None, uses defaults (preserve all)."
    )
    
    # NEW v2.4.0: AI Provider selection
    provider: AIProvider = Field(
        default=AIProvider.VERTEX,
        description="AI provider: vertex (Google/EU) or openrouter (global/USA)"
    )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage/messaging."""
        result = {
            "model": self.model.value,
            "temperature": self.temperature,
            "thinking_level": self.thinking_level.value,
            "confidence_threshold": self.confidence_threshold,
            "two_step_ocr": self.two_step_ocr,
            "step1_only": self.step1_only,  # v2.3.0: Manual Step 2 workflow
            "provider": self.provider.value,  # v2.4.0: AI provider selection
        }
        
        # Add new v2.2.0 fields if present
        if self.step1:
            result["step1"] = self.step1.to_dict()
        if self.step2:
            result["step2"] = self.step2.to_dict()
        if self.output_options:
            result["output_options"] = self.output_options.to_dict()
        
        return result
    
    def get_step1_config(self) -> StepConfig:
        """Get Step 1 configuration, falling back to legacy params if not set."""
        if self.step1:
            return self.step1
        
        # Fall back to legacy params
        model_map = {
            ModelChoice.FLASH: "gemini-3-flash-preview",
            ModelChoice.PRO: "gemini-3-pro-preview",
            ModelChoice.AUTO: "gemini-3-flash-preview",
        }
        return StepConfig(
            model=model_map[self.model],
            temperature=self.temperature,
            top_p=1.0,
            top_k=40,
        )
    
    def get_step2_config(self) -> StepConfig:
        """Get Step 2 configuration, using defaults if not set."""
        if self.step2:
            return self.step2
        
        # Default Step 2 uses Pro model
        return StepConfig(
            model="gemini-3-pro-preview",
            temperature=self.temperature,
            top_p=1.0,
            top_k=40,
        )
    
    def get_output_options(self) -> OutputOptions:
        """Get output options, using defaults if not set."""
        return self.output_options or OutputOptions()


class BatchSubmitResponse(BaseModel):
    """Response after batch submission."""
    
    batch_id: str = Field(description="Unique batch identifier")
    status: str = Field(default="processing", description="Batch status")
    total_files: int = Field(description="Number of files in batch")
    message: str = Field(default="Batch submitted successfully")


class BatchStatusResponse(BaseModel):
    """Response for batch status query."""
    
    batch_id: str
    status: str
    total_files: int
    completed_files: int
    failed_files: int
    pending_files: int
    progress_percentage: float
    
    class Config:
        json_schema_extra = {
            "example": {
                "batch_id": "abc123",
                "status": "processing",
                "total_files": 50,
                "completed_files": 25,
                "failed_files": 0,
                "pending_files": 25,
                "progress_percentage": 50.0,
            }
        }


class FileResult(BaseModel):
    """Individual file result."""
    
    file_id: str
    filename: str
    status: str
    text: Optional[str] = None
    confidence: Optional[float] = None
    model_used: Optional[str] = None
    error: Optional[str] = None


class BatchResultsResponse(BaseModel):
    """Response containing all batch results."""
    
    batch_id: str
    status: str
    total_files: int
    completed_files: int
    failed_files: int
    results: list[FileResult]
