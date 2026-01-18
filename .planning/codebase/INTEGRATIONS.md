# External Integrations

**Analysis Date:** 2026-01-18

## APIs & External Services

**Raccoon Server (Internal Service):**
- Self-hosted REST API running on Raspberry Pi
- Port: 8421 (configurable via `RACCOON_PORT`)
- Client: `RaccoonApiClient` in `raccoon/client/api.py`
- Auth: API token via `X-API-Token` header

**API Endpoints:**
- `/health` - Health check (public)
- `/api/v1/projects` - Project management
- `/api/v1/run/{project_id}` - Execute project
- `/api/v1/calibrate/{project_id}` - Motor calibration
- `/api/v1/codegen/{project_id}` - Code generation
- `/api/v1/commands/{command_id}/status` - Command status
- `/api/v1/lcm/*` - LCM spy/playback endpoints
- `/api/v1/hardware/encoder/read` - Hardware encoder reading

**WebSocket Endpoints:**
- `/ws/output/{command_id}` - Command output streaming
- `/ws/lcm` - LCM message streaming

## Data Storage

**Databases:**
- None (no database used)

**File Storage:**
- Local filesystem only
- Projects stored in `RACCOON_PROJECTS_DIR` (default: `~/programs` on Pi)
- LCM recordings in `~/.raccoon/lcm_recordings/` (JSONL format)
- Configuration in `~/.raccoon/` directory

**Caching:**
- In-memory only (`_active_commands` dict in `raccoon/server/routes/commands.py`)
- No persistent cache

## Authentication & Identity

**Auth Provider:**
- Custom token-based authentication
- Implementation: `raccoon/server/auth.py`

**Auth Flow:**
1. Server generates random API token on first start (stored at `~/.raccoon/api_token`)
2. Client retrieves token via SSH (`cat ~/.raccoon/api_token`)
3. Client includes token in `X-API-Token` header for API requests
4. WebSocket auth via `?token=` query parameter

**SSH Authentication:**
- Used for: SFTP file sync, API token retrieval
- Client: paramiko SSHClient (`raccoon/client/connection.py`)
- Key-based authentication preferred (uses SSH agent or default key)

## Monitoring & Observability

**Error Tracking:**
- None (no external error tracking service)

**Logs:**
- Python standard logging via `logging_utils.py`
- Rich console output for CLI
- systemd journal for Pi server (via journalctl)

## CI/CD & Deployment

**Hosting:**
- Self-hosted on Raspberry Pi
- systemd service (`raccoon.service`)

**CI Pipeline:**
- None detected (no CI configuration files)

**Deployment Method:**
- Manual via `deploy.sh`:
  1. rsync files to Pi
  2. pip install on Pi
  3. Install systemd service

## Environment Configuration

**Required env vars:**
- None strictly required (all have defaults)

**Optional env vars:**
- `RACCOON_HOST` - Server bind address
- `RACCOON_PORT` - Server port
- `RACCOON_PROJECTS_DIR` - Projects directory
- `RPI_HOST` - Pi address for deploy script

**Secrets location:**
- `~/.raccoon/api_token` - Server authentication token (generated automatically)
- Permissions restricted to owner only (chmod 600)

## Webhooks & Callbacks

**Incoming:**
- None

**Outgoing:**
- None

## External Libraries (Runtime Dependencies)

**LCM (Lightweight Communications and Marshalling):**
- Purpose: Robot sensor/actuator message passing
- Import: `import lcm` (conditional, only on Pi)
- Message types: `raccoon/exlcm/*.py` (generated from `lcm-messages/types/*.lcm`)
- Used by: LCM spy service for message capture/playback

**libstp (Botball Hardware Abstraction):**
- Purpose: Hardware control (motors, servos, sensors, IMU)
- Not bundled - external dependency installed on Pi
- Used by: Generated `defs.py` and `robot.py` files
- Types resolved dynamically: Motor, Servo, DigitalSensor, IRSensor, IMU, etc.

## Inter-Process Communication

**WebSockets:**
- Real-time command output streaming (`/ws/output/{command_id}`)
- LCM message streaming (`/ws/lcm`)
- JSON protocol for messages

**SFTP:**
- File synchronization between laptop and Pi
- Hash-based change detection (SHA256)
- Implemented in `raccoon/client/sftp_sync.py`

**LCM:**
- Inter-process messaging on Pi
- Used for robot sensor data and commands
- Spy service captures and streams LCM traffic

## Hardware Integrations

**Raspberry Pi GPIO (via libstp):**
- Motors (PWM control, encoder feedback)
- Servos (position control)
- Digital sensors
- Analog sensors (IR, etc.)
- IMU (accelerometer, gyroscope)

**Network:**
- HTTP API (port 8421)
- SSH (port 22, via paramiko)
- WebSocket (same port as HTTP)

---

*Integration audit: 2026-01-18*
