# Technology Stack

**Analysis Date:** 2026-01-18

## Languages

**Primary:**
- Python 3.8+ (>=3.8 specified) - All application code

**Secondary:**
- LCM (Lightweight Communications and Marshalling) - Message serialization definitions in `lcm-messages/types/`
- Bash - Deployment scripts (`deploy.sh`)

## Runtime

**Environment:**
- Python 3.8+ (venv uses Python 3.13)
- Targets both development machines (laptop/desktop) and Raspberry Pi

**Package Manager:**
- uv (lock file: `uv.lock`)
- pip/setuptools for package installation
- Lockfile: present (`uv.lock`)

## Frameworks

**Core:**
- FastAPI >=0.100 - REST API server for Pi-side daemon (`raccoon/server/`)
- Click >=8.0 - CLI framework for command-line interface (`raccoon/cli.py`)

**Web/Network:**
- uvicorn[standard] >=0.20 - ASGI server for FastAPI
- websockets >=10.0 - WebSocket support for real-time streaming
- httpx >=0.24 - Async HTTP client for API communication

**SSH/Remote:**
- paramiko >=3.0 - SSH and SFTP for file synchronization

**Testing:**
- Not detected in pyproject.toml (no test dependencies specified)

**Build/Dev:**
- setuptools >=61.0 - Build backend
- wheel >=0.45.1 - Wheel packaging
- black - Code formatting (used in code generation)

## Key Dependencies

**Critical:**
- `click>=8.0` - CLI command structure (`raccoon/cli.py`)
- `fastapi>=0.100` - Server API framework (`raccoon/server/app.py`)
- `paramiko>=3.0` - SSH/SFTP file sync (`raccoon/client/sftp_sync.py`)
- `httpx>=0.24` - HTTP client for Pi communication (`raccoon/client/api.py`)
- `websocket-client>=1.0` - WebSocket client for output streaming

**Configuration/Data:**
- `pyyaml` - YAML configuration parsing (`raccoon.project.yml` files)
- `jinja2>=3.0` - Template engine for code generation
- `jinja2-time>=0.2` - Time extension for Jinja2

**UI/Output:**
- `rich>=10.0` - Terminal formatting, progress bars, and logging

**Infrastructure:**
- `uvicorn[standard]>=0.20` - ASGI server
- `websockets>=10.0` - WebSocket protocol support

**Pi-specific (runtime only):**
- `lcm` - LCM library (imported conditionally, not in pyproject.toml)
- `libstp` - Hardware abstraction library for Botball (external dependency)

## Configuration

**Environment Variables:**
- `RACCOON_HOST` - Server bind address (default: "0.0.0.0")
- `RACCOON_PORT` - Server port (default: 8421)
- `RACCOON_PROJECTS_DIR` - Projects directory on Pi (default: ~/programs)
- `RPI_HOST` - Raspberry Pi hostname/IP for deployment script

**Configuration Files:**
- `raccoon.project.yml` - Project configuration (hardware definitions, robot config, missions)
- `/etc/raccoon/server.yml` - System-wide server config
- `~/.raccoon/server.yml` - User server config
- `~/.raccoon/config.yml` - Global client config (known Pis, tokens)
- `~/.raccoon/api_token` - Server authentication token
- `.raccoonignore` - Sync exclusion patterns (like .gitignore)

**Build:**
- `pyproject.toml` - Package metadata and dependencies

## Platform Requirements

**Development (Laptop):**
- Python 3.8+
- SSH access to Pi (key-based authentication recommended)
- Network connectivity to Pi

**Production (Raspberry Pi):**
- Raspberry Pi (tested on Pi 4/5)
- Python 3.8+
- systemd for service management
- LCM library installed (optional, for LCM spy features)
- libstp library installed (for hardware control)

**Entry Points:**
```
raccoon = raccoon.cli:main          # Client CLI
raccoon-server = raccoon.server.cli:main  # Server management CLI
```

---

*Stack analysis: 2026-01-18*
