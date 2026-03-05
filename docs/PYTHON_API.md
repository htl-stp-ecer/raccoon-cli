# Python API Reference

This document covers the maintained Python-facing surfaces in the repository. It is intentionally tied to the code that exists today rather than to placeholder packaging or release docs.

## Packages

### `raccoon`

Primary CLI and backend package for project management, code generation, remote execution, and IDE services.

Key modules:

- `raccoon.cli`: Click entry point for the `raccoon` command.
- `raccoon.project`: project-root discovery and `raccoon.project.yml` loading helpers.
- `raccoon.checkpoint`: git-backed invisible checkpoints stored under `refs/raccoon/checkpoints/`.
- `raccoon.client.api`: async HTTP client for the Pi-side server API.
- `raccoon.ide.*`: FastAPI backend, schemas, and services for the IDE/editor workflow.

### `raccoon_transport`

Small shared transport package around LCM.

Key exports:

- `raccoon_transport.Transport`
- `raccoon_transport.Channels`
- `raccoon_transport.ProtocolChannels`

Install it separately when developing against the Python transport package:

```bash
pip install -e raccoon-transport/python
```

## Core APIs

### Project helpers

Source: `raccoon.project`

- `find_project_root(start_path: Path | None = None) -> Path`
  Searches upward for `raccoon.project.yml` and returns the owning directory.
- `load_project_config(project_root: Path | None = None) -> dict[str, Any]`
  Parses and validates the current project config.
- `require_project() -> Path`
  Convenience wrapper used by commands that must run inside a project.

These functions raise `ProjectError` on invalid context or unreadable config.

### Checkpoints

Source: `raccoon.checkpoint`

- `create_checkpoint(project_root, label="checkpoint")`
  Captures staged and unstaged work without mutating the stash list.
- `list_checkpoints(project_root)`
  Returns checkpoints newest-first.
- `show_checkpoint_diff(project_root, identifier)`
  Shows the patch represented by a checkpoint.
- `restore_checkpoint(project_root, identifier)`
  Applies a checkpoint back onto the working tree.
- `delete_checkpoint(project_root, identifier)`
  Removes a checkpoint ref.
- `clean_checkpoints(project_root, max_age_days=7, delete_all=False)`
  Deletes old or all checkpoint refs.

Identifiers can be a 1-based list index or a commit SHA prefix.

### Pi HTTP client

Source: `raccoon.client.api`

Use `RaccoonApiClient` as an async context manager:

```python
from raccoon.client.api import RaccoonApiClient

async with RaccoonApiClient("http://192.168.4.1:8421", api_token="...") as client:
    health = await client.health()
    projects = await client.list_projects()
```

Important methods:

- `health()`
- `list_projects()`
- `get_project(project_id)`
- `run_project(project_id, args=None, env=None)`
- `calibrate_project(project_id, args=None, env=None)`
- `codegen_project(project_id, args=None, env=None)`
- `get_command_status(command_id)`
- `cancel_command(command_id)`
- `read_encoder(port, inverted=False)`

Returned models are lightweight dataclasses:

- `ProjectInfo`
- `CommandResult`
- `EncoderReading`

### Transport

Source: `raccoon_transport`

Example:

```python
from raccoon_transport import Channels, Transport
from raccoon_transport.types.raccoon.scalar_f_t import scalar_f_t

transport = Transport.create()

message = scalar_f_t()
message.value = 12.5
transport.publish(Channels.BATTERY_VOLTAGE, message, retained=True)
```

Important methods:

- `Transport.create(provider="")`
- `Transport.publish(channel, message, reliable=False, retained=False)`
- `Transport.subscribe(channel, handler, reliable=False, request_retained=False)`
- `Transport.spin_once(timeout_ms=100)`
- `Transport.spin()`
- `Transport.close()`

Notes:

- `reliable=True` is reserved for future work and currently falls back to plain LCM publish/subscribe with a warning.
- `retained=True` caches the last encoded payload for replay to future subscribers that request retained state.

### Channel namespaces

Source: `raccoon_transport.channels`

Use constants for singleton channels and helpers for indexed channels:

```python
Channels.BATTERY_VOLTAGE
Channels.motor_velocity_command(0)
Channels.servo_position(2)
ProtocolChannels.RETAIN_REQUEST
```

## IDE Backend APIs

The IDE backend is implemented in `raccoon.ide`. Its public shape is split into routes, services, and schemas.

### Schemas

- `raccoon.ide.schemas.project`
  Project CRUD and connection models.
- `raccoon.ide.schemas.mission`
  Compact mission-list models.
- `raccoon.ide.schemas.mission_detail`
  Rich editor document models such as `ParsedMission`, `ParsedStep`, `ParsedComment`, and `ParsedGroup`.

### Services

- `ProjectService`
  CRUD-oriented access to IDE project records.
- `StepDiscoveryService`
  Discovers local steps and manages the cached libstp step index.
- `MissionService`
  Handles mission discovery, parsing, JSON updates, execution, breakpoints, and simulation payload generation.

### Routes

- `raccoon.ide.routes.projects`
  Project CRUD endpoints.
- `raccoon.ide.routes.steps`
  Step discovery and step-index cache endpoints.
- `raccoon.ide.routes.missions`
  Mission list/detail/edit/run/simulation endpoints.

## How to Inspect Docs Locally

For quick standard-library output:

```bash
python3 -m pydoc raccoon.project
python3 -m pydoc raccoon.client.api
python3 -m pydoc raccoon_transport.transport
```

For source-first reading, prefer the module docstrings and type annotations in the package itself. Those are the maintained source of truth.
