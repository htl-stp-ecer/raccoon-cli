# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Raccoon is a toolchain CLI for Botball robot development using Raspberry Pi (Wombat). It provides project scaffolding, hardware configuration, code generation, motor calibration, and remote development with a client-server architecture.

## Common Commands

### Development
```bash
pip install -e .                    # Install locally for development
pytest tests/                       # Run all tests
pytest tests/test_sftp_sync.py -v   # Run specific test file
raccoon --help                      # View CLI help
```

### CLI Usage (after installation)
```bash
raccoon create project MyRobot      # Create new project
raccoon wizard                      # Interactive hardware configuration
raccoon codegen                     # Generate hardware code from YAML
raccoon calibrate                   # Calibrate motor PID/feedforward
raccoon run                         # Run project (auto-codegen + execute)
```

### Remote Development (laptop → Pi)
```bash
raccoon connect 192.168.4.1         # Connect to Pi server
raccoon sync                        # Sync files via SFTP
raccoon run                         # Auto-syncs and runs on Pi
```

### Pi Server
```bash
raccoon-server start                # Start server (foreground)
sudo raccoon-server install         # Install as systemd service
```

## Architecture

```
raccoon/
├── cli.py                 # Main Click CLI entry point
├── project.py             # Project discovery & validation
├── commands/              # CLI command implementations
│   ├── create.py          # Project/mission creation
│   ├── wizard.py          # Interactive hardware config
│   ├── codegen.py         # Code generation orchestration
│   ├── run.py             # Execute projects
│   └── calibrate/         # Motor calibration suite
├── codegen/               # Code generation system
│   ├── pipeline.py        # Main orchestrator
│   ├── generators/        # Pluggable generators (defs, robot)
│   │   ├── registry.py    # Generator discovery
│   │   └── base.py        # Abstract generator base
│   └── class_builder.py   # Python AST generation
├── client/                # Laptop-side (SSH/SFTP/HTTP)
│   ├── sftp_sync.py       # Hash-based file sync
│   ├── connection.py      # Connection management
│   └── api.py             # REST API client
├── server/                # Pi-side FastAPI daemon (raccoon-server)
│   ├── app.py             # FastAPI application
│   ├── routes/            # HTTP API endpoints
│   └── services/          # Business logic (executor, project manager)
├── ide/                   # Laptop-side FastAPI daemon (IDE backend)
│   ├── app.py             # FastAPI application
│   ├── routes/            # HTTP API endpoints
│   └── repositories/      # ProjectRepository, etc.
└── templates/             # Jinja2 project scaffolding
```

### Key Patterns

- **Generator Registry**: Pluggable code generators in `codegen/generators/` are auto-discovered via `registry.py`
- **Hash-based Sync**: SFTP sync uses content hashing for change detection, with `.raccoon_manifest.json` tracking state
- **Two backends** — the Web-IDE talks to *both*:
  - **IDE backend** (`raccoon_cli/ide/`, laptop, default port 3000): owns the project files on disk. Handles project CRUD, missions, steps, type definitions, files, arm chain read/IK/FK/positions.
  - **Pi server** (`raccoon_cli/server/`, robot, port 8421): owns the hardware. Handles real-time execution, motor/servo/hardware access, and arm `/command` (moves servos via `raccoon.hal`).
  - Frontend split: `HttpService.localApi(...)` → IDE backend; `HttpService.deviceApi(...)` → Pi server. When adding a new endpoint, decide *which* server actually has the data/hardware it needs — putting it on the wrong one yields a generic 404 (FastAPI route-not-found).

### Configuration Files

- `raccoon.project.yml` - Per-project config (hardware, motors, drivetrain, connection settings)
- `~/.raccoon/config.yml` - Global client config (known Pis, default user)
- `/etc/raccoon/server.yml` - Pi server config (host, port, projects directory)
- `.raccoonignore` - Fnmatch patterns for sync exclusion

## Code Generation Flow

1. `raccoon.project.yml` defines hardware (motors, sensors, drivetrain type)
2. `raccoon codegen` reads YAML and runs generators via pipeline
3. Generators output to `src/hardware/defs.py` (definitions) and `src/hardware/robot.py` (robot class)
4. Generated code uses Black formatting
