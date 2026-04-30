FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System packages: Python 3.12, build tools, git, curl — no python3-pip (bootstrapped below)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        python3 \
        python3-dev \
    # Node.js 20 via NodeSource
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Bootstrap pip directly — avoids apt-managed wheel/setuptools that have no RECORD file
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# Pre-install raccoon ecosystem + CI tooling so the test job works without setup steps
RUN pip3 install --no-cache-dir \
        wheel \
        setuptools \
        raccoon-stubs \
        raccoon-transport \
        pytest \
        pytest-asyncio \
        build
