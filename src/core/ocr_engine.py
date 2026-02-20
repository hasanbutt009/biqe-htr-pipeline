"""
OCR Engine with Per-Request Parameters and Rate Limiting.

This module implements the core AI logic with:
- Per-request temperature, topP, topK, and model selection
- Custom prompts passed as user message (v2.4.1)
- google-genai SDK (recommended by Google)
- Automatic retry on 429 Resource Exhausted errors
- Exponential backoff (60s, 120s, 240s, 480s max = ~15 min total)
- Gemini 3 models on global endpoint

v2.4.1: Prompt flexibility update
- Removed system_instruction restriction for Vertex AI
- Prompts now passed as user message (same as OpenRouter)
- Full prompt flexibility: transformation instructions now work

v2.4.0: Added OpenRouter provider support
- Same interface as Vertex AI engine
- Provider selected via `provider` parameter in BatchParams
- Factory function `get_ocr_engine()` returns appropriate engine

IMPORTANT: Gemini 3 Preview has strict 5 RPM quota.
The worker is responsible for throttling - this engine just handles retries.

SDK Upgrade (v2.2.1):
- Replaced vertexai.generative_models with google.genai
- Using HttpOptions for API version control
"""

import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import uuid4

import httpx  # v2.4.0: For OpenRouter API calls

# NEW SDK: google-genai (replaces vertexai.generative_models)
from google import genai
from google.genai import types
from google.genai.types import (
    Content,
    Part,
    GenerateContentConfig,
    HttpOptions,
    ThinkingConfig,
    # NOTE: ThinkingLevel removed - cannot use thinkingLevel AND thinkingBudget together
    # Only using thinkingBudget for direct control
)
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    stop_after_delay,
    retry_if_exception_type,
    before_sleep_log,
)

from src.config import settings


# Use standard Python logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ModelType(Enum):
    """Available Gemini models."""
    FLASH = "flash"
    PRO = "pro"


class ModelChoice(Enum):
    """Model selection options from client."""
    FLASH = "flash"
    PRO = "pro"
    AUTO = "auto"  # Flash first, Pro fallback


@dataclass
class OCRResult:
    """Structured OCR result from Gemini processing."""
    
    document_id: str
    source_file: str
    text_content: str
    confidence_score: float
    model_used: ModelType
    flash_attempted: bool
    pro_fallback_used: bool
    processing_time_ms: int
    raw_response: Optional[dict] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "document_id": self.document_id,
            "source_file": self.source_file,
            "processing": {
                "model": self.model_used.value,
                "confidence_score": self.confidence_score,
                "flash_attempted": self.flash_attempted,
                "pro_fallback_used": self.pro_fallback_used,
                "processing_time_ms": self.processing_time_ms,
            },
            "result": {
                "text_content": self.text_content,
            },
            "error": self.error,
        }


@dataclass
class StepParams:
    """Per-step OCR parameters (v2.2.0)."""
    model: str = "gemini-3-flash-preview"
    prompt: Optional[str] = None
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 40
    
    @classmethod
    def from_dict(cls, params: Optional[dict]) -> Optional["StepParams"]:
        """Create from dictionary (e.g., from Pub/Sub message)."""
        if not params:
            return None
        return cls(
            model=params.get("model", "gemini-3-flash-preview"),
            prompt=params.get("prompt"),
            temperature=params.get("temperature", 0.0),
            top_p=params.get("top_p", 1.0),
            top_k=params.get("top_k", 40),
        )


@dataclass
class OutputParams:
    """Output formatting parameters (v2.2.0)."""
    preserve_newlines: bool = True
    preserve_hyphens: bool = True
    
    @classmethod
    def from_dict(cls, params: Optional[dict]) -> "OutputParams":
        """Create from dictionary (e.g., from Pub/Sub message)."""
        if not params:
            return cls()
        return cls(
            preserve_newlines=params.get("preserve_newlines", True),
            preserve_hyphens=params.get("preserve_hyphens", True),
        )


@dataclass
class OCRParams:
    """
    Per-request OCR parameters.
    
    These can be overridden from the client for each batch/request.
    
    v2.2.0: Added step1, step2, and output_options for per-step configuration.
    """
    temperature: float = 0.0
    model: str = "auto"  # flash, pro, auto
    confidence_threshold: float = 0.95
    two_step_ocr: bool = False
    
    # NEW v2.2.0: Per-step configuration
    step1: Optional[StepParams] = None
    step2: Optional[StepParams] = None
    output_options: Optional[OutputParams] = None
    
    @classmethod
    def from_dict(cls, params: dict) -> "OCRParams":
        """Create from dictionary (e.g., from Pub/Sub message)."""
        return cls(
            temperature=params.get("temperature", 0.0),
            model=params.get("model", "auto"),
            confidence_threshold=params.get("confidence_threshold", 0.95),
            two_step_ocr=params.get("two_step_ocr", False),
            step1=StepParams.from_dict(params.get("step1")),
            step2=StepParams.from_dict(params.get("step2")),
            output_options=OutputParams.from_dict(params.get("output_options")),
        )
    
    def get_step1_params(self) -> StepParams:
        """Get Step 1 params, using defaults if not set."""
        if self.step1:
            return self.step1
        # Default: Flash model with legacy temperature
        # Use generic name "flash" - engine maps to correct model for provider
        return StepParams(
            model="flash",
            temperature=self.temperature,
            top_p=1.0,
            top_k=40,
        )
    
    def get_step2_params(self) -> StepParams:
        """Get Step 2 params, using defaults if not set."""
        if self.step2:
            return self.step2
        # Default: Pro model with legacy temperature
        # Use generic name "pro" - engine maps to correct model for provider
        return StepParams(
            model="pro",
            temperature=self.temperature,
            top_p=1.0,
            top_k=40,
        )
    
    def get_output_params(self) -> OutputParams:
        """Get output params, using defaults if not set."""
        return self.output_options or OutputParams()


class OCREngine:
    """
    Chained Logic OCR Engine with Per-Request Parameters.
    
    v2.2.1 - Upgraded to google-genai SDK with systemInstruction support.
    
    Features:
    - Per-request temperature, topP, topK, and model selection
    - Custom prompts via systemInstruction (Google-recommended)
    - Flash-first, Pro fallback routing
    - Tenacity retry for 429 Quota Exceeded errors
    - Exponential backoff (60s, 120s, 240s, 480s = ~15 min total)
    
    IMPORTANT: Gemini 3 Preview models have 5 RPM quota.
    This engine retries on 429, but the WORKER must throttle to 1 req/15s.
    """

    # System prompt for expert paleographer handwriting recognition
    # This is now used as systemInstruction (not concatenated in contents)
    SYSTEM_PROMPT = """You are an expert paleographer specialized in 17th to 19th-century colonial and European handwriting.
Your task is to accurately transcribe handwritten historical documents.

Expertise Areas:
- Colonial American court records and legal documents
- European administrative manuscripts
- Archaic spellings and period-specific orthography
- Legal abbreviations and notarial conventions
- Latin phrases commonly used in official documents

Instructions:
1. Extract ALL text visible in the image, preserving the EXACT original structure
2. Maintain paragraph breaks, indentation, and line formatting as they appear
3. Preserve archaic spellings exactly as written (do not modernize)
4. Recognize and expand common abbreviations where clear (e.g., "ye" for "the")
5. Use [unclear] for genuinely illegible portions
6. Report your confidence level (0.0 to 1.0) for the transcription

Output format:
- First line: CONFIDENCE: <score between 0.0 and 1.0>
- Remaining lines: The transcribed text with original formatting preserved

Example:
CONFIDENCE: 0.95
At a Court held for Rockingham County
on the first day of February 1791
Present:
William Benjamin Smith, Esquire
"""

    def __init__(self):
        """
        Initialize the OCR Engine with google-genai SDK.
        
        v2.2.1: Uses google.genai.Client instead of vertexai.GenerativeModel.
        v3.0: Changed to europe-west4 region with Gemini 2.0/1.5 for GDPR compliance.
        - Uses Vertex AI backend (not AI Studio)
        - EU region for data residency
        - Stable production models
        """
        # Initialize google-genai client with Vertex AI backend
        # v3.0: europe-west4 for GDPR compliance
        self._client = genai.Client(
            vertexai=True,  # Use Vertex AI backend (not AI Studio)
            project=settings.gcp_project_id,
            location="europe-west4",  # v3.0: EU region for GDPR compliance
            http_options=HttpOptions(api_version="v1"),
        )
        
        # v3.0: Use Gemini 2.5 models (latest stable, supports europe-west4)
        # Auto-updated aliases point to latest stable version in region
        self._flash_model_name = "gemini-2.5-flash"  # Latest Flash model
        self._pro_model_name = "gemini-2.5-pro"  # Latest Pro model
        
        # Default settings (can be overridden per-request)
        self._default_confidence_threshold = settings.confidence_threshold
        self._default_temperature = settings.temperature
        
        logger.info(
            f"OCR Engine initialized with google-genai SDK: "
            f"project={settings.gcp_project_id}, location=europe-west4, "
            f"flash={self._flash_model_name}, pro={self._pro_model_name}"
        )

    def process_image(
        self,
        image_path: str | Path,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process an image file through the chained logic pipeline.
        
        Args:
            image_path: Path to the image file (local)
            params: Optional per-request parameters
            
        Returns:
            OCRResult with transcribed text and metadata
        """
        path = Path(image_path)
        with open(path, "rb") as f:
            image_data = f.read()
        
        mime_type = self._get_mime_type(str(path))
        return self.process_image_bytes(image_data, mime_type, path.name, params)

    def process_image_bytes(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process image bytes through the chained logic pipeline.
        
        This is the main entry point for the FastAPI middleware and worker.
        
        Args:
            image_data: Raw image bytes
            mime_type: MIME type of the image
            filename: Original filename
            params: Optional per-request parameters:
                - temperature: float (0.0-2.0)
                - model: str (flash, pro, auto)
                - confidence_threshold: float (0.0-1.0)
            
        Returns:
            OCRResult with transcribed text and metadata
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        # Parse per-request parameters
        ocr_params = OCRParams.from_dict(params or {})
        temperature = ocr_params.temperature
        model_choice = ocr_params.model
        confidence_threshold = ocr_params.confidence_threshold
        
        # v2.4.1: Extract custom prompt from step1 config (if provided)
        step1_params = ocr_params.get_step1_params()
        custom_prompt = step1_params.prompt if step1_params else None
        
        logger.info(
            f"Starting OCR processing: document_id={document_id}, "
            f"filename={filename}, mime_type={mime_type}, size={len(image_data)} bytes, "
            f"temperature={temperature}, model={model_choice}, threshold={confidence_threshold}, "
            f"custom_prompt={'yes' if custom_prompt else 'no (using default)'}"
        )

        # Determine which model(s) to use based on model_choice
        if model_choice == "pro":
            # Pro only - skip Flash
            return self._process_with_pro_only(
                image_data, mime_type, filename, temperature, document_id, start_time,
                custom_prompt=custom_prompt
            )
        elif model_choice == "flash":
            # Flash only - no fallback
            return self._process_with_flash_only(
                image_data, mime_type, filename, temperature, document_id, start_time,
                custom_prompt=custom_prompt
            )
        else:
            # Auto mode - Flash first, Pro fallback on low confidence
            return self._process_with_auto_routing(
                image_data, mime_type, filename, temperature, 
                confidence_threshold, document_id, start_time,
                custom_prompt=custom_prompt
            )

    def _process_with_auto_routing(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        temperature: float,
        confidence_threshold: float,
        document_id: str,
        start_time: float,
        custom_prompt: Optional[str] = None,  # v2.4.1: Accept custom prompt
    ) -> OCRResult:
        """
        Auto mode: Flash first, Pro fallback on low confidence.
        
        v2.4.1: Now supports custom_prompt for full prompt flexibility.
        """
        # Step 1: Try Flash model first (cost optimization)
        flash_result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=self._flash_model_name,
            model_type=ModelType.FLASH,
            temperature=temperature,
            custom_prompt=custom_prompt,  # v2.4.1: Pass custom prompt
        )

        if flash_result.error:
            # Flash failed after all retries, try Pro as fallback
            logger.warning(f"Flash model failed, attempting Pro fallback: {flash_result.error}")
            pro_result = self._process_with_model_retry(
                image_data=image_data,
                mime_type=mime_type,
                model_name=self._pro_model_name,
                model_type=ModelType.PRO,
                temperature=temperature,
                custom_prompt=custom_prompt,  # v2.4.1: Pass custom prompt
            )
            pro_result.flash_attempted = True
            pro_result.pro_fallback_used = True
            pro_result.document_id = document_id
            pro_result.source_file = filename
            pro_result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
            return pro_result

        # Step 2: Evaluate confidence score
        if flash_result.confidence_score >= confidence_threshold:
            # High confidence - accept Flash result
            logger.info(
                f"Flash result accepted (high confidence): "
                f"confidence={flash_result.confidence_score}, threshold={confidence_threshold}"
            )
            flash_result.document_id = document_id
            flash_result.source_file = filename
            flash_result.flash_attempted = True
            flash_result.pro_fallback_used = False
            flash_result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
            return flash_result

        # Step 3: Low confidence - escalate to Pro model
        logger.info(
            f"Low confidence detected, escalating to Pro model: "
            f"flash_confidence={flash_result.confidence_score}, threshold={confidence_threshold}"
        )
        
        pro_result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=self._pro_model_name,
            model_type=ModelType.PRO,
            temperature=temperature,
            custom_prompt=custom_prompt,  # v2.4.1: Pass custom prompt
        )
        
        pro_result.document_id = document_id
        pro_result.source_file = filename
        pro_result.flash_attempted = True
        pro_result.pro_fallback_used = True
        pro_result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        logger.info(
            f"Pro model processing complete: "
            f"confidence={pro_result.confidence_score}, time={pro_result.processing_time_ms}ms"
        )
        
        return pro_result

    def _process_with_flash_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        temperature: float,
        document_id: str,
        start_time: float,
        custom_prompt: Optional[str] = None,  # v2.4.1: Accept custom prompt
    ) -> OCRResult:
        """
        Flash only mode - no Pro fallback.
        
        v2.4.1: Now supports custom_prompt for full prompt flexibility.
        """
        logger.info(f"Processing with Flash only (model=flash): {filename}")
        
        result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=self._flash_model_name,
            model_type=ModelType.FLASH,
            temperature=temperature,
            custom_prompt=custom_prompt,  # v2.4.1: Pass custom prompt
        )
        
        result.document_id = document_id
        result.source_file = filename
        result.flash_attempted = True
        result.pro_fallback_used = False
        result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        return result

    def _process_with_pro_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        temperature: float,
        document_id: str,
        start_time: float,
        custom_prompt: Optional[str] = None,  # v2.4.1: Accept custom prompt
    ) -> OCRResult:
        """
        Pro only mode - highest quality, skip Flash.
        
        v2.4.1: Now supports custom_prompt for full prompt flexibility.
        """
        logger.info(f"Processing with Pro only (model=pro): {filename}")
        
        result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=self._pro_model_name,
            model_type=ModelType.PRO,
            temperature=temperature,
            custom_prompt=custom_prompt,  # v2.4.1: Pass custom prompt
        )
        
        result.document_id = document_id
        result.source_file = filename
        result.flash_attempted = False
        result.pro_fallback_used = True
        result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        return result

    def _process_with_model_retry(
        self,
        image_data: bytes,
        mime_type: str,
        model_name: str,
        model_type: ModelType,
        temperature: float,
        top_p: float = 1.0,
        top_k: int = 40,
        custom_prompt: Optional[str] = None,
    ) -> OCRResult:
        """
        Process image with a specific Gemini model, WITH RETRY LOGIC.
        
        This wraps the actual API call with tenacity retry for quota errors.
        
        v2.2.1: Uses model_name (string) instead of model object.
        """
        try:
            text, confidence = self._call_gemini_with_retry(
                image_data=image_data,
                mime_type=mime_type,
                model_name=model_name,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                custom_prompt=custom_prompt,
            )
            
            return OCRResult(
                document_id="",
                source_file="",
                text_content=text,
                confidence_score=confidence,
                model_used=model_type,
                flash_attempted=False,
                pro_fallback_used=False,
                processing_time_ms=0,
            )
            
        except Exception as e:
            logger.error(f"Model processing failed after retries: model={model_type.value}, error={str(e)}")
            return OCRResult(
                document_id="",
                source_file="",
                text_content="",
                confidence_score=0.0,
                model_used=model_type,
                flash_attempted=False,
                pro_fallback_used=False,
                processing_time_ms=0,
                error=str(e),
            )

    # =========================================================================
    # CRITICAL: Tenacity Retry Decorator for Quota Management
    # =========================================================================
    @retry(
        retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable, ConnectionError, OSError)),
        wait=wait_exponential(multiplier=60, min=60, max=480),  # Wait 60s, 120s, 240s, 480s (~15 min total)
        stop=stop_after_attempt(5),  # Max 5 attempts
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _call_gemini_with_retry(
        self,
        image_data: bytes,
        mime_type: str,
        model_name: str,
        temperature: float,
        top_p: float = 1.0,
        top_k: int = 40,
        custom_prompt: Optional[str] = None,
    ) -> tuple[str, float]:
        """
        Call Gemini API via google-genai SDK with automatic retry on quota errors.
        
        v2.4.1: Removed system_instruction restriction - prompt now passed as user message
                for full prompt flexibility (same behavior as OpenRouter).
        
        This is the CRITICAL function that handles:
        - 429 Resource Exhausted (Quota) errors
        - 503 Service Unavailable errors
        
        Parameters:
        - model_name: Full model name (e.g., "gemini-3-flash-preview")
        - custom_prompt: Custom prompt passed as user message (uses default if None)
        - temperature, top_p, top_k: Generation parameters
        """
        logger.debug(f"Calling Gemini API via google-genai SDK (model={model_name}, temp={temperature}, topP={top_p}, topK={top_k})")
        
        # Use custom prompt if provided, otherwise use default SYSTEM_PROMPT
        user_prompt = custom_prompt if custom_prompt else self.SYSTEM_PROMPT
        
        # Create image part using the new SDK format
        image_part = Part.from_bytes(data=image_data, mime_type=mime_type)
        
        # Content: image + prompt text as user message (NOT system_instruction)
        # This allows full prompt flexibility like OpenRouter
        # Note: Use string directly in list, SDK converts it to Part
        contents = [image_part, user_prompt]
        
        # v3.0: Generation config for Gemini 2.5
        # Gemini 2.5 supports thinking and has 65,535 max output tokens
        config = GenerateContentConfig(
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_output_tokens=8192,  # Keep reasonable limit for OCR (max is 65535)
            thinking_config=ThinkingConfig(
                thinkingBudget=512,  # Gemini 2.5 supports thinking
                includeThoughts=False,
            ),
        )
        
        # Call the API using the new SDK
        # This may raise ResourceExhausted (429) which triggers retry
        response = self._client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )
        
        # Parse response - properly handle Gemini 3 Thinking Tokens
        # The model may return thinking parts AND text parts separately
        # See: https://cloud.google.com/vertex-ai/docs/generative-ai/thinking
        response_text = ""
        thinking_text = ""
        
        # Check if we have candidates with content parts
        if response.candidates and len(response.candidates) > 0:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content:
                if hasattr(candidate.content, 'parts') and candidate.content.parts:
                    for part in candidate.content.parts:
                        # Check if this is a "thinking" part (internal reasoning)
                        if hasattr(part, 'thought') and part.thought:
                            # This is a thinking/reasoning part, not the final answer
                            if hasattr(part, 'text') and part.text:
                                thinking_text = part.text
                            continue
                        # Extract actual text content
                        if hasattr(part, 'text') and part.text:
                            response_text = part.text
                            break  # Use first non-thinking text part
        
        # Fallback to response.text if our parsing didn't find text
        if not response_text and response.text:
            response_text = response.text
        
        # Log detailed info for debugging empty responses
        if not response_text:
            usage = getattr(response, 'usage_metadata', None)
            finish_reason = None
            if response.candidates:
                finish_reason = getattr(response.candidates[0], 'finish_reason', None)
            logger.warning(
                f"Gemini returned empty response for model={model_name}. "
                f"finish_reason={finish_reason}, "
                f"thinking_tokens_used={len(thinking_text) if thinking_text else 0}, "
                f"usage_metadata={usage}"
            )
            # If we have thinking but no output, this is the known Google bug
            if thinking_text:
                logger.error(
                    f"KNOWN BUG: Model generated {len(thinking_text)} chars of thinking "
                    f"but no output text. This is a confirmed Google issue with Gemini 3 Pro."
                )
        
        text_content, confidence = self._parse_response(response_text)
        
        return text_content, confidence

    def _get_mime_type(self, image_path: str) -> str:
        """Determine MIME type from file extension."""
        suffix = Path(image_path).suffix.lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
        }
        return mime_types.get(suffix, "image/jpeg")

    def _parse_response(self, response_text: str) -> tuple[str, float]:
        """
        Parse model response to extract text and confidence score.
        
        v2.4.2: Fixed to properly extract TRANSCRIPTION section instead of
        incorrectly returning metadata as content.
        
        Expected format (new):
        TRANSCRIPTION:
        <transcribed text>
        
        METADATA:
        CONFIDENCE: 0.95
        STATUS: OK
        
        Also supports legacy format:
        CONFIDENCE: 0.95
        <transcribed text>
        """
        # Handle None or empty response
        if not response_text:
            logger.warning("Empty or None response_text received in _parse_response")
            return "", 0.0
        
        # Extract transcription block
        transcription_text = ""
        if "TRANSCRIPTION:" in response_text:
            # Split on TRANSCRIPTION: marker (new structured format)
            parts = response_text.split("TRANSCRIPTION:", 1)
            if len(parts) > 1:
                # Get text between TRANSCRIPTION: and METADATA:
                content = parts[1]
                if "METADATA:" in content:
                    transcription_text = content.split("METADATA:")[0].strip()
                else:
                    # No METADATA section, take everything after TRANSCRIPTION:
                    transcription_text = content.strip()
        else:
            # Legacy format: CONFIDENCE: followed by text
            # or fallback: use entire response
            lines = response_text.strip().split("\n")
            text_start = 0
            
            # Look for confidence line
            for i, line in enumerate(lines):
                if line.upper().startswith("CONFIDENCE:"):
                    text_start = i + 1
                    break
            
            # If we found CONFIDENCE:, take text after it
            # Otherwise, take entire response
            if text_start > 0:
                transcription_text = "\n".join(lines[text_start:]).strip()
            else:
                transcription_text = response_text.strip()
        
        # Extract confidence from METADATA section or CONFIDENCE: line
        confidence = 0.85  # Default confidence if not parsed
        if "CONFIDENCE:" in response_text.upper():
            try:
                # Find the line containing CONFIDENCE:
                conf_line = [l for l in response_text.split("\n") if "CONFIDENCE:" in l.upper()][0]
                confidence_str = conf_line.split(":", 1)[1].strip()
                confidence = float(confidence_str)
                confidence = max(0.0, min(1.0, confidence))  # Clamp to [0, 1]
            except (ValueError, IndexError, AttributeError):
                logger.warning(f"Failed to parse confidence score from response")
                pass
        
        return transcription_text, confidence

    def process_two_step(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Two-step OCR: Flash → Pro ground truth correction.
        
        Step 1: Flash model (or custom step1.model) for fast initial transcription
        Step 2: Pro model (or custom step2.model) sees image + Flash output, corrects errors
        
        v2.2.0: Full per-step configuration with custom prompts, TopP, TopK.
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        # Parse per-request parameters (v2.2.0: includes step1, step2, output_options)
        ocr_params = OCRParams.from_dict(params or {})
        confidence_threshold = ocr_params.confidence_threshold
        
        # Get per-step configurations
        step1_params = ocr_params.get_step1_params()
        step2_params = ocr_params.get_step2_params()
        output_params = ocr_params.get_output_params()
        
        # Step 1: Initial transcription using step1 config (v2.2.1 - use model name string)
        step1_model_name = self._get_model_name_by_name(step1_params.model)
        step1_model_type = self._get_model_type_by_name(step1_params.model)
        
        # Step 2: Get model name for logging
        step2_model_name = self._get_model_name_by_name(step2_params.model)
        
        logger.info(f"Starting two-step OCR: {filename}, step1={step1_model_name}, step2={step2_model_name}")
        
        flash_result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=step1_model_name,
            model_type=step1_model_type,
            temperature=step1_params.temperature,
            top_p=step1_params.top_p,
            top_k=step1_params.top_k,
            custom_prompt=step1_params.prompt,
        )
        
        if flash_result.error or not flash_result.text_content:
            # Step 1 failed, fall back to step 2 model only
            return self._process_with_pro_only_v2(
                image_data, mime_type, filename, step2_params, document_id, start_time
            )
        
        # Check if confidence is high enough to skip Step 2
        if flash_result.confidence_score >= confidence_threshold:
            logger.info(f"Two-step: Step 1 high confidence ({flash_result.confidence_score}), skipping Step 2")
            flash_result.document_id = document_id
            flash_result.source_file = filename
            flash_result.flash_attempted = True
            flash_result.pro_fallback_used = False
            flash_result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
            
            # Apply output formatting (v2.2.0)
            flash_result.text_content = self._format_output(flash_result.text_content, output_params)
            
            return flash_result
        
        # Step 2: Correction model with ground truth correction
        # Build correction prompt - use custom step2 prompt if provided, else default
        if step2_params.prompt:
            # Custom prompt: inject Step 1 result using {step1_result} placeholder
            correction_prompt = step2_params.prompt.replace("{step1_result}", flash_result.text_content)
        else:
            # Default correction prompt
            correction_prompt = f"""You are an expert document layout analyst. Review the transcription below against the original image and correct any errors.

LAYOUT INSTRUCTIONS:
1. Line-for-Line Exactness: Break the text exactly where the lines break in the image
2. Marginalia Integration: Scan for notes in margins, insert inside square brackets [ ] at the end of the line it aligns with
3. Indentation: Preserve paragraph indentations and whitespace visually
4. No Meta-Data: Do not include headers like "Transcription:". Just output the raw text
5. Sanity Check: If the transcription contradicts the image (e.g., missed a word), trust the IMAGE and insert the missing word

TRANSCRIPTION TO REVIEW:
{flash_result.text_content}

OUTPUT FORMAT:
- First line: CONFIDENCE: <score between 0.0 and 1.0>
- Remaining lines: The corrected transcription with original formatting preserved
"""
        
        # Create content with image for Step 2 (v2.2.1 - new SDK format)
        image_part = Part.from_bytes(data=image_data, mime_type=mime_type)
        contents = [
            Content(
                role="user",
                parts=[
                    Part.from_text(text=correction_prompt),
                    image_part,
                ],
            ),
        ]
        
        # Step 2 generation config with per-step parameters (v2.2.1 - new SDK)
        # v3.0: Config for Gemini 2.5 (supports thinking, 65535 max tokens)
        step2_config = GenerateContentConfig(
            temperature=step2_params.temperature,
            top_p=step2_params.top_p,
            top_k=step2_params.top_k,
            max_output_tokens=8192,  # Keep reasonable (max is 65535)
            thinking_config=ThinkingConfig(
                thinkingBudget=512,
                includeThoughts=False,
            ),
        )
        
        # Get Step 2 model name (v2.2.1 - string instead of model object)
        step2_model_name = self._get_model_name_by_name(step2_params.model)
        step2_model_type = self._get_model_type_by_name(step2_params.model)
        
        try:
            response = self._client.models.generate_content(
                model=step2_model_name,
                contents=contents,
                config=step2_config,
            )
            text_content, confidence = self._parse_response(response.text)
            
            # Apply output formatting (v2.2.0)
            text_content = self._format_output(text_content, output_params)
            
            processing_time = int((time.perf_counter() - start_time) * 1000)
            
            logger.info(f"Two-step OCR complete: {filename}, confidence={confidence}")
            
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=text_content,
                confidence_score=confidence,
                model_used=step2_model_type,
                flash_attempted=True,
                pro_fallback_used=True,
                processing_time_ms=processing_time,
            )
            
        except Exception as e:
            logger.error(f"Two-step Step 2 correction failed: {e}")
            # Return Step 1 result as backup
            flash_result.document_id = document_id
            flash_result.source_file = filename
            flash_result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
            flash_result.text_content = self._format_output(flash_result.text_content, output_params)
            return flash_result
    
    def _get_model_name_by_name(self, model_name: str) -> str:
        """Get the full model name string by short name (v2.2.1)."""
        if "pro" in model_name.lower():
            return self._pro_model_name
        return self._flash_model_name
    
    def _get_model_type_by_name(self, model_name: str) -> ModelType:
        """Get the ModelType enum by model name."""
        if "pro" in model_name.lower():
            return ModelType.PRO
        return ModelType.FLASH
    
    def _format_output(self, text: str, output_params: OutputParams) -> str:
        """
        Apply output formatting options (v2.2.0).
        
        - preserve_newlines=False: Replace \\n with spaces
        - preserve_hyphens=False: Merge line-break hyphens (e.g., "recog-\\nnition" → "recognition")
        """
        import re
        
        if not text:
            return ""
        
        if not output_params.preserve_hyphens:
            # Merge line-break hyphens first (before removing newlines)
            # Pattern: hyphen followed by newline and next line continues word
            text = re.sub(r'-\n([a-zA-Z])', r'\1', text)
        
        if not output_params.preserve_newlines:
            # Replace newlines with spaces (but not double newlines which are paragraphs)
            text = re.sub(r'\n+', ' ', text)
            text = re.sub(r'\s+', ' ', text)  # Normalize multiple spaces
        
        return text.strip()
    
    def _process_with_pro_only_v2(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        step_params: StepParams,
        document_id: str,
        start_time: float,
    ) -> OCRResult:
        """
        Process with a specific step's model only (v2.2.1 - uses model name string).
        """
        model_name = self._get_model_name_by_name(step_params.model)
        model_type = self._get_model_type_by_name(step_params.model)
        
        logger.info(f"Processing with {step_params.model} only: {filename}")
        
        result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=model_name,
            model_type=model_type,
            temperature=step_params.temperature,
            top_p=step_params.top_p,
            top_k=step_params.top_k,
            custom_prompt=step_params.prompt,
        )
        
        result.document_id = document_id
        result.source_file = filename
        result.flash_attempted = False
        result.pro_fallback_used = True
        result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        return result

    def process_with_pro_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process with Pro model only (skip Flash).
        
        Used as fallback when Flash fails or for maximum quality.
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        # Parse per-request parameters
        ocr_params = OCRParams.from_dict(params or {})
        temperature = ocr_params.temperature
        
        return self._process_with_pro_only(
            image_data, mime_type, filename, temperature, document_id, start_time
        )
    
    # =========================================================================
    # v2.3.0: Manual Step 2 Verification Workflow Methods
    # =========================================================================
    
    def process_step1_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process with Step 1 (Flash) only, skip automatic Step 2.
        
        v2.3.0: For manual verification workflow.
        Client can review Step 1 results and decide which images need Step 2.
        
        Args:
            image_data: Raw image bytes
            mime_type: Image MIME type
            filename: Original filename
            params: OCR parameters (step1 config will be used)
        
        Returns:
            OCRResult with Step 1 transcription (confidence may be < threshold)
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        # Parse per-request parameters
        ocr_params = OCRParams.from_dict(params or {})
        step1_params = ocr_params.get_step1_params()
        output_params = ocr_params.get_output_params()
        
        logger.info(f"Step 1 ONLY: {filename}, model={step1_params.model}")
        
        # Get Step 1 model
        step1_model_name = self._get_model_name_by_name(step1_params.model)
        step1_model_type = self._get_model_type_by_name(step1_params.model)
        
        # Process with Step 1 config
        result = self._process_with_model_retry(
            image_data=image_data,
            mime_type=mime_type,
            model_name=step1_model_name,
            model_type=step1_model_type,
            temperature=step1_params.temperature,
            top_p=step1_params.top_p,
            top_k=step1_params.top_k,
            custom_prompt=step1_params.prompt,
        )
        
        # Set result metadata
        result.document_id = document_id
        result.source_file = filename
        result.flash_attempted = True
        result.pro_fallback_used = False  # Step 2 was NOT used
        result.processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        # Apply output formatting
        if result.text_content:
            result.text_content = self._format_output(result.text_content, output_params)
        
        logger.info(f"Step 1 ONLY complete: {filename}, confidence={result.confidence_score}")
        
        return result
    
    def process_step2_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        step1_result: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process with Step 2 (Pro) only, using provided Step 1 result.
        
        v2.3.0: For manual verification workflow.
        Client provides Step 1 result and explicitly requests Step 2 correction.
        
        Args:
            image_data: Raw image bytes
            mime_type: Image MIME type
            filename: Original filename
            step1_result: Text from Step 1 to correct
            params: OCR parameters (step2 config will be used)
        
        Returns:
            OCRResult with Step 2 corrected transcription
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        # Parse per-request parameters
        ocr_params = OCRParams.from_dict(params or {})
        step2_params = ocr_params.get_step2_params()
        output_params = ocr_params.get_output_params()
        
        logger.info(f"Step 2 ONLY: {filename}, model={step2_params.model}, step1_result_len={len(step1_result)}")
        
        # Build correction prompt
        if step2_params.prompt:
            # Custom prompt: inject Step 1 result using {step1_result} placeholder
            correction_prompt = step2_params.prompt.replace("{step1_result}", step1_result)
        else:
            # Default correction prompt
            correction_prompt = f"""You are an expert document layout analyst. Review the transcription below against the original image and correct any errors.

LAYOUT INSTRUCTIONS:
1. Line-for-Line Exactness: Break the text exactly where the lines break in the image
2. Marginalia Integration: Scan for notes in margins, insert inside square brackets [ ] at the end of the line it aligns with
3. Indentation: Preserve paragraph indentations and whitespace visually
4. No Meta-Data: Do not include headers like "Transcription:". Just output the raw text
5. Sanity Check: If the transcription contradicts the image (e.g., missed a word), trust the IMAGE and insert the missing word

TRANSCRIPTION TO REVIEW:
{step1_result}

OUTPUT FORMAT:
- First line: CONFIDENCE: <score between 0.0 and 1.0>
- Remaining lines: The corrected transcription with original formatting preserved
"""
        
        # Create content with image for Step 2
        image_part = Part.from_bytes(data=image_data, mime_type=mime_type)
        contents = [
            Content(
                role="user",
                parts=[
                    Part.from_text(text=correction_prompt),
                    image_part,
                ],
            ),
        ]
        
        # Step 2 generation config
        # v3.0: Config for Gemini 2.5 (supports thinking, 65535 max tokens)
        step2_config = GenerateContentConfig(
            temperature=step2_params.temperature,
            top_p=step2_params.top_p,
            top_k=step2_params.top_k,
            max_output_tokens=8192,  # Keep reasonable (max is 65535)
            thinking_config=ThinkingConfig(
                thinkingBudget=512,
                includeThoughts=False,
            ),
        )
        
        # Get Step 2 model
        step2_model_name = self._get_model_name_by_name(step2_params.model)
        step2_model_type = self._get_model_type_by_name(step2_params.model)
        
        try:
            response = self._client.models.generate_content(
                model=step2_model_name,
                contents=contents,
                config=step2_config,
            )
            text_content, confidence = self._parse_response(response.text)
            
            # Apply output formatting
            text_content = self._format_output(text_content, output_params)
            
            processing_time = int((time.perf_counter() - start_time) * 1000)
            
            logger.info(f"Step 2 ONLY complete: {filename}, confidence={confidence}")
            
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=text_content,
                confidence_score=confidence,
                model_used=step2_model_type,
                flash_attempted=True,  # Step 1 was already done (externally)
                pro_fallback_used=True,  # Step 2 WAS used
                processing_time_ms=processing_time,
            )
            
        except Exception as e:
            logger.error(f"Step 2 ONLY processing failed: {e}")
            # Return error result with Step 1 text as fallback
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=step1_result,  # Keep Step 1 result
                confidence_score=0.0,
                model_used=step2_model_type,
                flash_attempted=True,
                pro_fallback_used=False,  # Step 2 failed
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
                error=str(e),
            )


# =============================================================================
# v2.4.0: OpenRouter OCR Engine
# =============================================================================

class OpenRouterOCREngine:
    """
    OCR Engine using OpenRouter API instead of Vertex AI.
    
    v2.4.0 - Added for USA market (no GDPR restrictions).
    Same interface as OCREngine for seamless switching.
    
    Supported models:
    - gemini-3-flash-preview (google/gemini-3-flash-preview)
    - gemini-3-pro-preview (google/gemini-3-pro-preview)
    """
    
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    
    # Model mapping: internal name -> OpenRouter model ID
    MODELS = {
        "gemini-3-flash-preview": "google/gemini-3-flash-preview",
        "gemini-3-pro-preview": "google/gemini-3-pro-preview",
        "flash": "google/gemini-3-flash-preview",
        "pro": "google/gemini-3-pro-preview",
    }
    
    # Same system prompt as Vertex engine
    SYSTEM_PROMPT = """You are an expert paleographer specialized in 17th to 19th-century colonial and European handwriting.
Your task is to accurately transcribe handwritten historical documents.

Expertise Areas:
- Colonial American court records and legal documents
- European administrative manuscripts
- Archaic spellings and period-specific orthography
- Legal abbreviations and notarial conventions
- Latin phrases commonly used in official documents

Instructions:
1. Extract ALL text visible in the image, preserving the EXACT original structure
2. Maintain paragraph breaks, indentation, and line formatting as they appear
3. Preserve archaic spellings exactly as written (do not modernize)
4. Recognize and expand common abbreviations where clear (e.g., "ye" for "the")
5. Use [unclear] for genuinely illegible portions
6. Report your confidence level (0.0 to 1.0) for the transcription

Output format:
- First line: CONFIDENCE: <score between 0.0 and 1.0>
- Remaining lines: The transcribed text with original formatting preserved

Example:
CONFIDENCE: 0.95
At a Court held for Rockingham County
on the first day of February 1791
Present:
William Benjamin Smith, Esquire
"""

    def __init__(self):
        """
        Initialize the OpenRouter OCR Engine.
        
        v5.3: Added persistent httpx.Client for connection reuse.
        This prevents creating new TCP/TLS connections per request.
        """
        self._api_key = settings.openrouter_api_key
        if not self._api_key:
            logger.warning("OPENROUTER_API_KEY not set - OpenRouter engine will fail if used")
        
        # v5.3: Persistent HTTP client for connection pooling (memory optimization)
        # Keeps connections alive and reuses them across multiple API calls
        self._http_client = httpx.Client(
            timeout=120.0,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=300.0,  # 5 minutes
            ),
        )
        
        # Use same model names as Vertex for consistency
        self._flash_model_name = "gemini-3-flash-preview"
        # v5.2: Use GPT-4o for Step 2 instead of slow Gemini 3 Pro
        self._pro_model_name = "gpt-4o"
        
        # Default settings
        self._default_confidence_threshold = settings.confidence_threshold
        self._default_temperature = settings.temperature
        
        logger.info(f"OpenRouter OCR Engine initialized: flash={self._flash_model_name}, pro={self._pro_model_name}")

    def _get_mime_type(self, image_path: str) -> str:
        """Determine MIME type from file extension."""
        suffix = Path(image_path).suffix.lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
        }
        return mime_types.get(suffix, "image/jpeg")

    async def _call_openrouter(
        self,
        image_data: bytes,
        mime_type: str,
        prompt: str,
        model: str,
        temperature: float = 0.0,
        top_p: float = 1.0,
        top_k: int = 40,
        max_tokens: int = 8192,
    ) -> tuple[str, float]:
        """
        Call OpenRouter API with image and prompt.
        
        Returns:
            tuple of (text_content, confidence_score)
        """
        if not self._api_key:
            raise ValueError("OPENROUTER_API_KEY not configured")
        
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        model_id = self.MODELS.get(model, self.MODELS["flash"])
        
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}
                        }
                    ]
                }
            ],
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "max_tokens": max_tokens
        }
        
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://biqe-htr-api.run.app",
            "X-Title": "BIQE HTR OpenRouter API"
        }
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self.OPENROUTER_BASE_URL,
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
        
        # Handle OpenRouter response - validate structure before accessing
        if "error" in data:
            raise ValueError(f"OpenRouter API error: {data['error']}")
        
        if "choices" not in data or len(data["choices"]) == 0:
            logger.error(f"OpenRouter returned invalid response (no choices): {data}")
            raise ValueError(f"OpenRouter returned no choices in response")
        
        if "message" not in data["choices"][0] or "content" not in data["choices"][0]["message"]:
            logger.error(f"OpenRouter returned invalid message structure: {data['choices'][0]}")
            raise ValueError("OpenRouter returned invalid message structure")
        
        content = data["choices"][0]["message"]["content"]
        if not content:
            logger.warning("OpenRouter returned empty content")
            return "", 0.0
        
        text, confidence = self._parse_response(content)
        return text, confidence

    def _call_openrouter_sync(
        self,
        image_data: bytes,
        mime_type: str,
        prompt: str,
        model: str,
        temperature: float = 0.0,
        top_p: float = 1.0,
        top_k: int = 40,
        max_tokens: int = 8192,
    ) -> tuple[str, float]:
        """
        Synchronous version of _call_openrouter for compatibility with OCREngine interface.
        """
        if not self._api_key:
            raise ValueError("OPENROUTER_API_KEY not configured")
        
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        model_id = self.MODELS.get(model, self.MODELS["flash"])
        
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}
                        }
                    ]
                }
            ],
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "max_tokens": max_tokens
        }
        
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://biqe-htr-api.run.app",
            "X-Title": "BIQE HTR OpenRouter API"
        }
        
        # v5.3: Use persistent httpx client for connection reuse (memory optimization)
        # This prevents creating new TCP/TLS connections per API call
        response = self._http_client.post(
            self.OPENROUTER_BASE_URL,
            json=payload,
            headers=headers
        )
        response.raise_for_status()
        data = response.json()
        
        # Handle OpenRouter response - validate structure before accessing
        if "error" in data:
            raise ValueError(f"OpenRouter API error: {data['error']}")
        
        if "choices" not in data or len(data["choices"]) == 0:
            logger.error(f"OpenRouter returned invalid response (no choices): {data}")
            raise ValueError(f"OpenRouter returned no choices in response")
        
        if "message" not in data["choices"][0] or "content" not in data["choices"][0]["message"]:
            logger.error(f"OpenRouter returned invalid message structure: {data['choices'][0]}")
            raise ValueError("OpenRouter returned invalid message structure")
        
        content = data["choices"][0]["message"]["content"]
        
        # v5.3: Explicit memory cleanup of large objects (memory optimization)
        # The base64 string and payload can be large (1-5MB per image)
        del payload
        del data
        
        if not content:
            logger.warning("OpenRouter returned empty content")
            return "", 0.0
        
        text, confidence = self._parse_response(content)
        
        # v5.3: Clean up content string after parsing
        del content
        
        return text, confidence

    def _parse_response(self, response_text: str) -> tuple[str, float]:
        """
        Parse model response to extract text and confidence score.
        
        v2.4.2: Fixed to properly extract TRANSCRIPTION section instead of
        incorrectly returning metadata as content.
        """
        # Handle None or empty response
        if not response_text:
            logger.warning("Empty or None response_text received in OpenRouter._parse_response")
            return "", 0.0
        
        # Extract transcription block
        transcription_text = ""
        if "TRANSCRIPTION:" in response_text:
            # Split on TRANSCRIPTION: marker
            parts = response_text.split("TRANSCRIPTION:", 1)
            if len(parts) > 1:
                # Get text between TRANSCRIPTION: and METADATA:
                content = parts[1]
                if "METADATA:" in content:
                    transcription_text = content.split("METADATA:")[0].strip()
                else:
                    # No METADATA section, take everything after TRANSCRIPTION:
                    transcription_text = content.strip()
        else:
            # Fallback: no structured format, use entire response
            # (for backward compatibility with old responses)
            transcription_text = response_text.strip()
        
        # Extract confidence from METADATA section or CONFIDENCE: line
        confidence = 0.85  # Default confidence if not parsed
        if "CONFIDENCE:" in response_text.upper():
            try:
                # Find the line containing CONFIDENCE:
                conf_line = [l for l in response_text.split("\n") if "CONFIDENCE:" in l.upper()][0]
                confidence_str = conf_line.split(":", 1)[1].strip()
                confidence = float(confidence_str)
                confidence = max(0.0, min(1.0, confidence))
            except (ValueError, IndexError, AttributeError):
                logger.warning(f"Failed to parse confidence score from response")
                pass
        
        return transcription_text, confidence

    def _get_model_name_by_name(self, model_name: str) -> str:
        """Get the model name by short name."""
        if "pro" in model_name.lower():
            return self._pro_model_name
        return self._flash_model_name

    def _get_model_type_by_name(self, model_name: str) -> ModelType:
        """Get the ModelType enum by model name."""
        if "pro" in model_name.lower():
            return ModelType.PRO
        return ModelType.FLASH

    def _format_output(self, text: str, output_params: OutputParams) -> str:
        """Apply output formatting options (same as Vertex engine)."""
        if not text:
            return ""
        
        if not output_params.preserve_hyphens:
            text = re.sub(r'-\n([a-zA-Z])', r'\1', text)
        
        if not output_params.preserve_newlines:
            text = re.sub(r'\n+', ' ', text)
            text = re.sub(r'\s+', ' ', text)
        
        return text.strip()

    def process_image(
        self,
        image_path: str | Path,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """Process an image file through OpenRouter."""
        path = Path(image_path)
        with open(path, "rb") as f:
            image_data = f.read()
        
        mime_type = self._get_mime_type(str(path))
        return self.process_image_bytes(image_data, mime_type, path.name, params)

    def process_image_bytes(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process image bytes through OpenRouter API.
        Same interface as OCREngine for seamless switching.
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        ocr_params = OCRParams.from_dict(params or {})
        model_choice = ocr_params.model
        confidence_threshold = ocr_params.confidence_threshold
        step1_params = ocr_params.get_step1_params()
        
        logger.info(
            f"OpenRouter OCR: document_id={document_id}, filename={filename}, "
            f"model={model_choice}, threshold={confidence_threshold}"
        )

        try:
            # Use step1 params for single-step processing
            text, confidence = self._call_openrouter_sync(
                image_data=image_data,
                mime_type=mime_type,
                prompt=step1_params.prompt or self.SYSTEM_PROMPT,
                model=step1_params.model,
                temperature=step1_params.temperature,
                top_p=step1_params.top_p,
                top_k=step1_params.top_k,
            )
            
            model_type = self._get_model_type_by_name(step1_params.model)
            
            # Apply output formatting
            output_params = ocr_params.get_output_params()
            text = self._format_output(text, output_params)
            
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=text,
                confidence_score=confidence,
                model_used=model_type,
                flash_attempted=True,
                pro_fallback_used=False,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
            )
            
        except Exception as e:
            logger.error(f"OpenRouter OCR failed: {e}")
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content="",
                confidence_score=0.0,
                model_used=ModelType.FLASH,
                flash_attempted=True,
                pro_fallback_used=False,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
                error=str(e),
            )

    def process_two_step(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Two-step OCR using OpenRouter: Flash → Pro correction.
        Same interface as OCREngine for seamless switching.
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        ocr_params = OCRParams.from_dict(params or {})
        confidence_threshold = ocr_params.confidence_threshold
        step1_params = ocr_params.get_step1_params()
        step2_params = ocr_params.get_step2_params()
        output_params = ocr_params.get_output_params()
        
        logger.info(f"OpenRouter two-step OCR: {filename}")
        
        try:
            # Step 1: Flash model
            text1, confidence1 = self._call_openrouter_sync(
                image_data=image_data,
                mime_type=mime_type,
                prompt=step1_params.prompt or self.SYSTEM_PROMPT,
                model=step1_params.model,
                temperature=step1_params.temperature,
                top_p=step1_params.top_p,
                top_k=step1_params.top_k,
            )
            
            if confidence1 >= confidence_threshold:
                logger.info(f"Two-step: Step 1 high confidence ({confidence1}), skipping Step 2")
                return OCRResult(
                    document_id=document_id,
                    source_file=filename,
                    text_content=self._format_output(text1, output_params),
                    confidence_score=confidence1,
                    model_used=self._get_model_type_by_name(step1_params.model),
                    flash_attempted=True,
                    pro_fallback_used=False,
                    processing_time_ms=int((time.perf_counter() - start_time) * 1000),
                )
            
            # Step 2: Pro model with correction prompt
            correction_prompt = step2_params.prompt.replace("{step1_result}", text1) if step2_params.prompt else f"""You are an expert document layout analyst. Review the transcription below against the original image and correct any errors.

LAYOUT INSTRUCTIONS:
1. Line-for-Line Exactness: Break the text exactly where the lines break in the image
2. Marginalia Integration: Scan for notes in margins, insert inside square brackets [ ] at the end of the line it aligns with
3. Indentation: Preserve paragraph indentations and whitespace visually
4. No Meta-Data: Do not include headers like "Transcription:". Just output the raw text
5. Sanity Check: If the transcription contradicts the image (e.g., missed a word), trust the IMAGE and insert the missing word

TRANSCRIPTION TO REVIEW:
{text1}

OUTPUT FORMAT:
- First line: CONFIDENCE: <score between 0.0 and 1.0>
- Remaining lines: The corrected transcription with original formatting preserved
"""
            
            text2, confidence2 = self._call_openrouter_sync(
                image_data=image_data,
                mime_type=mime_type,
                prompt=correction_prompt,
                model=step2_params.model,
                temperature=step2_params.temperature,
                top_p=step2_params.top_p,
                top_k=step2_params.top_k,
            )
            
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=self._format_output(text2, output_params),
                confidence_score=confidence2,
                model_used=self._get_model_type_by_name(step2_params.model),
                flash_attempted=True,
                pro_fallback_used=True,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
            )
            
        except Exception as e:
            logger.error(f"OpenRouter two-step OCR failed: {e}")
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content="",
                confidence_score=0.0,
                model_used=ModelType.FLASH,
                flash_attempted=True,
                pro_fallback_used=False,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
                error=str(e),
            )

    def process_step1_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process with Step 1 only (for manual verification workflow).
        Same interface as OCREngine.
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        ocr_params = OCRParams.from_dict(params or {})
        step1_params = ocr_params.get_step1_params()
        output_params = ocr_params.get_output_params()
        
        logger.info(f"OpenRouter Step 1 ONLY: {filename}")
        
        try:
            text, confidence = self._call_openrouter_sync(
                image_data=image_data,
                mime_type=mime_type,
                prompt=step1_params.prompt or self.SYSTEM_PROMPT,
                model=step1_params.model,
                temperature=step1_params.temperature,
                top_p=step1_params.top_p,
                top_k=step1_params.top_k,
            )
            
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=self._format_output(text, output_params),
                confidence_score=confidence,
                model_used=self._get_model_type_by_name(step1_params.model),
                flash_attempted=True,
                pro_fallback_used=False,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
            )
            
        except Exception as e:
            logger.error(f"OpenRouter Step 1 failed: {e}")
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content="",
                confidence_score=0.0,
                model_used=ModelType.FLASH,
                flash_attempted=True,
                pro_fallback_used=False,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
                error=str(e),
            )

    def process_step2_only(
        self,
        image_data: bytes,
        mime_type: str,
        filename: str,
        step1_result: str,
        params: Optional[dict] = None,
    ) -> OCRResult:
        """
        Process with Step 2 only using provided Step 1 result.
        Same interface as OCREngine.
        """
        start_time = time.perf_counter()
        document_id = str(uuid4())
        
        ocr_params = OCRParams.from_dict(params or {})
        step2_params = ocr_params.get_step2_params()
        output_params = ocr_params.get_output_params()
        
        logger.info(f"OpenRouter Step 2 ONLY: {filename}")
        
        correction_prompt = step2_params.prompt.replace("{step1_result}", step1_result) if step2_params.prompt else f"""You are an expert document layout analyst. Review the transcription below against the original image and correct any errors.

LAYOUT INSTRUCTIONS:
1. Line-for-Line Exactness: Break the text exactly where the lines break in the image
2. Marginalia Integration: Scan for notes in margins, insert inside square brackets [ ] at the end of the line it aligns with
3. Indentation: Preserve paragraph indentations and whitespace visually
4. No Meta-Data: Do not include headers like "Transcription:". Just output the raw text
5. Sanity Check: If the transcription contradicts the image (e.g., missed a word), trust the IMAGE and insert the missing word

TRANSCRIPTION TO REVIEW:
{step1_result}

OUTPUT FORMAT:
- First line: CONFIDENCE: <score between 0.0 and 1.0>
- Remaining lines: The corrected transcription with original formatting preserved
"""
        
        try:
            text, confidence = self._call_openrouter_sync(
                image_data=image_data,
                mime_type=mime_type,
                prompt=correction_prompt,
                model=step2_params.model,
                temperature=step2_params.temperature,
                top_p=step2_params.top_p,
                top_k=step2_params.top_k,
            )
            
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=self._format_output(text, output_params),
                confidence_score=confidence,
                model_used=self._get_model_type_by_name(step2_params.model),
                flash_attempted=True,
                pro_fallback_used=True,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
            )
            
        except Exception as e:
            logger.error(f"OpenRouter Step 2 failed: {e}")
            return OCRResult(
                document_id=document_id,
                source_file=filename,
                text_content=step1_result,  # Keep Step 1 result
                confidence_score=0.0,
                model_used=self._get_model_type_by_name(step2_params.model),
                flash_attempted=True,
                pro_fallback_used=False,
                processing_time_ms=int((time.perf_counter() - start_time) * 1000),
                error=str(e),
            )


# =============================================================================
# v5.3: Singleton Factory Function for Provider Selection (Memory Fix)
# =============================================================================

# Global singleton instances for OCR engines (memory optimization)
# These are reused across all requests to prevent connection exhaustion
_vertex_engine: Optional[OCREngine] = None
_openrouter_engine: Optional[OpenRouterOCREngine] = None


def get_ocr_engine(provider: str = "vertex") -> OCREngine | OpenRouterOCREngine:
    """
    Factory function to get the appropriate OCR engine based on provider.
    
    v5.3: Now uses singleton pattern to prevent memory leaks.
    - Single OCR engine instance per provider
    - gRPC/HTTP connections are reused
    - Memory usage stays constant regardless of request count
    
    Args:
        provider: "vertex" or "openrouter"
    
    Returns:
        OCREngine (Vertex AI) or OpenRouterOCREngine based on provider
    
    Usage:
        engine = get_ocr_engine(provider="openrouter")
        result = engine.process_image_bytes(...)
    """
    global _vertex_engine, _openrouter_engine
    
    if provider == "openrouter":
        if _openrouter_engine is None:
            logger.info("Creating OpenRouter OCR engine (singleton)")
            _openrouter_engine = OpenRouterOCREngine()
        return _openrouter_engine
    else:
        if _vertex_engine is None:
            logger.info("Creating Vertex AI OCR engine (singleton)")
            _vertex_engine = OCREngine()
        return _vertex_engine
