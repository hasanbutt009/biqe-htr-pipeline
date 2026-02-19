# Contributing to BIQE HTR Pipeline

## Development Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd biqe-htr-pipeline
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   make install
   # or
   pip install -r requirements.txt -r requirements-api.txt
   ```

4. **Setup environment**
   ```bash
   cp .env.example .env
   # Edit .env with your GCP project details
   ```

## Code Standards

### Formatting & Linting

We use **Ruff** for linting and formatting, **MyPy** for type checking.

```bash
# Check code
make lint

# Auto-format
make format
```

### Testing

All code must have tests. Run tests with:

```bash
# Full test suite with coverage
make test

# Quick tests
make test-quick
```

### Type Hints

All functions must have type hints:

```python
# Good
def process_image(path: str) -> OCRResult:
    ...

# Bad
def process_image(path):
    ...
```

## Git Workflow

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make changes and add tests
3. Run `make lint test`
4. Commit with descriptive message
5. Push and create Pull Request

## Architecture Rules

### CRITICAL - These MUST be followed:

1. **Cloud Run Jobs ONLY** - Never use Vertex AI Batch
2. **Atomic Write Pattern** - All GCS writes MUST use `AtomicStorageManager`
3. **Flash-First** - Always try Gemini Flash before Pro
4. **Pub/Sub Events** - All status changes MUST emit events

### Adding New Features

1. Update `.memorybank/` docs if architecture changes
2. Add tests for new functionality
3. Update README if user-facing changes
