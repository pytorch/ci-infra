# pypi-cache: pypiserver + wheel builder image
#
# Two roles in one image:
#   1. Server  — pypiserver serves wheels from per-CUDA wheelhouse directories,
#                falls back to upstream PyPI for cache misses
#   2. Builder — periodic process parses access logs for .tar.gz (sdist) downloads
#                and pre-builds wheels for configured CUDA × Python matrix
#
# The DaemonSet runs this image with the orchestrator script (ConfigMap-delivered)
# that manages both roles.

# ---- Stage 1: Build dependencies ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    gfortran \
    && rm -rf /var/lib/apt/lists/*

# Install pypiserver and passlib (for auth, optional)
RUN pip install --no-cache-dir \
    "pypiserver[cache]==2.3.2" \
    watchdog==6.0.0

# ---- Stage 2: Multi-Python builder base ----
# Python 3.11 + 3.12 for building version-specific wheels.
# Additional versions can be added here.
FROM python:3.12-slim AS python-base

# Install Python 3.11 alongside 3.12
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- Stage 3: Runtime image ----
FROM python:3.12-slim

# Build tools needed for compiling wheels from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    gfortran \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pypiserver from builder stage
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/pypi-server /usr/local/bin/pypi-server

# ---- CUDA Toolkit Section ----
# To support building CUDA extension wheels (flash-attn, triton, etc.),
# uncomment and configure the CUDA toolkit versions needed.
#
# Option A: Install CUDA toolkits directly (large image, ~4GB per version)
#   RUN curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/... | ...
#
# Option B: Use nvidia/cuda base image instead of python:3.12-slim
#   Change the FROM line above to: FROM nvidia/cuda:12.1.0-devel-ubuntu22.04
#   Then install Python on top.
#
# Option C: Mount CUDA toolkit from the host (if available on runner nodes)
#   The DaemonSet can mount /usr/local/cuda from the host.
#
# For now, CPU-only wheel building is supported. CUDA builds require
# extending this Dockerfile with one of the options above.

# Default working directory
WORKDIR /app

# Run as non-root for security
USER 65534

# The orchestrator script is delivered via ConfigMap mount at /scripts/
# It manages pypiserver lifecycle and periodic builder runs.
ENTRYPOINT ["python3", "/scripts/orchestrator.py"]
