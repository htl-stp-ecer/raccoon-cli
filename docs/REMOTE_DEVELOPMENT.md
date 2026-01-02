# Raccoon Remote Development Guide

This guide explains how to use Raccoon's remote development features to develop on your laptop and run code on the Raspberry Pi robot (Wombat).

## Overview

The Raccoon toolchain supports a client-server architecture where:
- **Your laptop** runs the Raccoon CLI to write code, sync files, and view output
- **The Raspberry Pi** runs a server daemon that executes commands and streams output back

This setup is ideal for:
- Windows users who want to use PyCharm for development
- Teams working with multiple robots
- Anyone who prefers developing on their laptop rather than directly on the Pi

## Architecture

```
┌─────────────────────┐         ┌─────────────────────────┐
│     LAPTOP CLI      │   SSH   │     RASPBERRY PI        │
│  (Windows/Linux)    │◄───────►│                         │
├─────────────────────┤         ├─────────────────────────┤
│ raccoon connect     │         │ raccoon-server daemon   │
│ raccoon sync        │  SFTP   │  - FastAPI HTTP service │
│ raccoon run         │────────►│  - WebSocket streaming  │
│ raccoon calibrate   │         │                         │
│                     │◄────────│                         │
│ PyCharm config gen  │  WS/SSH │ /home/pi/programs/      │
└─────────────────────┘         └─────────────────────────┘
```

## Quick Start

### 1. Install on Laptop

```bash
pip install raccoon
```

### 2. Setup the Pi (One-Time)

On your Raspberry Pi:

```bash
# Install raccoon
pip install raccoon

# Install and start the server daemon
sudo raccoon-server install
```

The server will now start automatically on boot.

### 3. Connect to Pi

From your laptop, find your Pi's IP address and connect:

```bash
# Connect to your Pi by IP address
raccoon connect 192.168.4.1

# Or by hostname (if resolvable)
raccoon connect raspberrypi.local
```

### 4. Create a Project

```bash
# Create a new project (opens PyCharm automatically)
raccoon create project MyRobot
```

PyCharm opens automatically. Follow the on-screen instructions to set up the SSH interpreter.

### 5. Run on Robot

```bash
# Syncs code to Pi and runs with streamed output
raccoon run
```

## Commands Reference

### Connection Commands

#### `raccoon connect <ADDRESS>`

Connect to a Raccoon Pi server at the specified address.

```bash
# Connect by IP address
raccoon connect 192.168.4.1

# Connect by hostname
raccoon connect raspberrypi.local

# Specify custom port (default: 8421)
raccoon connect 192.168.4.1 --port 8421

# Specify SSH user (default: pi)
raccoon connect 192.168.4.1 --user myuser
```

#### `raccoon disconnect`

Disconnect from the current Pi.

```bash
raccoon disconnect
```

#### `raccoon status`

Show connection status and project information.

```bash
raccoon status
```

Output shows:
- Current Pi connection (address, hostname, version)
- Local project info (name, UUID, path)
- Remote project status (if connected)
- Known Pis from previous connections

### Sync Commands

#### `raccoon sync`

Synchronize project files to the connected Pi.

```bash
# Sync current project
raccoon sync

# Force sync (re-upload all files)
raccoon sync --force

# Don't delete remote files not in local
raccoon sync --no-delete
```

The sync uses SFTP and only uploads changed files (hash-based comparison).

**Excluded from sync:**
- `.git/`
- `__pycache__/`
- `*.pyc`, `*.pyo`
- `.idea/`, `.vscode/`
- `venv/`, `.venv/`
- `*.egg-info/`
- `.pytest_cache/`, `.mypy_cache/`

### Execution Commands

#### `raccoon run`

Run the project. If connected to a Pi, runs remotely with auto-sync.

```bash
# Run on Pi (auto-syncs first)
raccoon run

# Force local execution (skip remote)
raccoon run --local

# Pass arguments to the program
raccoon run -- --arg1 value1
```

When running remotely:
1. Syncs project files to Pi
2. Executes `raccoon run` on Pi
3. Streams output back to your terminal in real-time
4. Reports exit code when done

#### `raccoon calibrate`

Calibrate robot motors. Runs remotely if connected.

```bash
# Run calibration on Pi
raccoon calibrate

# Use aggressive mode (relay feedback)
raccoon calibrate --aggressive

# Force local execution
raccoon calibrate --local
```

### PyCharm Setup

When you create a project with `raccoon create project`, PyCharm opens automatically.

To configure the SSH Python interpreter:

1. Run `raccoon connect <PI_ADDRESS>` to connect to your Pi
2. Follow the JetBrains guide for configuring a remote interpreter via SSH:
   https://www.jetbrains.com/help/pycharm/configuring-remote-interpreters-via-ssh.html

Use these settings:
- **Host:** Your Pi's IP address (e.g., `192.168.4.1`)
- **Username:** `pi`
- **Python interpreter path:** `/usr/bin/python3`

## Pi Server Commands

Run these on the Raspberry Pi.

#### `raccoon-server start`

Start the server in foreground mode (for testing).

```bash
raccoon-server start
```

#### `raccoon-server install`

Install as a systemd service (requires sudo).

```bash
sudo raccoon-server install

# Specify different user
sudo raccoon-server install --user myuser
```

#### `raccoon-server status`

Check if the server is running.

```bash
raccoon-server status
```

#### `raccoon-server logs`

View recent server logs.

```bash
raccoon-server logs

# Follow logs in real-time
raccoon-server tail -f
```

#### `raccoon-server restart`

Restart the server service.

```bash
sudo raccoon-server restart
```

#### `raccoon-server uninstall`

Remove the systemd service.

```bash
sudo raccoon-server uninstall
```

## Network Configuration

### Hotspot Mode

When the Pi creates its own WiFi hotspot:
- Default IP: `192.168.4.1`
- Port: `8421`

### Local Network

When both laptop and Pi are on the same WiFi/LAN:
- Find your Pi's IP address using `hostname -I` on the Pi
- Or check your router's device list

### Direct Connection

USB or Ethernet direct connection:
- Configure Pi with static IP
- Use `raccoon connect <IP>`

## Finding Your Pi's IP Address

On the Raspberry Pi, run:

```bash
hostname -I
```

Or check using the network settings in the desktop environment.

## Troubleshooting

### "Failed to connect"

1. Check Pi is powered on
2. Verify `raccoon-server` is running: `raccoon-server status`
3. Check network connectivity (can you ping the Pi?)
4. Make sure you have the correct IP address

### Sync fails

1. Verify SSH access: `ssh pi@<PI_IP>`
2. Check SSH keys are set up (or use password auth)
3. Ensure the projects directory exists: `/home/pi/programs/`

### PyCharm shows "Cannot connect to interpreter"

1. Verify Pi is reachable
2. Check PyCharm's SSH configuration matches the Pi settings
3. Try: File > Invalidate Caches / Restart

### WebSocket connection fails

1. Check firewall allows port 8421
2. Restart the server: `sudo raccoon-server restart`
3. Check server logs: `raccoon-server logs`

## Configuration Files

### Global Config (`~/.raccoon/config.yml`)

Stores known Pis and default settings:

```yaml
known_pis:
  - hostname: raccoon-pi
    address: 192.168.4.1
    port: 8421
    last_seen: "2025-01-15T10:30:00"

default_pi_user: pi
```

### Project Config (`raccoon.project.yml`)

Connection settings saved per-project:

```yaml
name: MyRobot
uuid: abc-123-def

connection:
  pi_address: 192.168.4.1
  pi_port: 8421
  pi_user: pi
  remote_path: /home/pi/programs/abc-123-def

# ... robot configuration ...
```

### Server Config (`/etc/raccoon/server.yml` or `~/.raccoon/server.yml`)

Pi-side server configuration:

```yaml
host: "0.0.0.0"
port: 8421
projects_dir: /home/pi/programs
```

Environment variables can override:
- `RACCOON_HOST`
- `RACCOON_PORT`
- `RACCOON_PROJECTS_DIR`

## Security Notes

- Communication uses HTTP/WebSocket (not encrypted by default)
- In hotspot mode, only devices on the Pi's network can connect
- For production use, consider adding TLS and authentication
- SSH keys are recommended for SFTP sync

## API Reference

The Pi server exposes a REST API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /health` | - | Health check and version |
| `GET /api/v1/projects` | - | List projects on Pi |
| `GET /api/v1/projects/{id}` | - | Get project details |
| `POST /api/v1/run/{id}` | - | Start running a project |
| `POST /api/v1/calibrate/{id}` | - | Start calibration |
| `GET /api/v1/commands/{id}/status` | - | Check command status |
| `WS /ws/output/{id}` | - | Stream command output |

Default URL: `http://<pi-ip>:8421`
