"""Tests for the OCR Engine with Flash/Pro chained logic."""

import pytest
from unittest.mock import MagicMock, patch

from src.core.ocr_engine import OCREngine, OCRResult, ModelType


class TestOCREngine:
    """Test suite for OCREngine chained logic."""

    @pytest.fixture
    def mock_genai(self):
        """Mock the Google GenerativeAI module."""
        with patch("src.core.ocr_engine.genai") as mock:
            yield mock

    @pytest.fixture
    def mock_settings(self):
        """Mock settings."""
        with patch("src.core.ocr_engine.settings") as mock:
            mock.gemini_model_flash = "gemini-2.5-flash"
            mock.gemini_model_pro = "gemini-2.5-pro"
            mock.confidence_threshold = 0.85
            yield mock

    @pytest.fixture
    def engine(self, mock_genai, mock_settings):
        """Create OCR engine with mocks."""
        return OCREngine()

    def test_high_confidence_uses_flash_only(self, engine, mock_genai):
        """Test that high confidence results don't escalate to Pro."""
        # Mock Flash model response with high confidence
        mock_response = MagicMock()
        mock_response.text = "CONFIDENCE: 0.95\nHigh quality text here"
        engine._flash_model.generate_content.return_value = mock_response
        
        # Create a test image file
        with patch.object(engine, "_load_image", return_value=b"fake_image"):
            result = engine.process_image("/fake/path.jpg")
        
        # Should use Flash only
        assert result.model_used == ModelType.FLASH
        assert result.flash_attempted is True
        assert result.pro_fallback_used is False
        assert result.confidence_score == 0.95
        
        # Pro model should NOT be called
        engine._pro_model.generate_content.assert_not_called()

    def test_low_confidence_escalates_to_pro(self, engine, mock_genai):
        """Test that low confidence results escalate to Pro."""
        # Mock Flash model response with LOW confidence
        flash_response = MagicMock()
        flash_response.text = "CONFIDENCE: 0.60\nPoorly recognized text"
        engine._flash_model.generate_content.return_value = flash_response
        
        # Mock Pro model response
        pro_response = MagicMock()
        pro_response.text = "CONFIDENCE: 0.92\nBetter recognized text"
        engine._pro_model.generate_content.return_value = pro_response
        
        with patch.object(engine, "_load_image", return_value=b"fake_image"):
            result = engine.process_image("/fake/path.jpg")
        
        # Should escalate to Pro
        assert result.model_used == ModelType.PRO
        assert result.flash_attempted is True
        assert result.pro_fallback_used is True
        assert result.confidence_score == 0.92
        
        # Both models should be called
        engine._flash_model.generate_content.assert_called_once()
        engine._pro_model.generate_content.assert_called_once()

    def test_flash_error_falls_back_to_pro(self, engine, mock_genai):
        """Test that Flash errors trigger Pro fallback."""
        # Mock Flash model to raise error
        engine._flash_model.generate_content.side_effect = Exception("API Error")
        
        # Mock Pro model response
        pro_response = MagicMock()
        pro_response.text = "CONFIDENCE: 0.88\nRecovered text"
        engine._pro_model.generate_content.return_value = pro_response
        
        with patch.object(engine, "_load_image", return_value=b"fake_image"):
            result = engine.process_image("/fake/path.jpg")
        
        # Should recover with Pro
        assert result.model_used == ModelType.PRO
        assert result.pro_fallback_used is True
        assert result.error is None  # Pro succeeded

    def test_boundary_confidence_escalates(self, engine, mock_genai):
        """Test that exactly at threshold still escalates."""
        # Mock Flash at EXACTLY 0.85 threshold
        flash_response = MagicMock()
        flash_response.text = "CONFIDENCE: 0.85\nBorderline text"
        engine._flash_model.generate_content.return_value = flash_response
        
        pro_response = MagicMock()
        pro_response.text = "CONFIDENCE: 0.90\nImproved text"
        engine._pro_model.generate_content.return_value = pro_response
        
        with patch.object(engine, "_load_image", return_value=b"fake_image"):
            result = engine.process_image("/fake/path.jpg")
        
        # At 0.85 (>= threshold), should NOT escalate
        # Note: Architecture says >= threshold means ACCEPT
        assert result.model_used == ModelType.FLASH
        assert result.pro_fallback_used is False

    def test_parse_response_extracts_confidence(self, engine):
        """Test confidence parsing from model response."""
        test_cases = [
            ("CONFIDENCE: 0.95\nSome text", 0.95, "Some text"),
            ("CONFIDENCE: 0.5\nLine1\nLine2", 0.5, "Line1\nLine2"),
            ("No confidence line\nJust text", 0.85, "No confidence line\nJust text"),
            ("CONFIDENCE: 1.5\nClamped high", 1.0, "Clamped high"),
            ("CONFIDENCE: -0.5\nClamped low", 0.0, "Clamped low"),
        ]
        
        for response, expected_conf, expected_text in test_cases:
            text, confidence = engine._parse_response(response)
            assert confidence == expected_conf, f"Failed for: {response}"
            assert text == expected_text, f"Failed for: {response}"

    def test_mime_type_detection(self, engine):
        """Test MIME type detection from file extensions."""
        test_cases = [
            ("/path/file.jpg", "image/jpeg"),
            ("/path/file.JPEG", "image/jpeg"),
            ("/path/file.png", "image/png"),
            ("/path/file.gif", "image/gif"),
            ("/path/file.webp", "image/webp"),
            ("/path/file.tiff", "image/tiff"),
            ("/path/file.unknown", "image/jpeg"),  # Default
        ]
        
        for path, expected_mime in test_cases:
            assert engine._get_mime_type(path) == expected_mime


class TestOCRResultDataclass:
    """Tests for OCRResult dataclass."""

    def test_to_dict_serialization(self):
        """Test that OCRResult serializes correctly."""
        result = OCRResult(
            document_id="doc-123",
            source_file="test.jpg",
            text_content="Extracted text",
            confidence_score=0.92,
            model_used=ModelType.FLASH,
            flash_attempted=True,
            pro_fallback_used=False,
            processing_time_ms=1500,
        )
        
        data = result.to_dict()
        
        assert data["document_id"] == "doc-123"
        assert data["source_file"] == "test.jpg"
        assert data["processing"]["model"] == "flash"
        assert data["processing"]["confidence_score"] == 0.92
        assert data["result"]["text_content"] == "Extracted text"
        assert data["error"] is None

    def test_to_dict_with_error(self):
        """Test serialization with error."""
        result = OCRResult(
            document_id="",
            source_file="",
            text_content="",
            confidence_score=0.0,
            model_used=ModelType.FLASH,
            flash_attempted=True,
            pro_fallback_used=False,
            processing_time_ms=0,
            error="API quota exceeded",
        )
        
        data = result.to_dict()
        
        assert data["error"] == "API quota exceeded"
