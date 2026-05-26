FROM python:3.13-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System packages: Python 3.13, build tools, git, Node.js 20
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        libglib2.0-0 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Remove the PEP 668 guard so pip works freely inside this container
RUN find /usr/lib/python3* -name EXTERNALLY-MANAGED -delete

# Reinstall wheel + setuptools via pip (--ignore-installed shadows the apt versions,
# giving them a RECORD file so future pip operations can upgrade them freely)
RUN pip3 install --no-cache-dir --ignore-installed \
        wheel \
        setuptools

# Pre-install raccoon ecosystem + CI tooling
RUN pip3 install --no-cache-dir \
        raccoon-library \
        raccoon-transport \
        pytest \
        pytest-asyncio \
        build
