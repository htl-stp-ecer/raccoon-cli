# Codebase Structure

**Analysis Date:** 2026-01-18

## Directory Layout

```
toolchain/
├── raccoon/                    # Main Python package
│   ├── cli.py                  # Laptop CLI entry point
│   ├── project.py              # Project discovery utilities
│   ├── logging_utils.py        # Logging configuration
│   ├── __init__.py             # Package version
│   ├── commands/               # CLI command implementations
│   ├── client/                 # Laptop-to-Pi communication
│   ├── server/                 # Pi-side FastAPI server
│   ├── codegen/                # Code generation system
│   ├── templates/              # Project scaffolding templates
│   ├── exlcm/                  # LCM message types (generated)
│   ├── ide/                    # IDE integration (PyCharm launcher)
│   └── systemd/                # systemd service files
├── example/                    # Example projects
│   ├── manual-example/         # Full example with missions
│   └── tobi-test/              # Another example project
├── lcm-messages/               # LCM message definitions (source)
├── remote-project/             # Template for remote project structure
├── pyproject.toml              # Package configuration
├── uv.lock                     # Dependency lock file
├── COMMANDS.md                 # CLI command documentation
└── deploy.sh                   # Deployment script
```

## Directory Purposes

**raccoon/commands/:**
- Purpose: CLI command implementations using Click
- Contains: One file per command/command group
- Key files:
  - `__init__.py`: Exports all commands
  - `run.py`: Run command (local/remote execution)
  - `create.py`: Project and mission creation
  - `sync_cmd.py`: SFTP synchronization
  - `connect.py`: Pi connection management
  - `codegen.py`: Manual code generation
  - `calibrate.py`: Motor calibration
  - `wizard.py`: Project setup wizard
  - `lcm.py`: LCM-related commands

**raccoon/client/:**
- Purpose: Laptop-side utilities for Pi communication
- Contains: Connection, sync, API client, output handling
- Key files:
  - `connection.py`: ConnectionManager, state persistence
  - `sftp_sync.py`: SftpSync class, hash-based sync
  - `api.py`: RaccoonApiClient (httpx-based)
  - `output_handler.py`: WebSocket output streaming
  - `discovery.py`: Pi auto-discovery utilities
  - `ssh_keys.py`: SSH key management

**raccoon/server/:**
- Purpose: Pi-side FastAPI application
- Contains: Routes, services, WebSocket handlers, config
- Key files:
  - `app.py`: FastAPI app factory, middleware setup
  - `cli.py`: Server management CLI (start, install, status)
  - `config.py`: ServerConfig, token management
  - `auth.py`: API token authentication dependency
  - `routes/`: API endpoint routers
  - `services/`: Business logic (executor, project_manager)
  - `websocket/`: WebSocket handlers (output_stream, lcm_stream)

**raccoon/codegen/:**
- Purpose: YAML-to-Python code generation system
- Contains: Pipeline, generators, introspection, builders
- Key files:
  - `pipeline.py`: CodegenPipeline orchestration
  - `generators/base.py`: BaseGenerator template method
  - `generators/defs_generator.py`: Hardware definitions generator
  - `generators/robot_generator.py`: Robot class generator
  - `generators/registry.py`: GeneratorRegistry
  - `introspection.py`: Class/parameter resolution
  - `builder.py`: Constructor expression building
  - `yaml_resolver.py`: Type name to class mapping
  - `class_builder.py`: ClassBuilder for Python class generation
  - `cache.py`: Codegen caching utilities

**raccoon/templates/:**
- Purpose: Jinja2 templates for project scaffolding
- Contains: Project and mission templates
- Key files:
  - `project_scaffold/`: Complete project template
    - `raccoon.project.yml.jinja`: Project config template
    - `src/`: Source directory template
    - `.gitignore.jinja`, `.raccoonignore.jinja`
  - `mission/`: Mission file template
    - `src/missions/{{mission_snake_case}}_mission.py.jinja`

**example/:**
- Purpose: Working example projects for testing/reference
- Contains: Complete raccoon projects with missions
- Key files:
  - `manual-example/raccoon.project.yml`: Example project config
  - `manual-example/src/main.py`: Example entry point
  - `manual-example/src/missions/`: Example mission files

## Key File Locations

**Entry Points:**
- `raccoon/cli.py`: Main CLI entry (`raccoon` command)
- `raccoon/server/cli.py`: Server CLI entry (`raccoon-server` command)
- `raccoon/server/__main__.py`: Server module execution
- `raccoon/server/app.py`: FastAPI application instance

**Configuration:**
- `pyproject.toml`: Package metadata, dependencies, entry points
- `raccoon/server/config.py`: Server configuration loading
- User projects: `raccoon.project.yml`
- Global config: `~/.raccoon/config.yml`

**Core Logic:**
- `raccoon/codegen/pipeline.py`: Codegen orchestration
- `raccoon/codegen/generators/base.py`: Generator base class
- `raccoon/client/connection.py`: Connection state management
- `raccoon/server/services/executor.py`: Command execution

**Testing:**
- No dedicated test directory detected
- Example projects serve as integration test cases

## Naming Conventions

**Files:**
- `snake_case.py` for all Python modules
- Commands use `*_cmd.py` to avoid name conflicts (e.g., `sync_cmd.py`, `list_cmd.py`, `remove_cmd.py`)
- Generators use `*_generator.py` pattern
- Templates use `.jinja` extension

**Directories:**
- `snake_case` for all directories
- Plural names for collections (`commands/`, `routes/`, `templates/`)

**Classes:**
- `PascalCase` for all classes
- `*Generator` suffix for code generators
- `*Manager` suffix for state/lifecycle managers
- `*Handler` suffix for event/request handlers

**Functions:**
- `snake_case` for all functions
- Private functions prefixed with `_`
- Click commands use `*_command` suffix

## Where to Add New Code

**New CLI Command:**
1. Create file: `raccoon/commands/{name}.py`
2. Define Click command with `@click.command(name="...")`
3. Export in `raccoon/commands/__init__.py`
4. Register in `raccoon/cli.py` with `main.add_command()`

**New Code Generator:**
1. Create file: `raccoon/codegen/generators/{name}_generator.py`
2. Subclass `BaseGenerator` from `raccoon/codegen/generators/base.py`
3. Implement: `get_output_filename()`, `extract_config()`, `validate_config()`, `generate_body()`
4. Register in `raccoon/codegen/pipeline.py` `_setup_default_generators()`

**New Server Route:**
1. Create file: `raccoon/server/routes/{name}.py`
2. Define `router = APIRouter(prefix="/api/v1/{name}", tags=["..."])`
3. Export in `raccoon/server/routes/__init__.py`
4. Include in `raccoon/server/app.py` with `app.include_router()`

**New Client Utility:**
1. Add to appropriate file in `raccoon/client/` or create new module
2. Export in `raccoon/client/__init__.py` if public API

**New Mission in User Project:**
1. Run `raccoon create mission <name>`
2. Or manually create `src/missions/{name}_mission.py`
3. Add to `missions:` list in `raccoon.project.yml`

**Utilities:**
- Shared helpers: `raccoon/` root level (like `logging_utils.py`)
- Project utilities: `raccoon/project.py`

## Special Directories

**raccoon/exlcm/:**
- Purpose: Generated LCM (Lightweight Communications and Marshalling) message types
- Generated: Yes (from `lcm-messages/` definitions)
- Committed: Yes (generated code checked in)

**lcm-messages/:**
- Purpose: LCM message type definitions
- Generated: No (source definitions)
- Committed: Yes

**build/:**
- Purpose: Python package build artifacts
- Generated: Yes (by setuptools)
- Committed: No (in .gitignore)

**.venv/:**
- Purpose: Python virtual environment
- Generated: Yes (by uv/pip)
- Committed: No

**raccoon.egg-info/:**
- Purpose: Python package metadata (editable install)
- Generated: Yes
- Committed: No (in .gitignore)

---

*Structure analysis: 2026-01-18*
