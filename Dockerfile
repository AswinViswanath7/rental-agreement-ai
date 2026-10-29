# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# RentEase production Dockerfile.
# Multi-stage: build stage installs pinned dependencies; final stage runs as
# a non-root user with only the runtime needs.
#
# NOTE: Pin the base image digest (python:3.11-slim@sha256:...) before a
# production release. The tag below is a placeholder; replace with the digest
# of the exact image you audited.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System dependencies: tesseract + language packs, Noto fonts, build tools.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        tesseract-ocr-tam \
        tesseract-ocr-tel \
        tesseract-ocr-mar \
        tesseract-ocr-guj \
        tesseract-ocr-ben \
        tesseract-ocr-kan \
        tesseract-ocr-pan \
        tesseract-ocr-urd \
        fonts-noto \
        fonts-noto-cjk \
        fonts-noto-devanagari \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (pinned with hashes via requirements.txt / uv-style lock).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LLM_PROVIDER=none \
    TRANSLATION_ENABLED_LANGUAGES=en \
    RESULT_TTL_SECONDS=3600 \
    SESSION_COOKIE_SECURE=true

WORKDIR /app

# Runtime system deps (tesseract + language packs + Noto fonts, no build tools).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        tesseract-ocr-tam \
        tesseract-ocr-tel \
        tesseract-ocr-mar \
        tesseract-ocr-guj \
        tesseract-ocr-ben \
        tesseract-ocr-kan \
        tesseract-ocr-pan \
        tesseract-ocr-urd \
        fonts-noto \
        fonts-noto-cjk \
        fonts-noto-devanagari \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for the application.
RUN useradd --create-home --shell /bin/bash rentease

# Copy installed packages from builder and the app code.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY --chown=rentease:rentease . .

USER rentease

EXPOSE 5000

# HEALTHCHECK polls /ready.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import os, sys, urllib.request; port=os.getenv('PORT', '5000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/ready', timeout=5).status==200 else 1)"

# Jobs and results are in process memory, so keep one worker and one instance.
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 120"]
