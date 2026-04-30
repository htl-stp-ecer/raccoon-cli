FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System packages: Python 3.12, build tools, git, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        python3 \
        python3-pip \
        python3-dev \
        python3-venv \
    # Node.js 20 via NodeSource
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Remove apt-managed Python packages that pip can't uninstall/upgrade (no RECORD file)
RUN apt-get remove -y python3-wheel python3-setuptools 2>/dev/null || true \
    && rm -f /usr/lib/python3*/EXTERNALLY-MANAGED

# Pre-install raccoon ecosystem + CI tooling so the test job works without setup steps
RUN pip3 install --no-cache-dir \
        wheel \
        setuptools \
        raccoon-stubs \
        raccoon-transport \
        pytest \
        pytest-asyncio \
        build
