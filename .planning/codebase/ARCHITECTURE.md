# Architecture

**Analysis Date:** 2026-01-18

## Pattern Overview

**Overall:** Client-Server Architecture with CLI-Driven Development Workflow

**Key Characteristics:**
- Two deployment contexts: laptop-side CLI (`raccoon`) and Pi-side daemon (`raccoon-server`)
- YAML-driven configuration with code generation at build time
- Remote execution with real-time output streaming via WebSockets
- SFTP-based file synchronization for code deployment to embedded hardware

## Layers

**CLI Layer (Laptop-side):**
- Purpose: User-facing command-line interface for project management
- Location: `raccoon/cli.py`, `raccoon/commands/`
- Contains: Click-based command definitions, user prompts, progress display
- Depends on: Client layer, Codegen layer, Project utilities
- Used by: End users directly

**Client Layer:**
- Purpose: Handles communication between laptop CLI and Pi server
- Location: `raccoon/client/`
- Contains: Connection management, SSH/SFTP sync, API client, WebSocket output handler
- Depends on: paramiko (SSH), httpx (HTTP), websocket-client
- Used by: CLI commands that interact with remote Pi

**Server Layer (Pi-side):**
- Purpose: REST API and WebSocket server running on Raspberry Pi
- Location: `raccoon/server/`
- Contains: FastAPI routes, WebSocket handlers, command execution, project management
- Depends on: FastAPI, uvicorn, asyncio
- Used by: Laptop CLI via HTTP/WebSocket

**Codegen Layer:**
- Purpose: Generates Python code from YAML configuration
- Location: `raccoon/codegen/`
- Contains: Pipeline orchestration, generator classes, introspection, YAML resolution
- Depends on: black (formatting), jinja2 (templating)
- Used by: CLI run/codegen commands, server codegen endpoint

**Templates Layer:**
- Purpose: Project and mission scaffolding templates
- Location: `raccoon/templates/`
- Contains: Jinja2 templates for project structure, missions, config files
- Depends on: jinja2, jinja2-time
- Used by: `raccoon create project`, `raccoon create mission`

**Project Layer:**
- Purpose: Project discovery and configuration loading
- Location: `raccoon/project.py`
- Contains: Project root finding, YAML config loading, validation
- Depends on: pyyaml
- Used by: All CLI commands requiring project context

## Data Flow

**Local Execution Flow:**

1. User runs `raccoon run` in project directory
2. CLI finds project root by searching for `raccoon.project.yml`
3. Config loaded from YAML and validated
4. Codegen pipeline generates `defs.py` and `robot.py` in `src/hardware/`
5. `src.main` executed as subprocess with project root in Python path
6. Output displayed in terminal with exit code reporting

**Remote Execution Flow:**

1. User runs `raccoon run` (without --local flag)
2. Connection manager checks for active Pi connection (from project config or global config)
3. Project synced to Pi via SFTP (`SftpSync` with hash-based change detection)
4. HTTP POST to `/api/v1/run/{project_id}` starts execution on Pi
5. WebSocket connection to `/ws/output/{command_id}` for real-time output streaming
6. `OutputHandler` displays remote output locally
7. Exit code retrieved and displayed

**Code Generation Flow:**

1. `CodegenPipeline` orchestrates registered generators
2. Each generator (DefsGenerator, RobotGenerator) extracts relevant config
3. Introspection resolves class types from `libstp` library
4. `build_constructor_expr` generates typed Python constructor calls
5. Code formatted with black and written to `src/hardware/`
6. Cache (JSON) stores fingerprints to skip unchanged regenerations

**State Management:**
- Project config: `raccoon.project.yml` in project root
- Connection state: `~/.raccoon/config.yml` (global), project-level in `raccoon.project.yml`
- API tokens: `~/.raccoon/api_token` on Pi, fetched via SSH by client
- Codegen cache: `.codegen_cache.json` in output directory

## Key Abstractions

**Generator (BaseGenerator):**
- Purpose: Template Method pattern for code generation
- Examples: `raccoon/codegen/generators/defs_generator.py`, `raccoon/codegen/generators/robot_generator.py`
- Pattern: Abstract base class with hooks for extract_config, validate_config, generate_body

**ConnectionManager:**
- Purpose: Manages laptop-to-Pi connection state and SSH client lifecycle
- Examples: `raccoon/client/connection.py`
- Pattern: Singleton-like global instance with state persistence

**CommandExecutor:**
- Purpose: Async subprocess execution with output streaming
- Examples: `raccoon/server/services/executor.py`
- Pattern: Publisher-subscriber for output broadcasting to WebSocket clients

**YamlResolver:**
- Purpose: Maps YAML type names to Python classes with parameter extraction
- Examples: `raccoon/codegen/yaml_resolver.py`
- Pattern: Registry pattern with fallback lookup paths

## Entry Points

**raccoon CLI (laptop):**
- Location: `raccoon/cli.py` -> `main()`
- Triggers: User running `raccoon <command>` in terminal
- Responsibilities: Parse commands, initialize logging, delegate to command modules

**raccoon-server CLI (Pi):**
- Location: `raccoon/server/cli.py` -> `main()`
- Triggers: systemd service or manual `raccoon-server start`
- Responsibilities: Manage server lifecycle, systemd integration

**FastAPI Application:**
- Location: `raccoon/server/app.py` -> `app`
- Triggers: uvicorn import at server startup
- Responsibilities: HTTP/WebSocket routing, middleware, lifespan management

**Project Main:**
- Location: User project `src/main.py`
- Triggers: `raccoon run` command
- Responsibilities: Instantiate Robot, register missions, call `robot.start()`

## Error Handling

**Strategy:** Exception hierarchy with user-friendly messages

**Patterns:**
- `ProjectError` raised for project-related issues (missing config, invalid YAML)
- Commands catch exceptions and log via `logging_utils.py`
- Rich console output with styled error panels
- Server returns HTTP error codes with JSON detail messages
- WebSocket streams include error lines to output buffer

## Cross-Cutting Concerns

**Logging:**
- Centralized in `raccoon/logging_utils.py`
- Rich console handler with styled output
- Log level summary rendered after command completion
- Logger name: "raccoon" across all modules

**Validation:**
- YAML config validated during extraction in generators
- Hardware references validated against definitions section
- Type resolution validates class existence at generation time
- Server validates project existence before command execution

**Authentication:**
- API token stored in `~/.raccoon/api_token` on Pi (mode 600)
- Token fetched by client via SSH on connect
- All `/api/v1/*` routes require `X-API-Token` header
- Health endpoint (`/health`) is public for connection testing

---

*Architecture analysis: 2026-01-18*
