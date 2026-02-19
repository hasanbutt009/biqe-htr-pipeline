# BIQE HTR Pipeline - Makefile
# =============================
# Common development commands

.PHONY: help install test lint format clean deploy

# Default target
help:
	@echo "BIQE HTR Pipeline - Development Commands"
	@echo "========================================="
	@echo ""
	@echo "  make install     Install dependencies"
	@echo "  make test        Run tests with coverage"
	@echo "  make lint        Run linting (ruff + mypy)"
	@echo "  make format      Format code with ruff"
	@echo "  make clean       Remove build artifacts"
	@echo "  make docker      Build Docker images"
	@echo "  make deploy      Deploy to GCP"
	@echo ""

# Install dependencies
install:
	pip install --upgrade pip
	pip install -r requirements.txt
	pip install -r requirements-api.txt

# Run tests
test:
	pytest tests/ -v --cov=src --cov-report=html --cov-report=term-missing

# Quick tests without coverage
test-quick:
	pytest tests/ -v --tb=short

# Linting
lint:
	ruff check src/ tests/
	mypy src/

# Format code
format:
	ruff format src/ tests/
	ruff check --fix src/ tests/

# Clean build artifacts
clean:
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov .coverage
	rm -rf build dist *.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Build Docker images
docker:
	docker build -f deploy/Dockerfile -t biqe-htr-job:latest .
	docker build -f deploy/Dockerfile.api -t biqe-htr-api:latest .

# Local development with emulators
dev:
	docker-compose up -d pubsub-emulator gcs-emulator
	@echo "Emulators started. Run 'docker-compose logs -f' to see logs."

# Stop local development
dev-stop:
	docker-compose down -v

# Deploy to GCP (requires gcloud auth)
deploy:
	@echo "Deploying to GCP..."
	gcloud builds submit --config deploy/cloudbuild.yaml
